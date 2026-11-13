"""Keep existing conversation semantics through the new process boundaries."""
import base64
from unittest.mock import Mock, patch

from test_logging import LoggingFixture
from Coordinator import service as coordinator
from Listener import service as listener
from Audio import service as speaker


class ComponentTests(LoggingFixture):
    def setUp(self):
        super().setUp()
        self.client = Mock()
        modules = patch.dict("sys.modules", {"Logging": self.logs})
        modules.start()
        self.addCleanup(modules.stop)
        playback_ipc = patch.object(speaker, "call", return_value={"suppressed": True})
        playback_ipc.start()
        self.addCleanup(playback_ipc.stop)

    def make_service(self, name):
        return coordinator.create_service(network=self.client)

    def test_listen_upload_and_reply_preserve_origin_and_interaction(self):
        service = self.make_service("coordinator")
        self.client.send_audio.return_value.json.return_value = {"user_text": "Question?", "text": "Answer!"}
        details = self.logs.interaction_details()
        wav = b"RIFF-test-fixture"

        def upload(files, **metadata):
            self.assertEqual(files["file"].read(), wav)
            self.assertEqual(metadata, {"source": "microphone", "trigger": "typed_listen"})
            self.assertEqual(self.logs.current_context(), details)
            return self.client.send_audio.return_value

        self.client.send_audio.side_effect = upload
        work, wait = service.handler("command", {"text": "LiStEn"}, details)
        self.assertFalse(wait)
        with patch.object(coordinator, "call", side_effect=[{"wav_base64": base64.b64encode(wav).decode()}, {"played": True}]) as call:
            work()
        self.assertEqual(call.call_args_list[0].args, ("listener", "record"))
        self.assertEqual(call.call_args_list[1].args, ("speaker", "speak", {
            "text": "Answer!", "source": "microphone", "trigger": "typed_listen"}))
        self.assertTrue(all(c.kwargs["context"] == details for c in call.call_args_list))
        transcript = self.read("navi_conversation.log")
        self.assertIn('User: "Question?"', transcript)
        self.assertNotIn('User: "LiStEn"', transcript)

    def test_typed_repeat_uses_speaker_without_listener_or_server(self):
        service = self.make_service("coordinator")
        work, wait = service.handler("command", {"text": "Hello"}, {})
        with patch.object(coordinator, "call") as call:
            work()
        self.client.send_audio.assert_not_called()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[:2], ("speaker", "speak"))
        self.assertIn('User: "Hello"', self.read("navi_conversation.log"))

    def test_server_error_does_not_become_speech(self):
        service = self.make_service("coordinator")
        self.client.send_audio.return_value.raise_for_status.side_effect = RuntimeError("HTTP error")
        work, wait = service.handler("command", {"text": "listen"}, {})
        with patch.object(coordinator, "call", return_value={"wav_base64": "UklGRg=="}) as call:
            with self.assertRaisesRegex(RuntimeError, "HTTP error"):
                work()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.read("navi_conversation.log"), "")

    def test_listener_hands_off_existing_stream_without_another_recorder(self):
        audio = Mock()
        audio.reserve.return_value = "a" * 32
        audio.recording.return_value = {"wav_base64": base64.b64encode(b"RIFF-fixture").decode()}
        service = listener.create_service(audio=audio)
        work, wait = service.handler("record", {}, {})
        result = work()
        self.assertTrue(wait)
        self.assertEqual(base64.b64decode(result["wav_base64"]), b"RIFF-fixture")
        audio.reserve.assert_called_once_with(None)
        audio.recording.assert_called_once_with("a" * 32)
        audio.start.assert_not_called()

    def test_speaker_owns_playback_and_exposes_failure(self):
        playback = Mock(return_value=False)
        sender = Mock()
        service = speaker.create_service(sender, playback=playback)
        work, wait = service.handler("speak", {"text": "Hello", "source": "keyboard", "trigger": "typed_repeat"}, {})
        with self.assertRaisesRegex(RuntimeError, "playback failed"):
            work()
        self.assertTrue(wait)
        playback.assert_called_once_with("Hello", on_envelope=sender.send,
                                         process_callback=service.track_process)
        self.assertIn('Navi: "Hello"', self.read("navi_conversation.log"))

    def test_invalid_commands_rejected_before_starting_work(self):
        service = self.make_service("coordinator")
        for args in ({}, {"text": " "}, {"text": "a" * 4001}):
            with self.assertRaises(RuntimeError):
                service.handler("command", args, {})

    def test_speaker_waits_for_output_before_ready_and_exposes_device_health(self):
        output = Mock()
        output.status.return_value = {"device": "plughw:CARD=Device,DEV=0", "ready": True}
        service = speaker.create_service(Mock(), playback=Mock(return_value=True), output=output)
        output.prepare.assert_not_called()
        service.start()
        output.prepare.assert_called_once_with(timeout=20)
        self.assertEqual(service.status()["audio"], output.status.return_value)
        service.tick()
        output.check.assert_called_once_with()

    def test_speaker_unavailable_at_boot_fails_start_for_independent_restart(self):
        output = Mock()
        output.prepare.side_effect = RuntimeError("Speaker PCM unavailable")
        playback = Mock()
        service = speaker.create_service(Mock(), playback=playback, output=output)
        with self.assertRaisesRegex(RuntimeError, "Speaker PCM unavailable"):
            service.start()
        playback.assert_not_called()
