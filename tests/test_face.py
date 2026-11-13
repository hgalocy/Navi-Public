"""Exercise renderer callbacks without a display or GPU."""
import types
from collections import defaultdict
from unittest.mock import MagicMock, Mock, patch

from test_logging import LoggingFixture, load_module
from Face.service import CommandSubmitter


class FaceTests(LoggingFixture):
    def test_mouth_uses_ipc_samples_and_keyboard_submits_commands(self):
        events = {}
        window = Mock()
        window.get_framebuffer_size.return_value = (800, 600)
        window.event.side_effect = lambda fn: events.setdefault(fn.__name__, fn)
        pyglet = types.SimpleNamespace(window=types.SimpleNamespace(
            Window=Mock(return_value=window), key=types.SimpleNamespace(ENTER=13, BACKSPACE=8)), app=Mock())
        context = MagicMock()
        context.program.return_value = defaultdict(Mock)
        moderngl = types.SimpleNamespace(create_context=Mock(return_value=context), TRIANGLE_STRIP=5)
        numpy = types.SimpleNamespace(random=types.SimpleNamespace(uniform=Mock(return_value=4)),
                                      array=Mock(return_value=Mock()))
        with patch.dict("sys.modules", {"pyglet": pyglet, "moderngl": moderngl, "numpy": numpy}):
            renderer = load_module("face_renderer_test", "Face/Animation.py")
        submit, tick = Mock(), Mock()
        sample = Mock(side_effect=[(0.8, True), (0, False)])

        def frames():
            events["on_draw"]()
            self.assertAlmostEqual(context.program.return_value["mouth_open_"].value, 0.2)
            events["on_draw"]()
            self.assertAlmostEqual(context.program.return_value["mouth_open_"].value, 0.15)
            events["on_text"]("listenx")
            events["on_key_press"](8, 0)
            events["on_key_press"](13, 0)
        pyglet.app.run.side_effect = frames
        renderer.run(submit_command=submit, on_tick=tick, playback_sample=sample)
        submit.assert_called_once_with("listen")
        self.assertEqual(tick.call_count, 2)
        self.assertEqual(sample.call_count, 2)
        self.assertEqual(context.simple_vertex_array.return_value.render.call_count, 2)

    def test_command_submission_is_bounded_and_recovers_after_rejection(self):
        with patch("Face.service.ThreadPoolExecutor") as executor:
            sender = CommandSubmitter(self.logs.logger)
            pending = executor.return_value.submit.return_value
            pending.done.return_value = False
            sender.submit("listen")
            sender.submit("listen")
            executor.return_value.submit.assert_called_once()
            pending.done.return_value = True
            sender.submit("hello")
            self.assertEqual(executor.return_value.submit.call_count, 2)
            sender.close()
            executor.return_value.shutdown.assert_called_once_with(wait=True)

    def test_missing_coordinator_is_logged_without_terminating_face(self):
        from Services.ipc import IPCError
        sender = CommandSubmitter(self.logs.logger)
        try:
            with patch("Face.service.call", side_effect=IPCError("offline")):
                sender._send("listen")
            self.assertIn("command_rejected", self.read("navi.log"))
        finally:
            sender.close()
