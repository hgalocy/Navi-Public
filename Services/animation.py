"""Loss-tolerant mouth envelopes. Never block playback waiting for a Face."""
from collections import deque
import json
import os
import socket
import time
import uuid

from .ipc import runtime_dir


class EnvelopeSender:
    def __init__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.path = str(runtime_dir() / "animation.sock")
        self.session = uuid.uuid4().hex

    def send(self, at, level, done):
        message = {"v": 1, "session": self.session, "at": at,
                   "level": level, "done": done}
        try:
            self.sock.sendto(json.dumps(message).encode(), self.path)
        except OSError:
            pass  # Missing/restarting/slow face: audio continues.

    def close(self):
        self.sock.close()


class EnvelopeReceiver:
    def __init__(self):
        self.path = str(runtime_dir() / "animation.sock")
        if os.path.lexists(self.path):
            os.unlink(self.path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.setblocking(False)
        self.samples = deque(maxlen=256)
        self.session = None
        self.last_received = 0
        self.level = 0
        self.done = True

    def sample(self):
        now = time.monotonic()
        for _ in range(256):
            try:
                raw = self.sock.recv(2048)
            except BlockingIOError:
                break
            try:
                msg = json.loads(raw)
                if msg["v"] != 1 or not isinstance(msg["session"], str):
                    continue
                at, level, done = float(msg["at"]), float(msg["level"]), msg["done"]
                if not (0 <= level <= 1 and 0 <= at <= now + 1 and type(done) is bool):
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            if self.session != msg["session"]:
                self.samples.clear()
                self.session = msg["session"]
            self.last_received = now
            self.done = done
            if done:
                self.samples.clear()
                self.level = 0
            else:
                self.samples.append((at, level))
        while self.samples and self.samples[0][0] <= now - 0.06:
            self.level = self.samples.popleft()[1]
        active = not self.done and now - self.last_received < 0.5
        return (self.level if active else 0), active

    def close(self):
        self.sock.close()
        os.unlink(self.path)
