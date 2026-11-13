"""Speaker configuration; no mutable cross-component state or hardware access."""
from pathlib import Path
from shutil import which


def piper_path():
    installed = Path.home() / "piper" / "piper"
    return str(installed) if installed.exists() else which("piper") or str(installed)


def model_path():
    return str(Path.home() / "piper" / "voices" / "en_US" / "amy_low" / "en_US-amy-low.onnx")


SAMPLE_RATE = 22050
BYTES_PER_FRAME = 2
CHUNK_BYTES = 256 * BYTES_PER_FRAME
ENVELOPE_ATTACK = 0.65
ENVELOPE_RELEASE = 0.8
WRITE_AHEAD_SECONDS = 0.08
