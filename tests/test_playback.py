"""Exercise speech streaming and process lifecycle with simulated audio tools."""

import io
import math
import struct
import types
from unittest.mock import Mock, patch

from test_logging import LoggingFixture, load_module


class Samples:
    def __init__(self, raw):
        self.values = struct.unpack("<" + "h" * (len(raw) // 2), raw)
        self.size = len(self.values)

    def astype(self, dtype):
        return self

    def __mul__(self, other):
        return [value * value for value in self.values]


class PlaybackTests(LoggingFixture):
    def setUp(self):
        super().setUp()
        with patch.dict("sys.modules", {"Logging": self.logs, "numpy": types.ModuleType("numpy")}):
            self.speak = load_module("playback_test", "Audio/Playback.py")
        self.envelopes = Mock()
        self.track_process = Mock()
        self.output = Mock(device="plughw:CARD=Device,DEV=0")
        self.speak.np = types.SimpleNamespace(
            frombuffer=lambda raw, dtype: Samples(raw), int16=int, float32=float,
            sqrt=math.sqrt, mean=lambda values: sum(values) / len(values),
        )

    def process(self, audio=None, returncode=0):
        return Mock(stdin=io.BytesIO(), stdout=io.BytesIO(audio) if audio is not None else None,
                    stderr=io.BytesIO(), wait=Mock(return_value=returncode))

    def run_speech(self, processes):
        details = self.logs.interaction_details()
        with self.logs.interaction_context(details), \
                patch.object(self.speak.subprocess, "Popen", side_effect=processes), \
                patch.object(self.speak, "_stderr_worker", return_value=Mock()):
            self.speak.play("Hello", on_envelope=self.envelopes, process_callback=self.track_process,
                            output=self.output)
        self.envelopes.assert_any_call(0.0, 0.0, True)
        return self.read("navi.log")

    def test_completion_requires_audio_and_both_successful_exits(self):
        piper, aplay = self.process(b"\x10\x00" * 256), self.process()
        runtime = self.run_speech([piper, aplay])
        self.assertIn("subprocess_exit process=piper returncode=0", runtime)
        self.assertIn("subprocess_exit process=aplay returncode=0", runtime)
        self.assertIn("stage=playback status=completed", runtime)
        self.assertIn("stage=tts_first_audio elapsed_ms=", runtime)
        self.assertIn("audio_duration_ms=11.6 bytes_written=512", runtime)
        piper.wait.assert_called_once_with(timeout=5)
        aplay.wait.assert_called_once_with(timeout=5)

    def test_missing_piper_is_a_playback_failure_and_resets_animation(self):
        runtime = self.run_speech([FileNotFoundError("piper missing")])
        self.assertIn("piper missing", runtime)
        self.assertIn("stage=playback status=failed", runtime)
        self.assertNotIn("status=completed", runtime)

    def test_output_unavailable_never_launches_piper_or_animates_speech(self):
        self.output.prepare.side_effect = RuntimeError("Speaker PCM unavailable")
        with patch.object(self.speak.subprocess, "Popen") as spawn:
            completed = self.speak.play("Hello", on_envelope=self.envelopes,
                                        process_callback=self.track_process, output=self.output)
        self.assertFalse(completed)
        spawn.assert_not_called()
        self.assertEqual(self.envelopes.call_args_list, [((0.0, 0.0, True),)])
        self.assertIn("stage=playback status=failed", self.read("navi.log"))

    def test_aplay_uses_prepared_named_route_instead_of_numeric_default(self):
        with patch.object(self.speak.subprocess, "Popen", side_effect=[
                self.process(b"\x10\x00"), self.process()]) as spawn, \
                patch.object(self.speak, "_stderr_worker", return_value=Mock()):
            self.assertTrue(self.speak.play("Hello", on_envelope=self.envelopes,
                                            process_callback=self.track_process, output=self.output))
        self.output.prepare.assert_called_once_with()
        self.assertEqual(spawn.call_args_list[1].args[0][2], self.output.device)

    def test_request_after_failed_output_prepare_can_play_normally(self):
        self.output.prepare.side_effect = [RuntimeError("Speaker PCM unavailable"), None]
        with patch.object(self.speak.subprocess, "Popen", side_effect=[
                self.process(b"\x10\x00"), self.process()]) as spawn, \
                patch.object(self.speak, "_stderr_worker", return_value=Mock()):
            self.assertFalse(self.speak.play("First", on_envelope=self.envelopes,
                                             process_callback=self.track_process, output=self.output))
            self.assertTrue(self.speak.play("Second", on_envelope=self.envelopes,
                                            process_callback=self.track_process, output=self.output))
        self.assertEqual(spawn.call_count, 2)

    def test_missing_aplay_still_reaps_piper(self):
        piper = self.process(b"")
        runtime = self.run_speech([piper, FileNotFoundError("aplay missing")])
        piper.wait.assert_called_once()
        self.assertTrue(piper.stdin.closed)
        self.assertIn("stage=playback status=failed", runtime)

    def test_nonzero_exit_never_reports_successful_playback(self):
        for name in ("piper", "aplay"):
            with self.subTest(process=name):
                piper = self.process(b"\x10\x00", returncode=3 if name == "piper" else 0)
                aplay = self.process(returncode=3 if name == "aplay" else 0)
                runtime = self.run_speech([piper, aplay])
                self.assertIn(f"subprocess_exit process={name} returncode=3", runtime)
        self.assertNotIn("stage=playback status=completed", runtime)

    def test_silent_generator_does_not_count_as_playback(self):
        runtime = self.run_speech([self.process(b""), self.process()])
        self.assertIn("stage=playback status=failed", runtime)
        self.assertIn("bytes_written=0", runtime)

    def test_broken_audio_pipe_logs_failure_and_reaps_both_processes(self):
        piper, aplay = self.process(b"\x10\x00"), self.process()
        aplay.stdin = Mock(closed=False)
        aplay.stdin.write.side_effect = BrokenPipeError("Speaker failed")
        runtime = self.run_speech([piper, aplay])
        self.assertIn("Speaker failed", runtime)
        self.assertIn("stage=playback status=failed", runtime)
        piper.wait.assert_called_once()
        aplay.wait.assert_called_once()

    def test_timeout_terminates_then_kills_and_remains_failed(self):
        piper, aplay = self.process(b"\x10\x00"), self.process()
        timeout = self.speak.subprocess.TimeoutExpired("aplay", 5)
        aplay.wait.side_effect = [timeout, timeout, -9]
        runtime = self.run_speech([piper, aplay])
        aplay.terminate.assert_called_once()
        aplay.kill.assert_called_once()
        self.assertIn("subprocess_timeout process=aplay", runtime)
        self.assertIn("returncode=-9 timed_out=True", runtime)
        self.assertIn("stage=playback status=failed", runtime)

    def test_stderr_drainer_retains_interaction_without_speech_thread(self):
        details = self.logs.interaction_details()
        process = self.process()
        with self.logs.interaction_context(details), patch.object(self.speak.threading, "Thread") as factory:
            self.speak._stderr_worker(process, "piper")
        self.assertEqual(factory.call_args.kwargs["kwargs"]["context"], details)
        self.logs.stream_subprocess_output(io.BytesIO(b"Speaker failed\n"), context=details)
        self.assertIn("interaction=" + details["interaction_id"], self.read("navi.log"))

    def test_consecutive_utterances_have_independent_playback_clocks(self):
        for _ in range(2):
            self.run_speech([self.process(b"\x10\x00" * 256), self.process()])
        samples = [call.args for call in self.envelopes.call_args_list if not call.args[2]]
        self.assertEqual(len(samples), 2)
        self.assertGreaterEqual(samples[1][0], samples[0][0])
        self.assertEqual(self.track_process.call_count, 4)
