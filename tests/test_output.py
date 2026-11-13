"""Reproduce boot reordering, USB delays and mixer resets without touching audio."""
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from Audio.Output import Output, DEFAULT_DEVICE
from test_logging import LoggingFixture


class OutputTests(LoggingFixture):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"NAVI_SPEAKER_DEVICE": DEFAULT_DEVICE,
                                      "NAVI_SPEAKER_MIXER": "Speaker", "NAVI_SPEAKER_VOLUME": "46%"})
        env.start()
        self.addCleanup(env.stop)
        logs = patch.dict("sys.modules", {"Logging": self.logs})
        logs.start()
        self.addCleanup(logs.stop)
        self.clock = 0
        self.cards = {0: "Device", 1: "ArrayUAC10", 2: "HDA"}
        self.available = True
        self.muted = True
        self.volume = 0
        self.commands = []

    def sleep(self, duration):
        self.clock += duration

    def endpoint(self, path):
        return (self.available and "Device" in self.cards.values() and
                str(path).replace("\\", "/") == "/proc/asound/Device/pcm0p/info")

    def amixer(self, args, **kwargs):
        self.commands.append(args)
        self.assertEqual(args[:3], ["amixer", "-D", "hw:CARD=Device"])
        self.assertEqual(kwargs["timeout"], 2)
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")
        if args[3] == "sset":
            if args[4:] == ["Speaker", "playback", "mute"]:
                self.muted = True
            else:
                self.assertEqual(args[4:], ["Speaker", "playback", "46%", "unmute"])
                self.muted, self.volume = False, 46
            return subprocess.CompletedProcess(args, 0, "", "")
        state = "off" if self.muted else "on"
        text = ("Simple mixer control 'Speaker',0\n Capabilities: pvolume pswitch\n"
                " Front Left: Playback 17 [{0}%] [-20.00dB] [{1}]\n"
                " Front Right: Playback 17 [{0}%] [-20.00dB] [{1}]\n").format(self.volume, state)
        return subprocess.CompletedProcess(args, 0, text, "")

    def prepare(self, output, timeout=5, run=None, sleep=None):
        with patch.object(Path, "is_file", autospec=True, side_effect=self.endpoint), \
                patch("Audio.Output.subprocess.run", side_effect=run or self.amixer), \
                patch("Audio.Output.time.monotonic", side_effect=lambda: self.clock), \
                patch("Audio.Output.time.sleep", side_effect=sleep or self.sleep):
            output.prepare(timeout=timeout)

    def test_reboots_reorder_cards_but_keep_usb_speaker_and_restore_mixer(self):
        for cards in ({0: "Device", 1: "ArrayUAC10", 2: "HDA"},
                      {0: "HDA", 1: "ArrayUAC10", 2: "Device"},
                      {0: "ArrayUAC10", 1: "Device", 2: "HDA"}):
            with self.subTest(cards=cards):
                self.cards = cards
                self.muted, self.volume = True, 0  # Driver/restore state after reboot.
                output = Output()
                self.prepare(output)
                self.assertEqual(output.device, DEFAULT_DEVICE)
                self.assertEqual(output.pcm, Path("/proc/asound/Device/pcm0p/info"))
                self.assertTrue(output.ready)
                self.assertFalse(self.muted)
                self.assertEqual(self.volume, 46)

    def test_default_device_is_named_without_any_saved_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            output = Output()
        self.assertEqual(output.device, DEFAULT_DEVICE)
        self.assertEqual(output.mixer, "Speaker")

    def test_delayed_usb_enumeration_waits_then_prepares_without_losing_speech(self):
        output = Output()
        self.available = False
        def appear(duration):
            self.sleep(duration)
            if self.clock >= 1:
                self.available = True
        self.prepare(output, sleep=appear)
        self.assertTrue(output.ready)
        self.assertAlmostEqual(self.clock, 1.15)
        self.assertEqual(len(self.commands), 3)

    def test_missing_speaker_does_not_fall_back_to_hdmi_or_microphone(self):
        self.cards = {0: "HDA", 1: "ArrayUAC10"}
        output = Output()
        with self.assertRaisesRegex(RuntimeError, "Speaker PCM unavailable"):
            self.prepare(output, timeout=1)
        self.assertEqual(self.clock, 1)
        self.assertFalse(output.ready)
        self.assertEqual(self.commands, [])

    def test_successive_utterances_do_not_cycle_mute_or_repeat_startup_delay(self):
        output = Output()
        self.prepare(output)
        for _ in range(3):
            self.prepare(output)
        self.assertEqual(len(self.commands), 3)
        self.assertAlmostEqual(self.clock, 0.15)
        self.assertEqual(self.volume, 46)
        self.assertEqual(output.status()["mute_cycle_on"], "startup")

    def test_speaker_restart_initializes_mute_cycle_again(self):
        self.prepare(Output())
        self.muted, self.volume = True, 0
        restarted = Output()
        self.prepare(restarted)
        self.assertEqual(len(self.commands), 6)
        self.assertFalse(self.muted)
        self.assertEqual(self.volume, 46)

    def test_lost_endpoint_before_speech_fails_without_repeating_mixer_initialization(self):
        output = Output()
        self.prepare(output)
        self.available = False
        with self.assertRaisesRegex(RuntimeError, "disappeared"):
            self.prepare(output)
        self.assertEqual(len(self.commands), 3)
        self.assertFalse(output.ready)

    def test_cached_unmuted_switch_with_silent_hardware_requires_a_real_transition(self):
        cached_muted = False
        hardware_muted = True
        transitions = []

        def cached_driver(args, **kwargs):
            nonlocal cached_muted, hardware_muted
            if args[3] == "sset":
                requested = args[-1] == "mute"
                if requested != cached_muted:
                    transitions.append(requested)
                    hardware_muted = requested
                    cached_muted = requested
                return subprocess.CompletedProcess(args, 0, "", "")
            state = "off" if cached_muted else "on"
            text = ("Capabilities: pvolume pswitch\n"
                    " Front Left: Playback 18 [49%] [{0}]\n"
                    " Front Right: Playback 18 [49%] [{0}]\n").format(state)
            return subprocess.CompletedProcess(args, 0, text, "")

        # Reproduce the old sequence: successful command and unmuted readback,
        # but no hardware write because the cached bit was already unmuted.
        output = Output()
        with patch("Audio.Output.subprocess.run", side_effect=cached_driver):
            output._amixer("sset", "Speaker", "playback", "46%", "unmute")
            self.assertTrue(output._audible(output._amixer("sget", "Speaker")))
        self.assertTrue(hardware_muted)
        self.assertEqual(transitions, [])

        self.prepare(output, run=cached_driver)
        self.assertFalse(hardware_muted)
        self.assertEqual(transitions, [True, False])
        self.assertTrue(output.ready)

    def test_mixer_ignoring_unmute_never_reports_output_ready(self):
        def ignored(args, **kwargs):
            result = self.amixer(args, **kwargs)
            if args[3] == "sset":
                self.muted = True
            return result
        output = Output()
        with self.assertRaisesRegex(RuntimeError, "still muted"):
            self.prepare(output, timeout=1, run=ignored)
        self.assertFalse(output.ready)

    def test_mixer_command_failures_and_timeouts_are_bounded(self):
        for failure in (FileNotFoundError("amixer missing"),
                        subprocess.TimeoutExpired("amixer", 2)):
            with self.subTest(failure=failure):
                self.clock = 0
                output = Output()
                def fail(*args, **kwargs):
                    raise failure
                with self.assertRaises(RuntimeError):
                    self.prepare(output, timeout=1, run=fail)
                self.assertFalse(output.ready)
                self.assertEqual(self.clock, 1)

    def test_temporarily_unavailable_mixer_is_retried_before_any_speech(self):
        calls = 0
        def transient(args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return subprocess.CompletedProcess(args, 1, "", "Device not ready")
            return self.amixer(args, **kwargs)
        output = Output()
        self.prepare(output, run=transient)
        self.assertTrue(output.ready)
        self.assertAlmostEqual(self.clock, 0.65)

    def test_disconnected_output_fails_speaker_health_for_supervisor_recovery(self):
        output = Output()
        self.prepare(output)
        self.available = False
        with patch.object(Path, "is_file", autospec=True, side_effect=self.endpoint):
            self.assertFalse(output.status()["ready"])
            with self.assertRaisesRegex(RuntimeError, "disappeared"):
                output.check()
        self.assertFalse(output.ready)

    def test_explicit_custom_route_does_not_change_unrelated_mixer(self):
        with patch.dict(os.environ, {"NAVI_SPEAKER_DEVICE": "default"}):
            os.environ.pop("NAVI_SPEAKER_MIXER")
            output = Output()
        with patch("Audio.Output.subprocess.run") as run:
            output.prepare()
        self.assertEqual(output.device, "default")
        self.assertEqual(output.mixer, "")
        run.assert_not_called()

    def test_invalid_volume_is_rejected_before_hardware_changes(self):
        for volume in ("0%", "101%", "-1%", "50%; echo bad"):
            with self.subTest(volume=volume), patch.dict(os.environ, {"NAVI_SPEAKER_VOLUME": volume}):
                with self.assertRaisesRegex(ValueError, "1% to 100%"):
                    Output()

    def test_each_playback_channel_must_be_audible(self):
        for settings in ("", "Capabilities: pvolume\n Front Left: Playback 0 [0%] [on]",
                         "Capabilities: pvolume pswitch\n Front Left: Playback 17 [46%] [on]\n"
                         " Front Right: Playback 17 [46%] [off]"):
            with self.subTest(settings=settings):
                self.assertFalse(Output._audible(settings))
