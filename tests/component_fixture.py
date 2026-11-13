"""Actual service handlers with fake devices/server and a headless Face sink."""
import os
import io
import sys
import time
import wave

os.environ["NAVI_COMPONENT"] = sys.argv[1]
from Coordinator import service as coordinator
from Listener import service as listener
from Audio import service as speaker
from Services import runtime
from Services.animation import EnvelopeReceiver
from Services.ipc import IPCError, runtime_dir
from Listener.audio import ContinuousAudio


class Response:
    def raise_for_status(self):
        pass

    def json(self):
        return {"user_text": "Question?", "text": "Answer!"}


class Network:
    def send_audio(self, files, **metadata):
        data = files["file"].read()
        with wave.open(io.BytesIO(data), "rb") as wav:
            assert wav.getframerate() == 16000 and wav.getnchannels() == 1
            assert wav.getnframes() >= 63000
        assert metadata["source"] == "microphone"
        assert metadata["trigger"] in ("typed_listen", "wake_word")
        with (runtime_dir() / "uploads").open("a") as observed:
            observed.write(metadata["trigger"] + "\n")
        if metadata["trigger"] == "wake_word":
            (runtime_dir() / "wake-request.wav").write_bytes(data)
        return Response()


class FakeStream:
    def __init__(self, track):
        self.counter = 10
        self.next_frame = time.monotonic()

    def __enter__(self):
        with (runtime_dir() / "microphone-opens").open("a") as observed:
            observed.write(str(os.getpid()) + "\n")
        return self

    def __exit__(self, *unused):
        pass

    def read_frame(self, stop):
        # Model a continuously clocked device; scheduler delays accumulate PCM
        # in a real pipe rather than slowing down the microphone sample clock.
        self.next_frame += 0.08
        if stop.wait(max(0, self.next_frame - time.monotonic())):
            return None
        marker = runtime_dir() / "wake-trigger"
        if marker.exists():
            marker.unlink()
            value = 1
        else:
            value = self.counter
            self.counter = self.counter + 1 if self.counter < 30000 else 10
        return value.to_bytes(2, "little") * 1280


class FakeDetector:
    def reset(self):
        pass

    def score(self, raw):
        return 1.0 if raw[:2] == b"\x01\0" else 0.0


def playback(text, *, on_envelope, process_callback):
    for _ in range(8):
        on_envelope(time.monotonic() - 0.1, 0.7, False)
        time.sleep(0.05)
    on_envelope(0, 0, True)
    return True


def headless_face():
    def handler(op, args, context):
        raise IPCError("Face only supports health")

    with runtime.Lease("face-renderer"):
        receiver = EnvelopeReceiver()

        def tick():
            level, active = receiver.sample()
            if active:
                temporary = runtime_dir() / "observed-envelope.tmp"
                temporary.write_text(str(level))
                temporary.replace(runtime_dir() / "observed-envelope")
        try:
            runtime.run(runtime.Service("face", handler, tick=tick))
        finally:
            receiver.close()


if __name__ == "__main__":
    name = sys.argv[1]
    if name == "face":
        headless_face()
    else:
        module = {"listener": listener, "speaker": speaker, "coordinator": coordinator}[name]
        factory = module.create_service
        if name == "listener":
            module.create_service = lambda: factory(audio=ContinuousAudio(FakeDetector(), stream_factory=FakeStream))
        elif name == "speaker":
            module.create_service = lambda sender: factory(sender, playback=playback)
        else:
            module.create_service = lambda: factory(network=Network())
        module.run()
