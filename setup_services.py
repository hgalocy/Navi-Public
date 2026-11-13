#!/usr/bin/env python3
"""Install Navi's systemd user services on Jetson (Python 3.8+, no sudo)."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time


COMPONENTS = ("face", "listener", "speaker", "coordinator")
UNITS = ("navi.target",) + tuple("navi-" + name + ".service" for name in COMPONENTS)
ENV_OPTIONS = {"display": "DISPLAY", "xauthority": "XAUTHORITY",
               "mic_device": "NAVI_MIC_DEVICE", "speaker_device": "NAVI_SPEAKER_DEVICE",
               "speaker_mixer": "NAVI_SPEAKER_MIXER", "speaker_volume": "NAVI_SPEAKER_VOLUME",
               "wake_enabled": "NAVI_WAKE_ENABLED", "wake_threshold": "NAVI_WAKE_THRESHOLD"}
PROBE = """import importlib.util,json,os,sys
from Audio import config
print(json.dumps({'version':list(sys.version_info[:2]),
 'missing':[name for name in ('pyglet','moderngl','numpy','requests') if importlib.util.find_spec(name) is None],
 'piper':config.piper_path(), 'model':config.model_path()}))
"""


class SetupError(RuntimeError):
    pass


def say(message):
    width = max(20, min(88, shutil.get_terminal_size((80, 24)).columns - 1))
    for paragraph in str(message).splitlines() or [""]:
        for line in textwrap.wrap(paragraph, width=width, break_on_hyphens=False) or [""]:
            print(line)


def single_line(value):
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SetupError("Paths/settings must not contain control characters")
    return value


def unit_quote(value, command=False):
    # systemd syntax, not shell syntax. ExecStart also expands dollar variables.
    value = single_line(value).replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def env_quote(value):
    value = single_line(value)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_path(value):
    # systemd 245's WorkingDirectory parser takes a literal path, not a quoted word.
    value = single_line(value)
    if value != value.rstrip() or value.endswith("\\"):
        raise SetupError("Repository paths cannot end in whitespace or a backslash")
    return value.replace("%", "%%")


def check_platform():
    if not sys.platform.startswith("linux"):
        raise SetupError("Run this script on Jetson, inside the updated Navi checkout.")
    if os.geteuid() == 0:
        raise SetupError("Run as the Jetson desktop/SSH user, without sudo.")


def execute(argv, timeout=30, check=True, **kwargs):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError("Could not complete {}: {}".format(argv[0], exc)) from exc
    if check and result.returncode:
        raise SetupError("{} failed: {}".format(" ".join(argv[:3]),
                         (result.stderr or result.stdout).strip() or result.returncode))
    return result


def atomic_write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".navi-install-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Installer:
    def __init__(self, args, repository=None, home=None, run=execute, output=say):
        self.args = args
        self.repository = Path(repository or Path(__file__).parent).resolve()
        self.home = Path(home or Path.home())
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(self.home / ".config")))
        if not config_home.is_absolute():
            raise SetupError("XDG_CONFIG_HOME must be an absolute path")
        self.unit_dir = config_home / "systemd" / "user"
        # The checked-in unit definitions deliberately use this fixed location.
        self.env_file = self.home / ".config" / "navi" / "services.env"
        chosen = shutil.which(args.python) if not os.path.isabs(args.python) else args.python
        if not chosen:
            raise SetupError("Python interpreter not found: " + args.python)
        # Preserve a venv's executable path; resolving its symlink would lose the venv.
        self.python = os.path.abspath(chosen)
        listener_python = args.listener_python or self.python
        chosen_listener = shutil.which(listener_python) if not os.path.isabs(listener_python) else listener_python
        if not chosen_listener:
            raise SetupError("Listener Python interpreter not found: " + listener_python)
        self.listener_python = os.path.abspath(chosen_listener)
        self.run = run
        self.output = output
        self.previous = {}
        self.backup = None
        self.manager_environment = {}
        for option in ENV_OPTIONS:
            value = getattr(args, option)
            if value is not None and not single_line(value).strip() and option != "speaker_mixer":
                raise SetupError("--" + option.replace("_", "-") + " must not be empty")
        if args.speaker_volume is not None and not re.fullmatch(r"(?:[1-9][0-9]?|100)%", args.speaker_volume):
            raise SetupError("--speaker-volume must be a percentage from 1% to 100%")

    def systemctl(self, *args, **kwargs):
        return self.run(["systemctl", "--user", *args], **kwargs)

    def select_listener_python(self):
        if self.args.listener_python:
            return
        # Reinstalls must preserve the effective interpreter, including drop-ins.
        # systemd supplies the parsed executable path; do not parse ExecStart as shell.
        result = self.systemctl("show", "navi-listener.service", "--property=ExecStart", "--value")
        command = result.stdout.strip()
        if command:
            match = re.match(r"^\{\s*path=(.*?)\s*;\s*argv\[\]=", command)
            if not match or not os.path.isabs(match.group(1)):
                raise SetupError("Cannot determine installed Listener interpreter; "
                                 "supply --listener-python /path/to/python explicitly")
            self.listener_python = single_line(match.group(1))
        else:
            # Fresh installs may reuse the environment prepared by Listener/setup_env.py.
            candidate = self.repository / "Listener" / ".venv" / "bin" / "python"
            if candidate.is_file():
                self.listener_python = str(candidate)
        self.output("Listener Python: " + self.listener_python)

    def preflight(self):
        check_platform()
        for program in ("systemctl", "systemd-analyze", "systemd-run", "arecord", "aplay", "amixer"):
            if not shutil.which(program):
                hint = " Install alsa-utils." if program in ("arecord", "aplay", "amixer") else " Check the systemd installation."
                raise SetupError("Missing executable: " + program + "." + hint)
        for relative in ("Services/control.py", "Services/__main__.py", "Audio/config.py",
                         "Listener/service.py", "Listener/audio.py", "Listener/capture.py",
                         "Listener/wakeword.py", "Audio/service.py", "Coordinator/service.py",
                         "Face/service.py"):
            if not (self.repository / relative).is_file():
                raise SetupError("Update the Jetson checkout first; missing " + relative)
        for unit in UNITS:
            if not (self.repository / "systemd" / unit).is_file():
                raise SetupError("Missing source unit: " + unit)
        result = self.systemctl("show-environment")
        # Only retain desktop values; never print the user's full manager environment.
        self.manager_environment = dict(line.split("=", 1) for line in result.stdout.splitlines()
                                        if line.startswith(("DISPLAY=", "XAUTHORITY=")))
        self.select_listener_python()
        result = self.run([self.python, "-B", "-c", PROBE], cwd=str(self.repository))
        try:
            report = json.loads(result.stdout)
            if tuple(report["version"]) < (3, 8):
                raise SetupError("The selected Python must be version 3.8 or newer")
            if report["missing"]:
                raise SetupError("Missing Python packages: {}. Install them with this interpreter: {} -m pip install {}".format(
                    ", ".join(report["missing"]), self.python, " ".join(report["missing"])))
            if not os.access(report["piper"], os.X_OK):
                raise SetupError("Piper executable not found/executable: " + report["piper"])
            for path in (report["model"], report["model"] + ".json"):
                if not Path(path).is_file():
                    raise SetupError("Piper voice file missing: " + path)
        except (ValueError, KeyError, TypeError) as exc:
            raise SetupError("Invalid prerequisite check output from " + self.python) from exc
        self.output("Prerequisites OK: Python, client packages, ALSA tools, Piper, user systemd.")

    def environment(self):
        if self.env_file.is_symlink():
            raise SetupError("Refusing to replace a symlink: " + str(self.env_file))
        current = self.env_file.read_text(encoding="utf-8") if self.env_file.exists() else (
            "# Navi service settings. Existing values are preserved on reinstall.\n"
            "# Defaults: NAVI_MIC_DEVICE=plughw:1,0, NAVI_SPEAKER_DEVICE=plughw:CARD=Device,DEV=0\n")
        additions = {}
        for key in ("DISPLAY", "XAUTHORITY", "PATH"):
            if os.environ.get(key) and not re.search(r"^\s*" + key + r"=", current, re.M):
                additions[key] = os.environ[key]
        for option, key in ENV_OPTIONS.items():
            value = getattr(self.args, option)
            if value is not None:
                previous = re.findall(r"^\s*" + key + r"=(.*)$", current, re.M)
                if not previous or previous[-1].strip() != env_quote(value):
                    additions[key] = value
        if additions:
            block = "# Settings captured by setup_services.py\n" + "".join(
                key + "=" + env_quote(value) + "\n" for key, value in sorted(additions.items()))
            # Append explicit overrides; do not rewrite arbitrary EnvironmentFile syntax.
            if not current.endswith(block):
                current = current.rstrip() + "\n\n" + block
        return current

    def render(self):
        files = {}
        for unit in UNITS:
            text = (self.repository / "systemd" / unit).read_text(encoding="utf-8")
            if unit.endswith(".service"):
                component = unit[len("navi-"):-len(".service")]
                replacements = {
                    "WorkingDirectory=%h/Navi": "WorkingDirectory=" + unit_path(self.repository),
                    "ExecStart=/usr/bin/python3 -m Services " + component:
                        "ExecStart=" + unit_quote(self.listener_python if component == "listener" else self.python,
                                                 command=True) + " -m Services " + component,
                }
                for old, new in replacements.items():
                    if text.count(old) != 1:
                        raise SetupError("Unexpected source definition: " + unit)
                    text = text.replace(old, new)
            files[self.unit_dir / unit] = (text.encode("utf-8"), 0o644)
        files[self.env_file] = (self.environment().encode("utf-8"), 0o600)
        return files

    def validate(self, files):
        with tempfile.TemporaryDirectory(prefix="navi-unit-check-") as directory:
            staged = []
            for unit in UNITS:
                path = Path(directory) / unit
                path.write_bytes(files[self.unit_dir / unit][0])
                staged.append(str(path))
            self.run(["systemd-analyze", "--user", "verify", *staged])
            environment = Path(directory) / "services.env"
            environment.write_bytes(files[self.env_file][0])
            # Let systemd parse the actual settings, including existing overrides.
            # Warm ONNX in the Listener interpreter before modifying any installation.
            self.run(["systemd-run", "--user", "--pipe", "--wait", "--collect", "--quiet",
                      "--working-directory=" + str(self.repository),
                      "--property=EnvironmentFile=" + str(environment),
                      "--property=RuntimeMaxSec=45", "--", self.listener_python,
                      "-B", "-m", "Listener.wakeword"], timeout=55)
        for path in files:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise SetupError("Refusing to replace a symlink/non-file: " + str(path))
        self.output("Unit definitions validated.")

    def install(self, files):
        changed = {path: value for path, value in files.items()
                   if not path.exists() or path.read_bytes() != value[0]}
        for path in changed:
            self.previous[path] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
        if any(value is not None for value in self.previous.values()):
            directory = self.home / ".config" / "navi" / "backups"
            directory.mkdir(parents=True, exist_ok=True)
            self.backup = Path(tempfile.mkdtemp(prefix="services-", dir=str(directory)))
            for path, previous in self.previous.items():
                if previous is not None:
                    atomic_write(self.backup / path.name, *previous)
            self.output("Backups: " + str(self.backup))
        try:
            for path, value in changed.items():
                atomic_write(path, *value)
            self.systemctl("daemon-reload")
            for unit in UNITS:
                result = self.systemctl("show", unit, "--property=LoadState", "--value")
                if result.stdout.strip() != "loaded":
                    raise SetupError("Installed unit is not loadable: " + unit + "; check existing drop-ins")
        except (SetupError, OSError, KeyboardInterrupt):
            self.restore()
            raise
        self.output("Installed five user units." if changed else "User units/settings already up to date.")

    def restore(self):
        for path, previous in self.previous.items():
            if previous is None:
                if path.exists():
                    path.unlink()
            else:
                atomic_write(path, *previous)
        self.systemctl("daemon-reload", check=False)
        self.output("Installation failed; previous files restored.")

    def disable_legacy(self):
        result = self.systemctl("show", "navi-client.service", "--property=LoadState", "--value", check=False)
        load = result.stdout.strip()
        if load == "not-found":
            return
        if result.returncode or load not in ("loaded", "masked"):
            raise SetupError("Could not inspect navi-client.service; no startup attempted")
        self.systemctl("disable", "--now", "navi-client.service")
        self.output("Legacy navi-client.service stopped and disabled.")

    def health(self):
        # Let systemd read services.env itself, including custom NAVI_RUNTIME_DIR.
        # This short-lived probe is collected automatically, not a new supervisor.
        result = self.run(["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
                           "--property=RuntimeMaxSec=10", "--property=TimeoutStopSec=2",
                           "--working-directory=" + str(self.repository),
                           "--property=EnvironmentFile=" + str(self.env_file),
                           self.python, "-m", "Services.control", "status"], timeout=15, check=False)
        try:
            report = json.loads(result.stdout)
            rows = report["components"]
            if (report.get("v") != 1 or not isinstance(rows, list) or len(rows) != 4 or
                    any(not isinstance(row, dict) for row in rows) or
                    {row["component"] for row in rows} != set(COMPONENTS)):
                raise ValueError("incomplete health report")
        except (ValueError, KeyError, TypeError) as exc:
            raise SetupError("Health CLI failed: " + ((result.stderr or result.stdout).strip()[:400])) from exc
        return rows, result.returncode == 0 and all(row.get("state") in ("ready", "busy") for row in rows)

    def start(self):
        self.output("Starting/restarting the four components...")
        self.systemctl("restart", *UNITS, timeout=60)
        deadline = time.monotonic() + 15
        while True:
            rows, healthy = self.health()
            if healthy or time.monotonic() >= deadline:
                break
            time.sleep(1)
        for row in rows:
            self.output("{:<12} {}".format(row["component"], "responsive" if row.get("state") == "ready" else row.get("state", "unknown")))
        if not healthy:
            raise SetupError("Some components did not respond; setup did not confirm readiness")
        if self.args.enable:
            self.systemctl("enable", "navi.target")
            self.output("Automatic startup enabled for this user's systemd session.")

    def perform(self):
        self.preflight()
        files = self.render()
        if self.args.start and not self.manager_environment.get("DISPLAY"):
            configured = re.findall(r"^\s*DISPLAY=(.*)$", files[self.env_file][0].decode("utf-8"), re.M)
            if not configured or configured[-1].strip() in ("", '""', "''"):
                raise SetupError("No desktop DISPLAY found. Run from the Jetson desktop terminal, "
                                 "or supply --display and --xauthority for that desktop session.")
        self.validate(files)
        self.output("Repository: " + str(self.repository))
        self.output("Python: " + self.python)
        self.output("Listener Python: " + self.listener_python)
        self.output("Settings: " + str(self.env_file))
        if self.args.check:
            self.output("Checks passed. No installed files or running services changed.")
            return
        self.install(files)
        self.disable_legacy()
        if self.args.start:
            self.start()
            self.output("Navi is running; all four components respond.")
        else:
            self.output("Installed. In NaviRemoteControl, run: start client")
        self.output('Remote-control [client]: manager="systemd-user", name="navi.target"')
        self.output("Use the repository and Python paths shown above in that config.")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--check", action="store_true", help="Validate prerequisites and staged units without installing")
    result.add_argument("--start", action="store_true", help="Start/restart all four components and verify health")
    result.add_argument("--enable", action="store_true", help="Enable startup at user login after --start succeeds")
    result.add_argument("--python", default=sys.executable, help="Python interpreter used by Navi (default: this interpreter)")
    result.add_argument("--listener-python", help="Listener interpreter with openWakeWord 0.6.0 "
                        "(default: installed Listener, then Listener venv if present, then --python)")
    for option in ENV_OPTIONS:
        result.add_argument("--" + option.replace("_", "-"), help="Override the corresponding setting in services.env")
    return result


def main(argv=None):
    arguments = parser()
    args = arguments.parse_args(argv)
    if args.check and (args.start or args.enable):
        arguments.error("--check cannot be combined with --start or --enable")
    if args.enable and not args.start:
        arguments.error("--enable requires --start so health is verified first")
    try:
        Installer(args).perform()
        return 0
    except (SetupError, OSError) as exc:
        say("Setup failed: " + str(exc))
        say("No automatic retry was attempted. Fix the reported cause and rerun.")
        say("For installed services, use 'diagnose client' in NaviRemoteControl,")
        say("or: journalctl --user -u 'navi-*.service' -n 40 --no-pager")
        return 1
    except KeyboardInterrupt:
        say("Interrupted. Check status before retrying; systemd actions may still complete.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
