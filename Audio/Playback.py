"""Speaker-owned synchronous Piper/aplay pipeline with IPC envelope emission."""
from Audio import config
from Logging import (
    logger, stream_subprocess_output, current_context,
)
import logging
import threading
import subprocess
import time
import numpy as np


def _wait_process(process, name, timeout=5):
    """Always reap a launched process, including after a timeout or pipe failure."""
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        logger.error("subprocess_timeout process=%s timeout_s=%s", name, timeout)
        process.terminate()
        try:
            returncode = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait(timeout=2)
    logger.log(logging.INFO if returncode == 0 and not timed_out else logging.ERROR,
               "subprocess_exit process=%s returncode=%s timed_out=%s", name, returncode, timed_out)
    return returncode, timed_out


def play(text, *, on_envelope, process_callback, output=None):
    started = time.monotonic()
    stderr_workers = []
    processes = {}
    metrics = {"bytes_written": 0, "playback_started": None, "first_audio_at": None}
    generation_finished = None
    generation_ok = False
    failed = False

    try:
        if output is None:
            from Audio.Output import Output
            output = Output()
        output.prepare()
        piper = subprocess.Popen(
            [config.piper_path(), "--model", config.model_path(), "--output_raw", "--threads", "2"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        processes["piper"] = piper
        process_callback(piper)
        stderr_workers.append(_stderr_worker(piper, "piper"))
        aplay = subprocess.Popen(
            ["aplay", "-D", output.device, "-r", "22050", "-f", "S16_LE", "-t", "raw",
             "--buffer-time=20000", "--period-time=5000"],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        processes["aplay"] = aplay
        process_callback(aplay)
        stderr_workers.append(_stderr_worker(aplay, "aplay"))
        piper.stdin.write(text.encode("utf-8"))
        piper.stdin.close()
        _stream_audio(piper, aplay, metrics, started, on_envelope)
        generation_finished = time.monotonic()
    except Exception:
        failed = True
        logger.exception("Speech generation or playback failed")
    finally:
        for name, process in processes.items():
            try:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
            except Exception:
                failed = True
                logger.exception("Unable to close subprocess input process=%s", name)
        for name, process in processes.items():
            try:
                returncode, timed_out = _wait_process(process, name)
                if name == "piper":
                    generation_ok = returncode == 0 and not timed_out and generation_finished is not None
                failed = failed or returncode != 0 or timed_out
            except Exception:
                failed = True
                logger.exception("Unable to reap subprocess process=%s", name)
        # aplay has exited: buffered audio has drained, or playback failed.
        finished = time.monotonic()
        for worker in stderr_workers:
            try:
                worker.join(timeout=1)
            except Exception:
                failed = True
                logger.exception("Unable to join subprocess logging worker")
        for process in processes.values():
            try:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            except Exception:
                failed = True
                logger.exception("Unable to close subprocess output")

        completed = not failed and metrics["bytes_written"] > 0
        logger.log(logging.INFO if generation_ok else logging.ERROR,
                   "stage=speech_generation status=%s elapsed_ms=%.1f",
                   "completed" if generation_ok else "failed", ((generation_finished or finished) - started) * 1000)
        playback_ms = 0 if metrics["playback_started"] is None else (finished - metrics["playback_started"]) * 1000
        logger.log(logging.INFO if completed else logging.ERROR,
                   "stage=playback status=%s elapsed_ms=%.1f speech_elapsed_ms=%.1f audio_duration_ms=%.1f bytes_written=%s",
                   "completed" if completed else "failed", playback_ms, (finished - started) * 1000,
                   metrics["bytes_written"] / (config.SAMPLE_RATE * config.BYTES_PER_FRAME) * 1000,
                   metrics["bytes_written"])
        on_envelope(0.0, 0.0, True)
    return completed


def _stderr_worker(process, name):
    worker = threading.Thread(
        target=stream_subprocess_output,
        kwargs={"pipe": process.stderr, "prefix": f"[{name}] ", "context": current_context()},
        daemon=True,
    )
    worker.start()
    return worker


def _stream_audio(piper, aplay, metrics, speech_started, on_envelope):
    envelope = 0.0
    audio_written = 0.0
    while True:
        chunk = piper.stdout.read(config.CHUNK_BYTES)
        if not chunk:
            break
        if metrics["first_audio_at"] is None:
            metrics["first_audio_at"] = time.monotonic()
            logger.info("stage=tts_first_audio elapsed_ms=%.1f", (metrics["first_audio_at"] - speech_started) * 1000)
        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            continue
        rms = float(np.sqrt(np.mean(samples * samples)) / 32768.0)
        gain = config.ENVELOPE_ATTACK if rms > envelope else config.ENVELOPE_RELEASE
        envelope += (rms - envelope) * gain
        audio_t0 = audio_written
        audio_written += samples.size / config.SAMPLE_RATE
        # Local playback clock only. Face receives timestamped envelopes by IPC.
        while True:
            ahead = audio_written - (time.monotonic() - metrics["first_audio_at"])
            if ahead <= config.WRITE_AHEAD_SECONDS:
                break
            time.sleep(min(ahead - config.WRITE_AHEAD_SECONDS, 0.01))
        aplay.stdin.write(chunk)
        if metrics["playback_started"] is None:
            metrics["playback_started"] = time.monotonic()
        metrics["bytes_written"] += len(chunk)
        on_envelope(metrics["first_audio_at"] + audio_t0, min(envelope * 3.0, 1.0), False)
