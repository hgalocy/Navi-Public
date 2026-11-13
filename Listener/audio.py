"""Listener-local streaming state. Audio never stops for inference or IPC."""
import base64
import io
import math
import queue
import threading
import time
import uuid
import wave

from .capture import FRAME_BYTES, FRAME_SECONDS, SAMPLE_RATE, Stream
from Services.ipc import IPCError, call

REQUEST_SECONDS = 4
PREROLL_BYTES = int(1.5 * SAMPLE_RATE) * 2
MAX_WAV_BYTES = 44 + PREROLL_BYTES + REQUEST_SECONDS * SAMPLE_RATE * 2
COOLDOWN_SECONDS = 2


class ContinuousAudio:
    def __init__(self, detector=None, threshold=0.95, stream_factory=Stream, ipc_call=call, clock=time.monotonic):
        self.detector, self.threshold = detector, threshold
        self.stream_factory, self.call, self.clock = stream_factory, ipc_call, clock
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.frames = queue.Queue(maxsize=8)
        self.events = queue.Queue(maxsize=1)
        self.threads = []
        self.ring = bytearray()
        self.capture = None
        self.error = None
        self.last_frame = self.last_inference = self.last_ipc = self.clock()
        self.speaker_safe = False  # Fail closed until Speaker health has been read.
        self.speaker_checked = 0
        self.generation = 0
        # Detector construction already warmed/reset generation zero. Later
        # resets rebuild several seconds of openWakeWord features and can be slow.
        self.model_generation = 0
        self.cooldown_until = 0
        self.above = False
        self.last_score = self.peak_score = 0.0
        self.inference_frames = 0

    def start(self, track):
        for target in (lambda: self._capture_loop(track), self._infer_loop, self._ipc_loop):
            thread = threading.Thread(target=self._guard, args=(target,), daemon=True)
            self.threads.append(thread)
            thread.start()
        if not self.ready.wait(6):
            raise RuntimeError("Microphone did not deliver a frame before startup deadline")
        self.check()

    def _guard(self, target):
        try:
            target()
        except Exception as exc:
            if not self.stop.is_set():
                from Logging import logger
                logger.exception("listener_stream_failed")
                with self.condition:
                    self.error = str(exc) or type(exc).__name__
                    self.condition.notify_all()
                self.ready.set()
                self.stop.set()

    def check(self):
        if self.error:
            raise RuntimeError(self.error)
        if self.stop.is_set() or any(not thread.is_alive() for thread in self.threads):
            raise RuntimeError("Listener audio worker stopped")
        if self.clock() - self.last_frame > 5:
            raise RuntimeError("Listener microphone stalled")
        if self.detector and self.clock() - self.last_inference > 5:
            raise RuntimeError("Listener inference stalled")
        if self.clock() - self.last_ipc > 5:
            raise RuntimeError("Listener IPC worker stalled")

    def status(self):
        with self.condition:
            return {"wake_enabled": self.detector is not None, "threshold": self.threshold,
                    "monitoring": self._armed(), "speaker_safe": self.speaker_safe,
                    "model_ready": self.detector is not None and self.model_generation == self.generation,
                    "activation_id": self.capture["id"] if self.capture else None,
                    "buffer_bytes": len(self.ring), "error": self.error,
                    "last_score": self.last_score, "peak_score": self.peak_score,
                    "inference_frames": self.inference_frames}

    def _armed(self):
        return (self.detector is not None and self.capture is None and self.speaker_safe
                and self.model_generation == self.generation
                and self.clock() - self.speaker_checked < 1.5
                and self.clock() >= self.cooldown_until)

    def ingest(self, raw, received=None):
        """Only the capture thread writes audio; tests can supply deterministic PCM."""
        now = self.clock() if received is None else received
        if len(raw) != FRAME_BYTES:
            raise RuntimeError("Invalid microphone frame length")
        with self.condition:
            self.last_frame = now
            self.ring.extend(raw)
            del self.ring[:-PREROLL_BYTES]
            capture = self.capture
            if capture and not capture["done"]:
                # The last frame can straddle the monotonic four-second deadline.
                remaining = max(0, min(len(raw) // 2,
                                      round((capture["deadline"] - (now - FRAME_SECONDS)) * SAMPLE_RATE))) * 2
                capacity = PREROLL_BYTES + REQUEST_SECONDS * SAMPLE_RATE * 2 - len(capture["pcm"])
                capture["pcm"].extend(raw[:min(remaining, capacity)])
                if now >= capture["deadline"]:
                    capture["done"] = True
                    self.condition.notify_all()
            generation, armed = self.generation, self._armed()
        self.ready.set()
        if armed:
            # A stalled model must fail visibly; capture is never blocked on inference.
            self.frames.put_nowait((raw, generation, armed))

    def _capture_loop(self, track):
        with self.stream_factory(track) as stream:
            while not self.stop.is_set():
                raw = stream.read_frame(self.stop)
                if raw is not None:
                    self.ingest(raw)

    def _infer_loop(self):
        while not self.stop.is_set():
            with self.condition:
                generation = self.generation
                needs_reset = self.detector is not None and self.model_generation != generation
            if needs_reset:
                self.detector.reset()
                # Stale inference frames can be discarded; all microphone PCM
                # continues into the ring and any active recording in ingest().
                while True:
                    try:
                        self.frames.get_nowait()
                    except queue.Empty:
                        break
                with self.condition:
                    if generation == self.generation:
                        self.model_generation = generation
                self.last_inference = self.clock()
                continue
            try:
                raw, generation, armed = self.frames.get(timeout=0.1)
            except queue.Empty:
                self.last_inference = self.clock()
                continue
            with self.condition:
                current = generation == self.generation and self._armed()
            if armed and current:
                score = self.detector.score(raw)
                self.detect(score, generation)
            self.last_inference = self.clock()

    def detect(self, score, generation=None):
        from Logging import interaction_context, interaction_details, logger
        with self.condition:
            if (generation is not None and generation != self.generation) or not self._armed():
                return None
            if not math.isfinite(score):
                raise RuntimeError("Wake model returned a non-finite score")
            self.last_score = score
            self.peak_score = max(self.peak_score, score)
            self.inference_frames += 1
            high = score >= self.threshold
            rising = high and not self.above
            self.above = high
            if not rising:
                return None
            capture = self._begin(True)
            details = interaction_details()
            self.events.put_nowait((capture["id"], details))
            with interaction_context(details):
                logger.info("wake_detected activation_id=%s score=%.5f threshold=%.5f", capture["id"], score, self.threshold)
            return capture["id"]

    def _begin(self, wake):
        if self.capture is not None:
            raise IPCError("Listener activation already in progress")
        now = self.clock()
        self.capture = {"id": uuid.uuid4().hex, "deadline": now + REQUEST_SECONDS,
                        "expires": now + 310, "pcm": bytearray(self.ring if wake else b""),
                        "done": False, "claimed": False, "delivered": False}
        self.generation += 1
        return self.capture

    def reserve(self, activation_id=None):
        with self.condition:
            if activation_id is None:
                capture = self._begin(False)
            else:
                capture = self.capture
                if capture is None or capture["id"] != activation_id:
                    raise IPCError("Unknown/stale Listener activation")
            if capture["claimed"]:
                raise IPCError("Listener activation already consumed")
            capture["claimed"] = True
            return capture["id"]

    def recording(self, activation_id):
        from Logging import logger, timed_stage
        with timed_stage("recording"), self.condition:
            deadline = self.clock() + 7
            while True:
                capture = self.capture
                if self.error or self.stop.is_set():
                    raise IPCError(self.error or "Listener stopping")
                if capture is None or capture["id"] != activation_id:
                    raise IPCError("Listener activation expired")
                if capture["done"]:
                    pcm = bytes(capture["pcm"])
                    capture["delivered"] = True
                    break
                if self.clock() > deadline:
                    raise IPCError("Continuous capture did not finish")
                self.condition.wait(0.1)
        with io.BytesIO() as output:
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(pcm)
            data = output.getvalue()
        if len(data) > MAX_WAV_BYTES:
            raise IPCError("Oversized Listener WAV")
        logger.info("recording_ready activation_id=%s samples=%s", activation_id, len(pcm) // 2)
        return {"activation_id": activation_id, "wav_base64": base64.b64encode(data).decode("ascii")}

    def release(self, activation_id):
        with self.condition:
            if self.capture and self.capture["id"] == activation_id:
                self.capture = None
                self._reset_window()
                self.condition.notify_all()

    def _reset_window(self):
        self.generation += 1
        self.ring.clear()
        self.cooldown_until = self.clock() + COOLDOWN_SECONDS
        # A continuing high score cannot create a second event after release.

    def playback_started(self):
        with self.condition:
            self.speaker_safe = False
            self._reset_window()

    def poll_speaker(self):
        with self.condition:
            generation = self.generation
        try:
            status = self.call("speaker", "health", timeout=0.4)
            safe = status["state"] == "ready"
        except (IPCError, OSError, KeyError, TypeError):
            safe = False
        with self.condition:
            # A health reply sampled before the synchronous playback barrier cannot undo it.
            if generation != self.generation:
                return
            self.speaker_checked = self.clock()
            if safe != self.speaker_safe:
                self.speaker_safe = safe
                self._reset_window()

    def _ipc_loop(self):
        from Logging import logger
        job = None
        while not self.stop.is_set():
            self.last_ipc = self.clock()
            self.poll_speaker()
            try:
                activation_id, details = self.events.get_nowait()
            except queue.Empty:
                pass
            else:
                try:
                    result = self.call("coordinator", "wake", {"activation_id": activation_id}, context=details, timeout=1)
                    job = (activation_id, result["job_id"])
                    logger.info("wake_accepted activation_id=%s job_id=%s", *job)
                except (IPCError, OSError, KeyError, TypeError) as exc:
                    logger.warning("wake_rejected activation_id=%s error=%s", activation_id, exc)
                    # Ambiguous acceptance is never retried or replayed.
                    self.release(activation_id)
            if job:
                try:
                    status = self.call("coordinator", "health", timeout=0.4)
                    finished = status["job_id"] != job[1]
                except (IPCError, OSError, KeyError, TypeError):
                    finished = True
                if finished:
                    self.release(job[0])
                    job = None
            else:
                with self.condition:
                    delivered = self.capture["id"] if self.capture and self.capture["delivered"] else None
                if delivered:
                    try:
                        finished = self.call("coordinator", "health", timeout=0.4)["state"] == "ready"
                    except (IPCError, OSError, KeyError, TypeError):
                        finished = True
                    if finished:
                        self.release(delivered)
            with self.condition:
                if self.capture and self.clock() >= self.capture["expires"]:
                    self.release(self.capture["id"])
            self.stop.wait(0.15)

    def close(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
        for thread in self.threads:
            thread.join(timeout=2)
