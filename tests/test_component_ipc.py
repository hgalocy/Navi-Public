"""Exercise actual service conversation handlers across real process boundaries."""
import os
import io
from pathlib import Path
import subprocess
import sys
import unittest
import wave

from Services import ipc
import test_services
from test_services import ROOT, eventually


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux Unix sockets/flock required")
class ComponentIPCTests(unittest.TestCase):
    setUp = test_services.ServiceIntegrationTests.setUp
    cleanup_processes = test_services.ServiceIntegrationTests.cleanup_processes
    ready = test_services.ServiceIntegrationTests.ready

    def launch(self, name):
        process = subprocess.Popen([sys.executable, str(ROOT / "tests/component_fixture.py"), name],
                                   cwd=ROOT, env=os.environ.copy(),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return process

    def start_all(self):
        processes = {name: self.launch(name) for name in ipc.COMPONENTS}
        for name in ipc.COMPONENTS:
            self.ready(name)
        return processes

    def complete_command(self, text):
        accepted = ipc.call("coordinator", "command", {"text": text})
        self.assertTrue(accepted["accepted"])
        status = eventually(lambda: (lambda s: s if s["last_job"] and
                            s["last_job"]["id"] == accepted["job_id"] else None)(
                                ipc.call("coordinator", "health")), timeout=8)
        self.assertEqual(status["last_job"]["state"], "completed", status)
        return accepted["job_id"]

    def test_typed_and_voice_routes_deliver_envelopes_and_correlated_logs(self):
        self.start_all()
        self.complete_command("Hello")
        self.assertIsNone(ipc.call("listener", "health")["last_job"])
        self.complete_command("listen")
        self.assertEqual(ipc.call("listener", "health")["last_job"]["state"], "completed")
        self.assertEqual((Path(self.runtime) / "observed-envelope").read_text(), "0.7")
        logs = Path(os.environ["NAVI_LOG_DIR"])
        user = (logs / "coordinator/navi_conversation.log").read_text()
        reply = (logs / "speaker/navi_conversation.log").read_text()
        diagnostics = (logs / "coordinator/navi.log").read_text()
        self.assertIn('User: "Question?"', user, diagnostics)
        self.assertIn('Navi: "Answer!"', reply)
        import re
        self.assertEqual(re.findall(r"interaction=([a-f0-9]+)", user),
                         re.findall(r"interaction=([a-f0-9]+)", reply))

    def test_independent_restart_during_voice_and_reconnect(self):
        processes = self.start_all()
        listener_pid = self.ready("listener")["pid"]
        accepted = ipc.call("coordinator", "command", {"text": "listen"})
        eventually(lambda: ipc.call("listener", "health")["state"] == "busy")
        processes["face"].kill()
        processes["face"].wait(timeout=3)
        self.launch("face")
        face_pid = self.ready("face")["pid"]
        eventually(lambda: ipc.call("coordinator", "health")["last_job"], timeout=8)
        self.assertEqual(ipc.call("coordinator", "health")["last_job"],
                         {"id": accepted["job_id"], "state": "completed"})
        self.assertEqual(self.ready("listener")["pid"], listener_pid)
        processes["listener"].kill()
        processes["listener"].wait(timeout=3)
        self.launch("listener")
        self.assertNotEqual(self.ready("listener")["pid"], listener_pid)
        self.assertEqual(self.ready("face")["pid"], face_pid)
        self.complete_command("listen")

    def test_duplicate_device_owners_are_refused(self):
        for name in ("listener", "speaker"):
            self.launch(name)
            original = self.ready(name)["pid"]
            duplicate = self.launch(name)
            self.assertNotEqual(duplicate.wait(timeout=3), 0)
            self.assertEqual(self.ready(name)["pid"], original)

    def test_missing_listener_reports_failed_job_then_recovers(self):
        self.launch("speaker")
        self.launch("coordinator")
        self.ready("speaker")
        self.ready("coordinator")
        ipc.call("coordinator", "command", {"text": "listen"})
        status = eventually(lambda: (lambda s: s if s["last_error"] else None)(ipc.call("coordinator", "health")))
        self.assertIn("listener unavailable", status["last_error"])
        self.assertEqual(status["last_job"]["state"], "failed")
        self.launch("listener")
        self.ready("listener")
        self.complete_command("listen")

    def monitoring(self):
        return eventually(lambda: ipc.call("listener", "health")["audio"]["monitoring"], timeout=8)

    def wake(self):
        self.monitoring()
        (Path(self.runtime) / "wake-trigger").touch()
        return eventually(lambda: ipc.call("listener", "health")["audio"]["activation_id"])

    def uploads(self):
        path = Path(self.runtime) / "uploads"
        return path.read_text().splitlines() if path.exists() else []

    def test_wake_handoff_survives_face_crash_and_rearms_with_one_microphone(self):
        processes = self.start_all()
        original = self.ready("listener")["pid"]
        activation = self.wake()
        processes["face"].kill()
        processes["face"].wait(timeout=3)
        # More wake-like audio while recording/processing must not create extra uploads.
        (Path(self.runtime) / "wake-trigger").touch()
        eventually(lambda: self.uploads() == ["wake_word"], timeout=8)
        self.monitoring()
        self.assertEqual(self.uploads(), ["wake_word"])
        self.assertEqual(self.ready("listener")["pid"], original)
        with wave.open(str(Path(self.runtime) / "wake-request.wav"), "rb") as wav:
            pcm = wav.readframes(wav.getnframes())
            # Synthetic wake marker and audio preceding it reach the server unchanged.
            marker = pcm.find(b"\x01\0" * 1280)
            self.assertGreater(marker, 0)
            # This wall-clock fixture is subject to OS scheduling. Exact four-second
            # boundaries and every PCM byte are checked with the deterministic clock.
            self.assertGreater(len(pcm) - marker, 3.5 * 32000)
        self.launch("face")
        self.ready("face")
        self.complete_command("listen")
        self.assertEqual(self.uploads(), ["wake_word", "typed_listen"])
        self.assertEqual((Path(self.runtime) / "microphone-opens").read_text().splitlines(), [str(original)])

    def test_listener_restart_discards_activation_and_accepts_new_wake(self):
        processes = self.start_all()
        activation = self.wake()
        eventually(lambda: ipc.call("listener", "health")["state"] == "busy")
        original = processes["listener"].pid
        processes["listener"].kill()
        processes["listener"].wait(timeout=3)
        self.launch("listener")
        self.assertNotEqual(self.ready("listener")["pid"], original)
        status = eventually(lambda: (lambda s: s if s["last_job"] else None)(ipc.call("coordinator", "health")))
        self.assertEqual(status["last_job"]["state"], "failed")
        with self.assertRaisesRegex(ipc.IPCError, "stale"):
            ipc.call("listener", "record", {"activation_id": activation})
        self.wake()
        eventually(lambda: self.uploads() == ["wake_word"], timeout=8)
        self.monitoring()
        opens = (Path(self.runtime) / "microphone-opens").read_text().splitlines()
        self.assertEqual(len(opens), 2)
        self.assertEqual(len(set(opens)), 2)

    def test_playback_blocks_self_activation_and_recovers_after_speaker_restart(self):
        processes = self.start_all()
        self.monitoring()
        ipc.call("coordinator", "command", {"text": "Hey Navi"})
        eventually(lambda: ipc.call("speaker", "health")["state"] == "busy")
        (Path(self.runtime) / "wake-trigger").touch()
        self.monitoring()
        self.assertEqual(self.uploads(), [])
        processes["speaker"].kill()
        processes["speaker"].wait(timeout=3)
        eventually(lambda: not ipc.call("listener", "health")["audio"]["speaker_safe"])
        self.launch("speaker")
        self.ready("speaker")
        self.wake()
        eventually(lambda: self.uploads() == ["wake_word"], timeout=8)
        self.monitoring()
