#!/usr/bin/env python3
"""Prepare Listener's pinned Jetson environment and verified ONNX feature assets."""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import venv

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".venv"
MODEL_SHA256 = "68abe08532063dca36a16d7630d6334001ebe8765e08e7b4b8cb2d7d061b3b3e"
FEATURES = {
    "melspectrogram.onnx": "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
    "embedding_model.onnx": "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
}
RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"


def check_host():
    if (sys.version_info[:2] != (3, 8) or platform.system() != "Linux" or
            platform.machine().lower() not in ("aarch64", "arm64")):
        raise RuntimeError("Run on Jetson with Python 3.8; this setup never installs globally")
    if not shutil.which("arecord"):
        raise RuntimeError("Install the OS alsa-utils package first")


def verify(path, expected):
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Missing or invalid Listener asset: " + str(path))


def prepare_assets():
    directory = ROOT / "models"
    verify(directory / "hey_navi_v3.onnx", MODEL_SHA256)
    for name, digest in FEATURES.items():
        target = directory / name
        if target.exists():
            verify(target, digest)
            continue
        # Migrate verified feature assets without moving an existing environment.
        candidates = (ROOT.parent / "wakeword_poc" / "models" / name,)
        cached = next((p for p in candidates if p.is_file() and
                       hashlib.sha256(p.read_bytes()).hexdigest() == digest), None)
        descriptor, temporary = tempfile.mkstemp(prefix=".feature-", dir=str(directory))
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as output:
                if cached:
                    with cached.open("rb") as source:
                        shutil.copyfileobj(source, output)
                else:
                    with urllib.request.urlopen(RELEASE + name, timeout=30) as source:
                        shutil.copyfileobj(source, output)
            verify(temporary, digest)
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()


def install_environment():
    if ENV.exists() and not (ENV / "pyvenv.cfg").is_file():
        raise RuntimeError("Refusing to reuse a non-venv directory: " + str(ENV))
    if not ENV.exists():
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(str(ENV))
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("PIP_") and key not in ("PYTHONPATH", "PYTHONHOME")}
    environment.update(PYTHONNOUSERSITE="1", PIP_REQUIRE_VIRTUALENV="true",
                       PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1")
    python = str(ENV / "bin" / "python")
    probe = ("import sys; from pathlib import Path; "
             "assert sys.version_info[:2] == (3,8); "
             "assert sys.prefix != sys.base_prefix; "
             "assert 'include-system-site-packages = false' in "
             "(Path(sys.prefix)/'pyvenv.cfg').read_text().lower()")
    subprocess.run([python, "-I", "-c", probe], check=True, env=environment, timeout=10)
    pip = [python, "-I", "-m", "pip"]
    options = ["--index-url", "https://pypi.org/simple", "--only-binary=:all:"]
    subprocess.run(pip + ["install"] + options + ["pip==25.0.1", "setuptools==75.3.2", "wheel==0.45.1"],
                   check=True, env=environment, timeout=300)
    subprocess.run(pip + ["install"] + options + ["-r", str(ROOT / "requirements.txt")],
                   check=True, env=environment, timeout=300)
    subprocess.run(pip + ["check"], check=True, env=environment, timeout=30)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate existing assets/environment without installing")
    args = parser.parse_args(argv)
    try:
        check_host()
        if args.check:
            verify(ROOT / "models" / "hey_navi_v3.onnx", MODEL_SHA256)
            for name, digest in FEATURES.items():
                verify(ROOT / "models" / name, digest)
        else:
            prepare_assets()
            install_environment()
        subprocess.run([str(ENV / "bin" / "python"), "-B", "-m", "Listener.wakeword"],
                       cwd=str(ROOT.parent), check=True, timeout=45)
        print("Listener ready: " + str(ENV / "bin" / "python"))
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print("Listener setup failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
