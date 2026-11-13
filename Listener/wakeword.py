"""Listener-owned ONNX adapter and production wake-word assets."""
import importlib.metadata
import math
import os
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "models"


def settings():
    enabled = os.environ.get("NAVI_WAKE_ENABLED", "1")
    if enabled not in ("0", "1"):
        raise ValueError("NAVI_WAKE_ENABLED must be 0 or 1")
    threshold = float(os.environ.get("NAVI_WAKE_THRESHOLD", "0.95"))
    if not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValueError("NAVI_WAKE_THRESHOLD must be finite and in (0, 1]")
    return enabled == "1", threshold


class Detector:
    def __init__(self):
        import numpy as np
        from openwakeword.model import Model
        if importlib.metadata.version("openwakeword") != "0.6.0":
            raise RuntimeError("Listener requires openwakeword==0.6.0")
        paths = [ASSETS / name for name in
                 ("hey_navi_v3.onnx", "melspectrogram.onnx", "embedding_model.onnx")]
        for path in paths:
            if not path.is_file():
                raise RuntimeError("Missing wake asset {}; see docs/setup.md".format(path))
        self.np = np
        self.model = Model(wakeword_models=[str(paths[0])], inference_framework="onnx",
                           melspec_model_path=str(paths[1]), embedding_model_path=str(paths[2]),
                           ncpu=1, device="cpu", vad_threshold=0, enable_speex_noise_suppression=False)
        for _ in range(20):
            self.model.predict(np.zeros(1280, dtype=np.int16))
        self.reset()

    def score(self, raw):
        return float(self.model.predict(self.np.frombuffer(raw, dtype="<i2"))["hey_navi_v3"])

    def reset(self):
        self.model.reset()


def check():
    """Installer/manual check: load and warm without opening the microphone."""
    enabled, _ = settings()
    if enabled:
        Detector()


if __name__ == "__main__":
    check()
    print("Wake-word configuration/model check passed (microphone unopened)")
