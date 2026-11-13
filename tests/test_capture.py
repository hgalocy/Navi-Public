"""Listener subprocess ownership, failures and forced cleanup."""
import subprocess
import threading
from unittest.mock import Mock, patch

from test_logging import LoggingFixture, load_module


class CaptureTests(LoggingFixture):
    def setUp(self):
        super().setUp()
        with patch.dict("sys.modules", {"Logging": self.logs}):
            self.capture = load_module("capture_test", "Listener/capture.py")
        modules = patch.dict("sys.modules", {"Logging": self.logs})
        modules.start()
        self.addCleanup(modules.stop)

    def test_capture_tracks_one_unbounded_raw_process_before_reading(self):
        process = Mock(returncode=0)
        process.poll.return_value = 0
        process.stderr.readline.return_value = b""
        tracked = []
        with patch.object(self.capture.subprocess, "Popen", return_value=process) as popen, patch.object(self.capture.selectors, "DefaultSelector"):
            with self.capture.Stream(tracked.append):
                self.assertEqual(tracked, [process])
            command = popen.call_args.args[0]
            self.assertIn("raw", command)
            self.assertIn("16000", command)
            self.assertNotIn("-d", command)
            popen.assert_called_once()
        process.kill.assert_not_called()
        process.wait.assert_called_once_with(timeout=2)

    def test_capture_timeout_kills_and_reaps_child(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("arecord", 2), None]
        stream = self.capture.Stream(Mock())
        stream.process = process
        stream.close()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_failed_capture_preserves_stderr_and_does_not_succeed(self):
        stream = self.capture.Stream(Mock())
        stream.process, stream.selector = Mock(), Mock()
        stream.process.poll.return_value = 2
        stream.errors.append("Microphone unavailable")
        with patch.object(self.capture.os, "read", return_value=b""):
            with self.assertRaisesRegex(RuntimeError, "Microphone unavailable"):
                stream.read_frame(threading.Event())

    def test_partial_pipe_reads_make_one_complete_frame(self):
        stream = self.capture.Stream(Mock())
        stream.process, stream.selector = Mock(), Mock()
        with patch.object(self.capture.os, "read", side_effect=[b"a" * 100, b"b" * 2460]):
            self.assertEqual(stream.read_frame(threading.Event()), b"a" * 100 + b"b" * 2460)

    def test_stall_and_tracking_failure_are_bounded_and_reaped(self):
        stream = self.capture.Stream(Mock())
        stream.process, stream.selector = Mock(), Mock()
        with patch.object(self.capture.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaisesRegex(RuntimeError, "stalled"):
                stream.read_frame(threading.Event())
        process = Mock()
        process.poll.return_value = None
        with patch.object(self.capture.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "stopping"):
                with self.capture.Stream(Mock(side_effect=RuntimeError("stopping"))):
                    self.fail("Must not enter a failed stream")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=2)
