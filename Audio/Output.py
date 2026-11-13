"""Prepare Speaker's ALSA route and mixer before synthesis or animation starts."""
import os
from pathlib import Path
import re
import subprocess
import time


DEFAULT_DEVICE = "plughw:CARD=Device,DEV=0"


class Output:
    def __init__(self):
        self.device = os.environ.get("NAVI_SPEAKER_DEVICE", DEFAULT_DEVICE)
        # The USB adapter used by Navi exposes Speaker; custom routes opt in.
        self.mixer = os.environ.get("NAVI_SPEAKER_MIXER",
                                    "Speaker" if self.device == DEFAULT_DEVICE else "")
        self.volume = os.environ.get("NAVI_SPEAKER_VOLUME", "50%")
        if not re.fullmatch(r"(?:[1-9][0-9]?|100)%", self.volume):
            raise ValueError("NAVI_SPEAKER_VOLUME must be a percentage from 1% to 100%")
        match = re.fullmatch(r"(?:plug)?hw:(?:CARD=)?([A-Za-z0-9_-]+)(?:,(?:DEV=)?([0-9]+))?",
                             self.device)
        self.card = match.group(1) if match else None
        self.pcm = None
        if match:
            card_path = "card" + self.card if self.card.isdigit() else self.card
            self.pcm = Path("/proc/asound") / card_path / ("pcm" + (match.group(2) or "0") + "p/info")
        if self.mixer and self.card is None:
            raise ValueError("NAVI_SPEAKER_MIXER requires a hw/plughw playback device")
        self.ready = False
        self.last_error = None

    def status(self):
        return {"device": self.device, "mixer": self.mixer, "volume": self.volume,
                "mute_cycle": bool(self.mixer),
                "mute_cycle_on": "startup" if self.mixer else "disabled",
                "ready": self.ready and (self.pcm is None or self.pcm.is_file()),
                "error": self.last_error}

    def _amixer(self, *arguments):
        result = subprocess.run(["amixer", "-D", "hw:CARD=" + self.card, *arguments],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=2, env=dict(os.environ, LC_ALL="C"))
        if result.returncode:
            raise RuntimeError("Speaker mixer failed: " + (result.stderr or result.stdout).strip()[:300])
        return result.stdout

    @staticmethod
    def _audible(settings):
        # Inspect playback channels only; never alter microphone/capture controls.
        channels = [line for line in settings.splitlines() if "Playback " in line and "[" in line]
        if not channels or "Capabilities:" not in settings:
            return False
        for line in channels:
            volumes = re.findall(r"\[(\d+)%\]", line)
            if "[off]" in line or any(int(value) == 0 for value in volumes):
                return False
            if not volumes and "[on]" not in line:
                return False
        return True

    def prepare(self, timeout=5):
        if self.ready:
            # Speaker already initialized this output. Normal utterances only
            # check the endpoint; they do not change the mixer or wait again.
            self.check()
            return
        from Logging import logger
        deadline = time.monotonic() + timeout
        self.ready = False
        while True:
            try:
                if self.pcm is not None and not self.pcm.is_file():
                    raise RuntimeError("Speaker PCM unavailable: " + self.device)
                if self.mixer:
                    # The USB adapter can be silent while ALSA's cached switch is
                    # already "on". Repeating unmute alone may send no hardware
                    # change. Force a transition during output initialization.
                    self._amixer("sset", self.mixer, "playback", "mute")
                    time.sleep(0.15)
                    self._amixer("sset", self.mixer, "playback", self.volume, "unmute")
                    if not self._audible(self._amixer("sget", self.mixer)):
                        raise RuntimeError("Speaker mixer is still muted or at zero volume: " + self.mixer)
                self.ready = True
                self.last_error = None
                logger.info("speaker_output_ready device=%s mixer=%s volume=%s mute_cycle=%s mute_cycle_on=startup",
                            self.device, self.mixer or "unmanaged", self.volume, bool(self.mixer))
                return
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                self.last_error = str(exc) or type(exc).__name__
                if time.monotonic() >= deadline:
                    raise RuntimeError(self.last_error) from exc
                logger.warning("speaker_output_wait device=%s error=%s", self.device, self.last_error)
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))

    def check(self):
        if self.pcm is not None and not self.pcm.is_file():
            self.ready = False
            self.last_error = "Speaker PCM disappeared: " + self.device
            raise RuntimeError(self.last_error)
