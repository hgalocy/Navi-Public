"""Real local IPC/process tests plus protocol and supported Python checks."""
import ast
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from Services import ipc

ROOT = Path(__file__).resolve().parents[1]


def eventually(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (ipc.IPCError, OSError):
            pass
        time.sleep(0.02)
    raise AssertionError("Condition did not become true")


class ProtocolTests(unittest.TestCase):
    def test_systemd_entrypoint_routes_to_component_packages_without_changing_names(self):
        import importlib
        from Services.__main__ import main
        for name, path in (("listener", "Listener.service"), ("speaker", "Audio.service"),
                           ("coordinator", "Coordinator.service"), ("face", "Face.service")):
            module = importlib.import_module(path)
            with self.subTest(component=name), \
                    patch.dict(os.environ, {"INVOCATION_ID": "test", "NOTIFY_SOCKET": "unused"}), \
                    patch("sys.argv", ["Services", name]), patch.object(module, "run") as run:
                main()
                run.assert_called_once_with()
                self.assertEqual(os.environ["NAVI_COMPONENT"], name)

    def test_frame_limits_version_and_shape(self):
        for frame in (b'[]\n', b'{"v":2}\n', b'{"v":true}\n', b'{"v":1}', b'bad\n'):
            with self.subTest(frame=frame), self.assertRaises(ipc.IPCError):
                ipc.decode(frame)
        with self.assertRaises(ipc.IPCError):
            ipc.encode({"v": 1, "text": "x" * ipc.MAX_MESSAGE})
        self.assertEqual(ipc.decode(ipc.encode({"v": 1, "text": "hello"}))["text"], "hello")

    def test_sources_parse_as_python38(self):
        for directory in ("Services", "Listener", "Coordinator", "Audio", "Face", "Logging", "Network", "tests"):
            for path in (ROOT / directory).glob("*.py"):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 8))

    def test_manual_runtime_launch_points_to_systemd_before_importing_hardware(self):
        environment = {k: v for k, v in os.environ.items() if k not in ("INVOCATION_ID", "NOTIFY_SOCKET")}
        result = subprocess.run([sys.executable, "-B", "-m", "Services", "face"],
                                cwd=ROOT, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("systemctl --user start navi.target", result.stderr)

    def test_lifecycle_cli_only_runs_allowlisted_systemd_unit(self):
        from Services.control import main
        with patch("Services.control.subprocess.run") as execute:
            execute.return_value.returncode = 0
            self.assertEqual(main(["restart", "listener"]), 0)
        execute.assert_called_once_with(["systemctl", "--user", "restart", "navi-listener.service"], timeout=35)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux Unix sockets/flock required")
class ServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="navi-ipc-test-")
        self.addCleanup(self.directory.cleanup)
        self.runtime = str(Path(self.directory.name) / "run")
        self.environment = patch.dict(os.environ, {
            "NAVI_RUNTIME_DIR": self.runtime, "NAVI_LOG_DIR": str(Path(self.directory.name) / "logs"),
            "WATCHDOG_USEC": "300000", "PYTHONPATH": str(ROOT),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.processes = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def launch(self, name, **environment):
        process = subprocess.Popen([sys.executable, str(ROOT / "tests/service_fixture.py"), name],
                                   cwd=ROOT, env=dict(os.environ, **environment),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return process

    def ready(self, name):
        return eventually(lambda: ipc.call(name, "health", timeout=0.2))

    def test_roundtrip_reconnect_and_independent_lifecycles(self):
        listener = self.launch("listener")
        listener_pid = self.ready("listener")["pid"]
        face = self.launch("face")
        self.ready("face")
        # A Face death while Listener is working cannot cancel that operation.
        ipc.call("listener", "hold", {"seconds": 0.4})
        face.kill()
        face.wait(timeout=3)
        self.launch("face")
        self.ready("face")
        self.assertEqual(self.ready("listener")["pid"], listener_pid)
        eventually(lambda: ipc.call("listener", "health")["last_job"])
        self.assertEqual(ipc.call("listener", "health")["last_job"]["state"], "completed")
        face_pid = self.ready("face")["pid"]
        listener.kill()
        listener.wait(timeout=3)
        self.launch("listener")
        self.assertNotEqual(self.ready("listener")["pid"], listener_pid)
        self.assertEqual(self.ready("face")["pid"], face_pid)
        context = {"interaction_id": "abc"}
        self.assertEqual(ipc.call("listener", "echo", {"hello": "world"}, context),
                         {"args": {"hello": "world"}, "context": context})

    def test_busy_rejection_and_failure_are_observable(self):
        self.launch("listener")
        self.ready("listener")
        ipc.call("listener", "hold")
        self.assertEqual(ipc.call("listener", "health")["state"], "busy")
        with self.assertRaisesRegex(ipc.IPCError, "busy"):
            ipc.call("listener", "echo")
        eventually(lambda: ipc.call("listener", "health")["state"] == "ready")
        ipc.call("listener", "fail")
        state = eventually(lambda: (lambda s: s if s["last_error"] else None)(ipc.call("listener", "health")))
        self.assertIn("fixture failure", state["last_error"])
        self.assertEqual(state["last_job"]["state"], "failed")

    def test_stuck_worker_exits_and_releases_microphone_lease(self):
        process = self.launch("listener", FIXTURE_DEADLINE="0.2")
        self.ready("listener")
        ipc.call("listener", "hold", {"seconds": 30})
        self.assertNotEqual(process.wait(timeout=6), 0)
        self.launch("listener")
        self.ready("listener")

    def test_duplicate_microphone_owner_is_refused(self):
        self.launch("listener")
        first = self.ready("listener")
        duplicate = self.launch("listener")
        self.assertNotEqual(duplicate.wait(timeout=3), 0)
        self.assertEqual(self.ready("listener")["pid"], first["pid"])

    def test_malformed_frame_does_not_kill_service(self):
        self.launch("listener")
        self.ready("listener")
        for frame in (b'{"v":2}\n', b'x' * (ipc.MAX_MESSAGE + 1) + b'\n'):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(3)
                sock.connect(ipc.socket_path("listener"))
                sock.sendall(frame)
                self.assertFalse(ipc.decode(sock.recv(4096))["ok"])
        self.ready("listener")

    def test_graceful_stop_reaps_owned_subprocess(self):
        process = self.launch("speaker")
        self.ready("speaker")
        child_pid = ipc.call("speaker", "child")["child_pid"]
        process.terminate()
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertFalse(Path("/proc/{}".format(child_pid)).exists())
        self.assertFalse(Path(ipc.socket_path("speaker")).exists())

    def test_watchdog_datagrams_stop_when_event_loop_is_frozen(self):
        address = str(Path(self.directory.name) / "notify.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
            receiver.bind(address)
            receiver.settimeout(2)
            process = self.launch("listener", NOTIFY_SOCKET=address)
            self.assertIn(b"READY=1", receiver.recv(4096))
            self.assertEqual(receiver.recv(4096), b"WATCHDOG=1")
            os.kill(process.pid, signal.SIGSTOP)
            try:
                receiver.setblocking(False)
                while True:
                    try:
                        receiver.recv(4096)
                    except BlockingIOError:
                        break
                receiver.settimeout(0.3)
                with self.assertRaises(socket.timeout):
                    receiver.recv(4096)
            finally:
                os.kill(process.pid, signal.SIGCONT)

    def test_animation_does_not_require_a_face_and_recovers_after_restart(self):
        from Services.animation import EnvelopeReceiver, EnvelopeSender
        sender = EnvelopeSender()
        self.addCleanup(sender.close)
        sender.send(time.monotonic(), 0.7, False)  # No Face exists.
        receiver = EnvelopeReceiver()
        try:
            sender.send(time.monotonic() - 0.1, 0.7, False)
            self.assertEqual(receiver.sample(), (0.7, True))
        finally:
            receiver.close()
        sender.send(time.monotonic(), 0.7, False)
        receiver = EnvelopeReceiver()
        try:
            sender.send(time.monotonic() - 0.1, 0.4, False)
            self.assertEqual(receiver.sample(), (0.4, True))
            # Speaker crash: no completion packet is needed to return to idle.
            with patch("Services.animation.time.monotonic", return_value=time.monotonic() + 0.6):
                self.assertEqual(receiver.sample(), (0, False))
            sender.send(0, 0, True)
            self.assertEqual(receiver.sample(), (0, False))
        finally:
            receiver.close()
