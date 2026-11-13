"""Opt-in systemd 245 smoke test with isolated transient units and fake hardware.

Run on Linux: python3 tests/systemd_smoke.py
Does not install units, enable boot startup, open devices, or contact NaviServer.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from Services.ipc import IPCError, call


def command(argv):
    return subprocess.run(argv, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=20).stdout.strip()


def eventually(check, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            result = check()
            if result:
                return result
        except (IPCError, subprocess.CalledProcessError, OSError):
            pass
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for systemd recovery")


def main():
    units = []
    report = {}
    with tempfile.TemporaryDirectory(prefix="navi-systemd-test-") as directory:
        os.environ["NAVI_RUNTIME_DIR"] = directory + "/run"
        try:
            for name in ("listener", "face", "speaker"):
                unit = "navi-foundation-test-{}-{}.service".format(name, uuid.uuid4().hex[:8])
                units.append(unit)
                command(["systemd-run", "--user", "--unit=" + unit,
                         "--property=Type=notify", "--property=Restart=on-failure",
                         "--property=RestartSec=0.3", "--property=WatchdogSec=1",
                         "--property=TimeoutAbortSec=1", "--property=TimeoutStopSec=5",
                         "--property=KillMode=control-group", "--working-directory=" + str(ROOT),
                         "--setenv=NAVI_RUNTIME_DIR=" + os.environ["NAVI_RUNTIME_DIR"],
                         "--setenv=NAVI_LOG_DIR=" + directory + "/logs",
                         "--setenv=PYTHONPATH=" + str(ROOT), "--setenv=NAVI_COMPONENT=" + name,
                         sys.executable, str(ROOT / "tests/service_fixture.py"), name])
                report[name + "_initial_pid"] = eventually(lambda: call(name, "health"))["pid"]
            listener_pid = report["listener_initial_pid"]
            call("listener", "hold", {"seconds": 1})
            command(["systemctl", "--user", "kill", "--kill-who=main", "--signal=SIGKILL", units[1]])
            new_face = eventually(lambda: (lambda s: s if s["pid"] != report["face_initial_pid"] else None)(call("face", "health")))
            eventually(lambda: call("listener", "health")["last_job"])
            assert call("listener", "health")["pid"] == listener_pid
            assert call("listener", "health")["last_job"]["state"] == "completed"
            report["face_crash_restarted_without_interrupting_listener"] = True
            command(["systemctl", "--user", "kill", "--kill-who=main", "--signal=SIGSTOP", units[0]])
            new_listener = eventually(lambda: (lambda s: s if s["pid"] != listener_pid else None)(call("listener", "health", timeout=0.2)))
            assert call("face", "health")["pid"] == new_face["pid"]
            report["listener_watchdog_restarted_without_restarting_face"] = True
            report["listener_restarts"] = int(command(["systemctl", "--user", "show", units[0], "--property=NRestarts", "--value"]))
            report["listener_recovered_pid"] = new_listener["pid"]
            child_pid = call("speaker", "child")["child_pid"]
            command(["systemctl", "--user", "stop", units[2]])
            assert not Path("/proc/{}".format(child_pid)).exists()
            report["speaker_stop_reaped_child"] = True
            report["passed"] = True
            print(json.dumps(report, indent=2))
        finally:
            for unit in units:
                subprocess.run(["systemctl", "--user", "stop", unit], timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["systemctl", "--user", "reset-failed", unit], timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
