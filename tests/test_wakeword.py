"""Deterministic continuous PCM handoff, IPC and activation lifecycle tests."""
import base64
import io
import os
import threading
import time
import wave
from unittest.mock import Mock, patch

from Listener.capture import FRAME_BYTES
from Listener.wakeword import settings
from Coordinator import service as coordinator
from Listener import service as listener
from Audio import service as speaker
from Services import runtime
from Services.ipc import IPCError, encode, MAX_MESSAGE
from Listener.audio import ContinuousAudio, MAX_WAV_BYTES
from test_logging import LoggingFixture


class WakeTests(LoggingFixture):
    def setUp(self):
        super().setUp()
        modules = patch.dict("sys.modules", {"Logging": self.logs})
        modules.start()
        self.addCleanup(modules.stop)
        self.now = 10.0
        self.audio = ContinuousAudio(Mock(), ipc_call=Mock(return_value={"state": "ready"}), clock=lambda: self.now)
        self.audio.speaker_safe = True
        self.audio.speaker_checked = self.now

    def feed(self, number):
        self.now = round(self.now + 0.08, 5)
        raw = number.to_bytes(2, "little") * 1280
        self.audio.ingest(raw)
        if not self.audio.frames.empty():
            self.audio.frames.get_nowait()  # Deterministic tests drive detection directly.
        return raw

    def pcm(self, result):
        data = base64.b64decode(result["wav_base64"])
        with wave.open(io.BytesIO(data), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 16000))
            return wav.readframes(wav.getnframes())

    def arm(self):
        self.now = max(self.now, self.audio.cooldown_until)
        self.audio.speaker_checked = self.now
        self.audio.model_generation = self.audio.generation

    def test_threshold_defaults_and_invalid_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings(), (True, 0.95))
        for value in ("nan", "inf", "0", "1.01", "garbage"):
            with patch.dict(os.environ, {"NAVI_WAKE_THRESHOLD": value}), self.assertRaises(ValueError):
                settings()
        with patch.dict(os.environ, {"NAVI_WAKE_ENABLED": "0"}):
            self.assertFalse(settings()[0])

    def test_wake_sends_one_event_at_threshold_and_holds_until_release(self):
        self.assertIsNone(self.audio.detect(0.949))
        activation = self.audio.detect(0.95)
        self.assertEqual(len(activation), 32)
        event_id, details = self.audio.events.get_nowait()
        self.assertEqual(event_id, activation)
        self.assertNotEqual(details["interaction_id"], "-")
        for _ in range(100):
            self.assertIsNone(self.audio.detect(1))
        self.assertTrue(self.audio.events.empty())
        with self.assertRaisesRegex(IPCError, "in progress"):
            self.audio.reserve()

    def test_preroll_and_four_seconds_are_contiguous_even_with_delayed_ipc(self):
        before = b"".join(self.feed(n) for n in range(20))
        self.arm()
        activation = self.audio.detect(1)
        detected = self.now
        after = b"".join(self.feed(n) for n in range(20, 70))
        self.assertEqual(self.audio.capture["deadline"], detected + 4)
        self.assertTrue(self.audio.capture["done"])
        # Coordinator may fetch after capture has finished; this never starts a new recording.
        self.now += 1
        self.assertEqual(self.audio.reserve(activation), activation)
        result = self.audio.recording(activation)
        self.assertEqual(self.pcm(result), before[-48000:] + after)
        self.assertEqual(len(base64.b64decode(result["wav_base64"])), MAX_WAV_BYTES)
        self.assertLess(len(encode({"v": 1, "result": result})), MAX_MESSAGE)
        with self.assertRaisesRegex(IPCError, "consumed"):
            self.audio.reserve(activation)

    def test_typed_listen_uses_the_same_capture_and_excludes_old_preroll(self):
        self.feed(99)
        activation = self.audio.reserve()
        expected = b"".join(self.feed(n) for n in range(50))
        result = self.audio.recording(activation)
        self.assertEqual(self.pcm(result), expected)
        self.assertEqual(len(expected), 128000)

    def test_cooldown_and_high_score_latch_prevent_duplicates_after_release(self):
        activation = self.audio.detect(1)
        self.audio.release(activation)
        self.audio.model_generation = self.audio.generation  # Reset completes during cooldown.
        self.assertIsNone(self.audio.detect(0))
        self.now += 1.99
        self.audio.speaker_checked = self.now
        self.assertIsNone(self.audio.detect(1))
        self.now += 0.01
        self.assertIsNone(self.audio.detect(1))
        self.audio.detect(0)
        self.audio.events.get_nowait()
        self.assertIsNotNone(self.audio.detect(1))

    def test_speaker_restart_missing_health_and_stale_health_fail_closed(self):
        self.audio.playback_started()
        self.feed(1)
        self.assertIsNone(self.audio.detect(1))
        self.audio.call.side_effect = IPCError("speaker unavailable")
        self.audio.poll_speaker()
        self.assertFalse(self.audio.speaker_safe)
        self.audio.call.side_effect = None
        self.audio.poll_speaker()
        self.assertTrue(self.audio.speaker_safe)
        self.assertEqual(self.audio.ring, b"")
        self.assertIsNone(self.audio.detect(1))  # post-playback cooldown
        self.now += 2
        self.audio.poll_speaker()
        self.audio.model_generation = self.audio.generation
        self.assertIsNotNone(self.audio.detect(1))
        fresh = ContinuousAudio(Mock(), clock=lambda: self.now)
        self.assertFalse(fresh.status()["monitoring"])

    def test_playback_barrier_invalidates_inflight_inference_and_health_reply(self):
        generation = self.audio.generation
        def health(*args, **kwargs):
            self.audio.playback_started()
            return {"state": "ready"}
        self.audio.call.side_effect = health
        self.audio.poll_speaker()
        self.assertFalse(self.audio.speaker_safe)
        self.assertIsNone(self.audio.detect(1, generation))
        self.assertTrue(self.audio.events.empty())

    def test_listener_restart_rejects_old_activation_and_cannot_replay_audio(self):
        activation = self.audio.detect(1)
        restarted = ContinuousAudio(Mock())
        with self.assertRaisesRegex(IPCError, "stale"):
            restarted.reserve(activation)
        restarted.release(activation)
        self.assertIsNone(restarted.capture)

    def test_listener_controls_stay_available_during_recording(self):
        service = listener.create_service(self.audio)
        work, wait = service.handler("record", {}, {})
        activation = self.audio.capture["id"]
        service.busy_since = self.now
        self.assertEqual(service.control("playback_started", {}, {}), {"suppressed": True})
        self.assertEqual(service.control("release", {"activation_id": "wrong"}, {}), {"released": True})
        self.assertEqual(self.audio.capture["id"], activation)
        service.control("release", {"activation_id": activation}, {})
        with self.assertRaisesRegex(IPCError, "expired"):
            work()

    def test_coordinator_routes_wake_audio_once_and_releases_after_speech(self):
        network = Mock()
        network.send_audio.return_value.json.return_value = {"user_text": "What's your name?", "text": "Navi"}
        service = coordinator.create_service(network)
        activation = "a" * 32
        context = self.logs.interaction_details()
        work, wait = service.handler("wake", {"activation_id": activation}, context)
        with patch.object(coordinator, "call", side_effect=[{"wav_base64": "UklGRg==", "activation_id": activation}, {"played": True}, {}]) as ipc:
            work()
        self.assertFalse(wait)
        self.assertEqual([c.args[:2] for c in ipc.call_args_list],
                         [("listener", "record"), ("speaker", "speak"), ("listener", "release")])
        self.assertEqual(ipc.call_args_list[0].args[2], {"activation_id": activation})
        self.assertTrue(all(c.kwargs["context"] == context for c in ipc.call_args_list))
        self.assertEqual(network.send_audio.call_args.kwargs, {"source": "microphone", "trigger": "wake_word"})
        with self.assertRaisesRegex(IPCError, "Duplicate"):
            service.handler("wake", {"activation_id": activation}, context)
        for args in ({}, {"activation_id": "bad"}, {"activation_id": activation, "text": "listen"}):
            with self.assertRaises(IPCError):
                service.handler("wake", args, context)

    def test_coordinator_releases_wake_when_server_or_listener_fails(self):
        service = coordinator.create_service(Mock())
        work, _ = service.handler("wake", {"activation_id": "b" * 32}, {})
        with patch.object(coordinator, "call", side_effect=[IPCError("stale"), {}]) as ipc:
            with self.assertRaisesRegex(IPCError, "stale"):
                work()
        self.assertEqual(ipc.call_args.args[:2], ("listener", "release"))

    def test_speaker_acknowledges_suppression_before_playback(self):
        ordered = []
        def playback(*args, **kwargs):
            ordered.append("playback")
            return True
        service = speaker.create_service(Mock(), playback)
        work, _ = service.handler("speak", {"text": "Hey Navi"}, {})
        with patch.object(speaker, "call", side_effect=lambda *a, **k: ordered.append("barrier")) as ipc:
            work()
        self.assertEqual(ordered, ["barrier", "playback"])
        self.assertEqual(ipc.call_args.args, ("listener", "playback_started"))

    def test_continuous_capture_does_not_wait_for_blocked_ipc(self):
        blocked, release = threading.Event(), threading.Event()
        reads = []
        class FakeStream:
            def __init__(self, track):
                reads.append("opened")
            def __enter__(self):
                return self
            def __exit__(self, *unused):
                reads.append("closed")
            def read_frame(self, stop):
                stop.wait(0.01)
                reads.append("frame")
                return b"\0" * FRAME_BYTES
        def blocked_health(*args, **kwargs):
            blocked.set()
            release.wait(2)
            return {"state": "busy"}
        audio = ContinuousAudio(stream_factory=FakeStream, ipc_call=blocked_health)
        self.addCleanup(audio.close)
        self.addCleanup(release.set)
        audio.start(Mock())
        self.assertTrue(blocked.wait(1))
        before = reads.count("frame")
        time.sleep(0.08)
        self.assertGreater(reads.count("frame"), before + 3)
        self.assertEqual(reads.count("opened"), 1)
        release.set()
        audio.close()
        self.assertEqual(reads.count("closed"), 1)

    def test_rejected_wake_is_released_and_never_retried(self):
        activation = self.audio.detect(1)
        def transport(component, op, *args, **kwargs):
            if component == "speaker":
                return {"state": "ready"}
            self.audio.stop.set()
            raise IPCError("Coordinator busy")
        self.audio.call.side_effect = transport
        self.audio._ipc_loop()
        self.assertIsNone(self.audio.capture)
        self.assertTrue(self.audio.events.empty())
        wakes = [c for c in self.audio.call.call_args_list if c.args[:2] == ("coordinator", "wake")]
        self.assertEqual(len(wakes), 1)
        self.assertEqual(wakes[0].args[2], {"activation_id": activation})

    def test_slow_model_reset_preserves_capture_without_inference_backlog(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(self.audio.close)
        self.addCleanup(release.set)
        self.audio.detector.reset.side_effect = lambda: (started.set(), release.wait(2))
        self.audio.generation += 1
        worker = threading.Thread(target=self.audio._guard, args=(self.audio._infer_loop,), daemon=True)
        self.audio.threads.append(worker)
        worker.start()
        self.assertTrue(started.wait(1))
        self.assertFalse(self.audio.status()["monitoring"])
        self.assertFalse(self.audio.status()["model_ready"])
        activation = self.audio.reserve()
        # Fifty frames exceed the eight-frame queue while reset remains blocked.
        expected = b"".join(self.feed(n) for n in range(50))
        self.audio.check()
        self.assertTrue(self.audio.frames.empty())
        self.assertEqual(self.pcm(self.audio.recording(activation)), expected)
        self.assertEqual(len(self.audio.ring), 48000)
        self.audio.release(activation)
        self.now = self.audio.cooldown_until
        self.audio.speaker_checked = self.now
        release.set()
        deadline = time.monotonic() + 1
        while not self.audio.status()["monitoring"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.audio.status()["monitoring"])
        self.assertIsNone(self.audio.error)

    def test_model_reset_failure_fails_health_with_an_empty_exception_message(self):
        self.audio.generation += 1
        self.audio.detector.reset.side_effect = RuntimeError()
        self.audio._guard(self.audio._infer_loop)
        with self.assertRaisesRegex(RuntimeError, "RuntimeError"):
            self.audio.check()

    def test_stalled_ipc_worker_fails_listener_watchdog_check(self):
        self.audio.last_ipc = self.now - 6
        with self.assertRaisesRegex(RuntimeError, "IPC worker stalled"):
            self.audio.check()

    def test_exception_with_empty_message_is_reported_as_failed_job(self):
        service = runtime.Service("listener", Mock())
        service.loop = Mock()
        service.job = "job"
        service._work(Mock(side_effect=AssertionError()), Mock())
        callback = service.loop.call_soon_threadsafe.call_args.args
        callback[0](*callback[1:])
        self.assertEqual(service.last_job, {"id": "job", "state": "failed"})
        self.assertEqual(service.last_error, "AssertionError")

    def test_capture_failure_wakes_record_waiter_and_fails_health(self):
        activation = self.audio.reserve()
        self.audio._guard(Mock(side_effect=RuntimeError("device lost")))
        with self.assertRaisesRegex(RuntimeError, "device lost"):
            self.audio.check()
        with self.assertRaisesRegex(IPCError, "device lost"):
            self.audio.recording(activation)
