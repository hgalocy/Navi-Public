"""One continuous, tracked ALSA recorder, owned exclusively by Listener."""
from collections import deque
import os
import selectors
import subprocess
import threading
import time

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE


class Stream:
    """Exact 80ms raw PCM frames; bounded stalls, stderr draining and cleanup."""
    def __init__(self, process_callback):
        self.track = process_callback
        self.process = self.selector = self.stderr_worker = None
        self.errors = deque(maxlen=8)

    def __enter__(self):
        from Logging import logger
        command = ["arecord", "-D", os.environ.get("NAVI_MIC_DEVICE", "plughw:1,0"),
                   "-t", "raw", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1",
                   "--buffer-time=160000", "-"]
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self.track(self.process)
            self.stderr_worker = threading.Thread(target=self._stderr, daemon=True)
            self.stderr_worker.start()
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            logger.info("microphone_open pid=%s device=%s", self.process.pid, command[2])
            return self
        except BaseException:
            self.close()
            raise

    def _stderr(self):
        from Logging import logger
        for raw in iter(self.process.stderr.readline, b""):
            line = raw.decode(errors="replace").strip()
            self.errors.append(line)
            logger.info("arecord_stderr %s", line)

    def read_frame(self, stop):
        data = bytearray()
        last_data = time.monotonic()
        while len(data) < FRAME_BYTES and not stop.is_set():
            if time.monotonic() - last_data > 5:
                raise RuntimeError("Microphone stalled; arecord stderr: {}".format(list(self.errors)))
            if not self.selector.select(timeout=0.1):
                continue
            chunk = os.read(self.process.stdout.fileno(), FRAME_BYTES - len(data))
            if not chunk:
                raise RuntimeError("arecord ended ({}); stderr: {}".format(self.process.poll(), list(self.errors)))
            data.extend(chunk)
            last_data = time.monotonic()
        return bytes(data) if len(data) == FRAME_BYTES else None

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
            if self.stderr_worker:
                self.stderr_worker.join(timeout=2)
            for pipe in (self.process.stdout, self.process.stderr):
                pipe.close()
        if self.selector:
            self.selector.close()

    def __exit__(self, *unused):
        self.close()
