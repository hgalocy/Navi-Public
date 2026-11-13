"""Component logging tests; no audio, graphics, or network packages needed."""

import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LoggingFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {
            "NAVI_LOG_DIR": self.directory.name,
            "NAVI_LOG_LEVEL": "INFO",
            "NAVI_COMPONENT": "test",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.journal = patch("sys.stdout", new=io.StringIO())
        self.journal.start()
        self.addCleanup(self.journal.stop)
        self.logs = load_module("navi_logging_test", "Logging/__init__.py")
        self.addCleanup(self.close_logs)

    def close_logs(self):
        for logger in (self.logs.logger, self.logs.conversation_logger):
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)

    def read(self, filename):
        return (Path(self.logs.LOG_DIR) / filename).read_text(encoding="utf-8")


class LoggingTests(LoggingFixture):
    def test_launches_get_unique_sessions_and_use_configured_device_name(self):
        environment = dict(os.environ, NAVI_CLIENT_NAME="desk-navi")
        command = [sys.executable, "-c", "import json, Logging; print(json.dumps(Logging.current_context()))"]
        launches = [json.loads(subprocess.run(
            command, cwd=ROOT, env=environment, check=True, capture_output=True, text=True,
        ).stdout) for _ in range(2)]
        self.assertNotEqual(launches[0]["client_session_id"], launches[1]["client_session_id"])
        self.assertTrue(all(item["client_name"] == "desk-navi" for item in launches))

    def test_context_restores_after_failure_and_timing_uses_monotonic_clock(self):
        details = self.logs.interaction_details()
        with self.assertRaises(RuntimeError), self.logs.interaction_context(details):
            with patch.object(self.logs.time, "monotonic", side_effect=[10, 10.25]):
                with self.logs.timed_stage("recording"):
                    raise RuntimeError("Recorder failed")
        self.assertEqual(self.logs.current_context()["interaction_id"], "-")
        runtime = self.read("navi.log")
        self.assertIn("stage=recording status=failed elapsed_ms=250.0", runtime)
        self.assertIn("interaction=" + details["interaction_id"], runtime)

    def test_transcript_is_separate_and_preserves_multiline_unicode(self):
        text = 'Hello, Alex!\nShe said "café".'
        self.logs.logger.info("Recording audio")
        self.logs.log_conversation("User", text, "keyboard", "typed_repeat")
        self.logs.log_conversation("Navi", text, "keyboard", "typed_repeat")
        self.logs.log_conversation("User", "  ", "keyboard", "typed_repeat")

        runtime = self.read("navi.log")
        transcript = self.read("navi_conversation.log")
        self.assertIn("Recording audio", runtime)
        self.assertNotIn("Alex", runtime)
        self.assertNotIn("Recording audio", transcript)
        lines = transcript.splitlines()
        self.assertEqual(len(lines), 2)
        for speaker, line in zip(("User", "Navi"), lines):
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} ")
            self.assertIn("[source=keyboard trigger=typed_repeat]", line)
            self.assertEqual(json.loads(line.split(f"{speaker}: ", 1)[1]), text)

    def test_runtime_threshold_does_not_disable_transcript(self):
        os.environ["NAVI_LOG_LEVEL"] = "ERROR"
        load_module("navi_logging_reload_test", "Logging/__init__.py")
        self.logs.logger.info("Hidden progress")
        self.logs.logger.error("Visible failure")
        self.logs.log_conversation("User", "Still logged", "microphone", "wakeword")
        self.assertNotIn("Hidden progress", self.read("navi.log"))
        self.assertIn("[ERROR] Visible failure", self.read("navi.log"))
        self.assertIn("trigger=wakeword", self.read("navi_conversation.log"))
        self.assertEqual(len(self.logs.logger.handlers), 2)
        self.assertEqual(len(self.logs.conversation_logger.handlers), 1)

    def test_invalid_level_falls_back_with_warning(self):
        os.environ["NAVI_LOG_LEVEL"] = "INVALID"
        load_module("navi_logging_invalid_test", "Logging/__init__.py")
        self.assertEqual(self.logs.logger.level, logging.INFO)
        self.assertIn("[WARNING] Unknown NAVI_LOG_LEVEL", self.read("navi.log"))

    def test_subprocess_severity_does_not_follow_runtime_threshold(self):
        lines = (
            b"[piper] [info] Loaded voice\n"
            b"[E:onnxruntime:] pthread_setaffinity_np failed\n"
            b"underrun!!! (at least 93 ms long)\n"
        )
        self.logs.stream_subprocess_output(io.BytesIO(lines), prefix="[audio] ")
        runtime = self.read("navi.log")
        self.assertNotIn("Loaded voice", runtime)
        self.assertIn("[ERROR] [audio] [E:onnxruntime:]", runtime)
        self.assertIn("[WARNING] [audio] underrun", runtime)
        self.logs.logger.setLevel(logging.DEBUG)
        self.logs.stream_subprocess_output(io.BytesIO(b"Loaded voice\n"))
        self.assertIn("[DEBUG] Loaded voice", self.read("navi.log"))
        self.assertEqual(self.read("navi_conversation.log"), "")

    def test_both_logs_rotate(self):
        for logger in (self.logs.logger, self.logs.conversation_logger):
            logger.handlers[0].maxBytes = 200
        for number in range(10):
            self.logs.logger.info("Runtime event %s %s", number, "x" * 100)
            self.logs.log_conversation("User", f"Message {number} " + "é" * 100, "keyboard", "typed_repeat")
        for filename in ("navi.log", "navi_conversation.log"):
            self.assertTrue((Path(self.logs.LOG_DIR) / (filename + ".1")).exists())
            self.assertTrue(self.read(filename))


if __name__ == "__main__":
    unittest.main()
