"""Installer tests use temporary homes and a fake systemd; no device actions."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import setup_services as setup


ROOT = Path(__file__).resolve().parents[1]


def completed(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="navi setup ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        self.repository = self.root / "Navi's checkout 50%"
        (self.repository / "systemd").mkdir(parents=True)
        for name in setup.UNITS:
            shutil.copyfile(ROOT / "systemd" / name, self.repository / "systemd" / name)
        for name in ("Services/control.py", "Services/__main__.py", "Audio/config.py",
                     "Listener/service.py", "Listener/audio.py", "Listener/capture.py",
                     "Listener/wakeword.py", "Audio/service.py", "Coordinator/service.py",
                     "Face/service.py"):
            path = self.repository / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("# fixture\n")
        self.piper = self.root / "piper"
        self.piper.write_text("fixture")
        self.piper.chmod(0o755)
        self.model = self.root / "voice.onnx"
        self.model.write_text("fixture")
        Path(str(self.model) + ".json").write_text("{}")
        self.report = {"version": [3, 8], "missing": [], "piper": str(self.piper), "model": str(self.model)}
        self.calls = []
        self.output = []
        self.legacy = "loaded"
        self.health_rows = [{"component": name, "state": "ready"} for name in setup.COMPONENTS]
        self.failed_unit = None
        self.listener_exec = ""
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / ".config"),
                                      "DISPLAY": ":0", "XAUTHORITY": str(self.home / ".Xauthority")})
        env.start()
        self.addCleanup(env.stop)
        platform = patch("setup_services.check_platform")
        platform.start()
        self.addCleanup(platform.stop)
        which = patch("setup_services.shutil.which", side_effect=lambda name: "/usr/bin/" + name)
        which.start()
        self.addCleanup(which.stop)

    def fake_run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if "-c" in argv:
            return completed(json.dumps(self.report))
        if argv[0] == "systemctl":
            if argv[2] == "show-environment":
                return completed("DISPLAY=:0\nUNRELATED_SECRET=do-not-print\n")
            if argv[2] == "show":
                if "--property=ExecStart" in argv:
                    return completed(self.listener_exec)
                return completed(self.legacy if argv[3] == "navi-client.service" else
                                 "bad-setting" if argv[3] == self.failed_unit else "loaded")
        if argv[0] == "systemd-run":
            return completed(json.dumps({"v": 1, "components": self.health_rows}),
                             0 if all(row["state"] in ("ready", "busy") for row in self.health_rows) else 1)
        return completed()

    def installer(self, *arguments):
        return setup.Installer(setup.parser().parse_args(list(arguments)), repository=self.repository,
                               home=self.home, run=self.fake_run, output=self.output.append)

    def mutations(self):
        return [argv for argv, _ in self.calls if argv[0] == "systemctl" and
                argv[2] in ("daemon-reload", "disable", "restart", "enable", "start", "stop", "reset-failed")]

    def test_default_installs_all_units_and_disables_legacy_without_starting(self):
        installer = self.installer()
        installer.perform()
        self.assertTrue(all((installer.unit_dir / unit).is_file() for unit in setup.UNITS))
        text = (installer.unit_dir / "navi-face.service").read_text()
        self.assertIn("WorkingDirectory=" + str(self.repository).replace("%", "%%"), text)
        self.assertNotIn('WorkingDirectory="', text)  # systemd 245 treats quotes here literally.
        self.assertIn("ExecStart=" + setup.unit_quote(sys.executable, command=True), text)
        for setting in ("Restart=on-failure", "WatchdogSec=10", "KillMode=control-group", "PartOf=navi.target"):
            self.assertIn(setting, text)
        self.assertIn(["systemctl", "--user", "disable", "--now", "navi-client.service"], self.mutations())
        self.assertFalse(any(argv[2] in ("restart", "enable") for argv in self.mutations()))
        self.assertIn("start client", "\n".join(self.output))
        self.assertNotIn("do-not-print", "\n".join(self.output))

    def test_check_validates_staged_units_without_installed_changes(self):
        installer = self.installer("--check")
        installer.perform()
        self.assertFalse(installer.unit_dir.exists())
        self.assertFalse(installer.env_file.exists())
        self.assertEqual(self.mutations(), [])
        verify = next(argv for argv, _ in self.calls if argv[0] == "systemd-analyze")
        self.assertEqual({Path(value).name for value in verify[3:]}, set(setup.UNITS))

    def test_verify_failure_happens_before_cutover_or_writes(self):
        installer = self.installer("--start")
        original = installer.run
        def fail(argv, **kwargs):
            if argv[0] == "systemd-analyze":
                raise setup.SetupError("invalid unit")
            return original(argv, **kwargs)
        installer.run = fail
        with self.assertRaisesRegex(setup.SetupError, "invalid unit"):
            installer.perform()
        self.assertFalse(installer.unit_dir.exists())
        self.assertEqual(self.mutations(), [])

    def test_listener_can_use_poc_interpreter_without_changing_other_services(self):
        chosen = str(self.root / "wake-env" / "bin" / "python")
        installer = self.installer("--listener-python", chosen, "--wake-threshold", "0.95")
        files = installer.render()
        self.assertIn(setup.unit_quote(chosen, command=True),
                      files[installer.unit_dir / "navi-listener.service"][0].decode())
        self.assertIn(setup.unit_quote(sys.executable, command=True),
                      files[installer.unit_dir / "navi-face.service"][0].decode())
        self.assertIn('NAVI_WAKE_THRESHOLD="0.95"', files[installer.env_file][0].decode())

    def test_named_speaker_settings_survive_reinstall_and_wait_for_boot_device(self):
        installer = self.installer("--speaker-device", "plughw:CARD=Device,DEV=0",
                                   "--speaker-mixer", "Speaker", "--speaker-volume", "46%")
        installer.perform()
        text = installer.env_file.read_text()
        self.assertIn('NAVI_SPEAKER_DEVICE="plughw:CARD=Device,DEV=0"', text)
        self.assertIn('NAVI_SPEAKER_MIXER="Speaker"', text)
        self.assertIn('NAVI_SPEAKER_VOLUME="46%"', text)
        self.assertIn("TimeoutStartSec=45", (installer.unit_dir / "navi-speaker.service").read_text())
        fresh = self.installer()
        fresh.perform()
        self.assertEqual(fresh.env_file.read_text(), text)
        self.assertIsNone(fresh.backup)

    def test_invalid_speaker_volume_never_reaches_installation(self):
        with self.assertRaisesRegex(setup.SetupError, "1% to 100%"):
            self.installer("--speaker-volume", "0%")
        self.assertEqual(self.calls, [])

    def test_explicit_empty_speaker_mixer_disables_mixer_management(self):
        installer = self.installer("--speaker-mixer", "")
        self.assertIn('NAVI_SPEAKER_MIXER=""', installer.environment())

    def test_wake_model_preflight_failure_does_not_mutate_installed_services(self):
        installer = self.installer("--start")
        original = installer.run
        def fail(argv, **kwargs):
            if "Listener.wakeword" in argv:
                raise setup.SetupError("missing ONNX runtime")
            return original(argv, **kwargs)
        installer.run = fail
        with self.assertRaisesRegex(setup.SetupError, "missing ONNX"):
            installer.perform()
        self.assertFalse(installer.unit_dir.exists())
        self.assertEqual(self.mutations(), [])

    def test_plain_reinstall_preserves_effective_listener_interpreter(self):
        chosen = str(self.root / "custom wake env" / "bin" / "python")
        self.listener_exec = "{ path=" + chosen + " ; argv[]=" + chosen + " -m Services listener ; }"
        installer = self.installer("--start")
        installer.perform()
        text = (installer.unit_dir / "navi-listener.service").read_text()
        self.assertIn("ExecStart=" + setup.unit_quote(chosen, command=True), text)
        probe = next(argv for argv, _ in self.calls if "Listener.wakeword" in argv)
        self.assertIn(chosen, probe)
        face = (installer.unit_dir / "navi-face.service").read_text()
        self.assertIn("ExecStart=" + setup.unit_quote(sys.executable, command=True), face)

    def test_explicit_listener_interpreter_overrides_installed_choice(self):
        self.listener_exec = "unrecognized installed command"
        chosen = str(self.root / "explicit-python")
        installer = self.installer("--listener-python", chosen, "--check")
        installer.perform()
        self.assertEqual(installer.listener_python, chosen)
        self.assertFalse(any("--property=ExecStart" in argv for argv, _ in self.calls))

    def test_fresh_install_uses_listener_environment_without_resolving_symlink(self):
        chosen = self.repository / "Listener" / ".venv" / "bin" / "python"
        chosen.parent.mkdir(parents=True)
        chosen.write_text("fixture")
        installer = self.installer("--check")
        installer.perform()
        self.assertEqual(installer.listener_python, str(chosen))
        self.assertEqual(self.mutations(), [])

    def test_unknown_installed_command_requires_explicit_choice_before_mutation(self):
        self.listener_exec = "unexpected format"
        with self.assertRaisesRegex(setup.SetupError, "supply --listener-python"):
            self.installer("--start").perform()
        self.assertEqual(self.mutations(), [])
        self.assertFalse((self.home / ".config" / "systemd" / "user").exists())

    def test_missing_dependency_does_not_change_files_or_legacy_service(self):
        self.report["missing"] = ["moderngl"]
        installer = self.installer("--start")
        with self.assertRaisesRegex(setup.SetupError, "pip install moderngl"):
            installer.perform()
        self.assertFalse(installer.unit_dir.exists())
        self.assertEqual(self.mutations(), [])

    def test_missing_model_metadata_prevents_installation(self):
        Path(str(self.model) + ".json").unlink()
        with self.assertRaisesRegex(setup.SetupError, "voice file missing"):
            self.installer().perform()
        self.assertEqual(self.mutations(), [])

    def test_wrong_python_version_is_actionable(self):
        self.report["version"] = [3, 6]
        with self.assertRaisesRegex(setup.SetupError, "3.8"):
            self.installer().perform()
        self.assertEqual(self.mutations(), [])

    def test_existing_environment_is_preserved_and_explicit_overrides_are_idempotent(self):
        installer = self.installer("--mic-device", "custom:2,0")
        installer.env_file.parent.mkdir(parents=True)
        existing = '# User comment\nDISPLAY=":7"\nNAVI_RUNTIME_DIR="/custom/run"\nNAVI_MIC_DEVICE=old\n'
        installer.env_file.write_text(existing)
        files = installer.render()
        text = files[installer.env_file][0].decode()
        self.assertTrue(text.startswith(existing))
        self.assertNotIn('DISPLAY=":0"', text)
        self.assertIn('NAVI_MIC_DEVICE="custom:2,0"', text)
        self.assertGreater(text.index('NAVI_MIC_DEVICE="custom:2,0"'), text.index('NAVI_MIC_DEVICE=old'))
        installer.env_file.write_bytes(files[installer.env_file][0])
        self.assertEqual(installer.environment(), text)

    def test_changed_files_backed_up_and_unchanged_reinstall_does_not_rewrite(self):
        installer = self.installer()
        installer.unit_dir.mkdir(parents=True)
        face = installer.unit_dir / "navi-face.service"
        face.write_text("previous definition")
        installer.perform()
        self.assertEqual((installer.backup / face.name).read_text(), "previous definition")
        stamp = face.stat().st_mtime_ns
        fresh = self.installer()
        fresh.perform()
        self.assertEqual(face.stat().st_mtime_ns, stamp)
        self.assertIsNone(fresh.backup)

    def test_installed_load_failure_restores_original_files_and_keeps_legacy_running(self):
        installer = self.installer()
        installer.unit_dir.mkdir(parents=True)
        face = installer.unit_dir / "navi-face.service"
        face.write_text("original")
        self.failed_unit = "navi-speaker.service"
        with self.assertRaisesRegex(setup.SetupError, "not loadable"):
            installer.perform()
        self.assertEqual(face.read_text(), "original")
        self.assertFalse((installer.unit_dir / "navi.target").exists())
        self.assertFalse(installer.env_file.exists())
        self.assertFalse(any(argv[2] == "disable" for argv in self.mutations()))

    def test_partial_write_failure_restores_files(self):
        installer = self.installer()
        files = installer.render()
        original = setup.atomic_write
        calls = [0]
        def fail(path, *args):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError("disk full")
            return original(path, *args)
        with patch("setup_services.atomic_write", side_effect=fail), self.assertRaisesRegex(OSError, "disk full"):
            installer.install(files)
        self.assertFalse(any(path.exists() for path in files))

    def test_start_is_one_group_request_and_enable_follows_health(self):
        self.health_rows[1]["state"] = "busy"
        installer = self.installer("--start", "--enable")
        installer.perform()
        actions = [argv for argv, _ in self.calls]
        restart = ["systemctl", "--user", "restart", *setup.UNITS]
        self.assertEqual(actions.count(restart), 1)
        health_index = next(i for i, argv in enumerate(actions) if argv[0] == "systemd-run" and "Services.control" in argv)
        self.assertLess(actions.index(restart), health_index)
        self.assertGreater(actions.index(["systemctl", "--user", "enable", "navi.target"]), health_index)
        probe = actions[health_index]
        self.assertIn("--property=EnvironmentFile=" + str(installer.env_file), probe)
        self.assertIn("--working-directory=" + str(self.repository), probe)
        self.assertIn("--collect", probe)
        self.assertIn("--property=RuntimeMaxSec=10", probe)
        self.assertIn("all four components respond", "\n".join(self.output))

    def test_failed_health_never_enables_or_replays_restart(self):
        self.health_rows[0]["state"] = "unavailable"
        installer = self.installer("--start", "--enable")
        with patch("setup_services.time.monotonic", side_effect=[0, 16]), self.assertRaisesRegex(setup.SetupError, "did not respond"):
            installer.perform()
        self.assertEqual(sum(argv[2] == "restart" for argv in self.mutations()), 1)
        self.assertFalse(any(argv[2] in ("enable", "reset-failed") for argv in self.mutations()))
        self.assertTrue((installer.unit_dir / "navi.target").exists())

    def test_legacy_missing_is_supported(self):
        self.legacy = "not-found"
        self.installer().perform()
        self.assertFalse(any(argv[2] == "disable" for argv in self.mutations()))

    def test_custom_dropins_and_unrelated_files_are_not_modified(self):
        installer = self.installer()
        path = installer.unit_dir / "navi-face.service.d" / "custom.conf"
        path.parent.mkdir(parents=True)
        path.write_text("[Service]\nNice=5\n")
        poc = self.repository / "experiments"
        poc.mkdir()
        (poc / "model.onnx").write_bytes(b"untouched")
        installer.perform()
        self.assertEqual(path.read_text(), "[Service]\nNice=5\n")
        self.assertEqual((poc / "model.onnx").read_bytes(), b"untouched")

    def test_no_display_prevents_start_cutover(self):
        installer = self.installer("--start")
        original = installer.run
        def headless(argv, **kwargs):
            if "show-environment" in argv:
                return completed()
            return original(argv, **kwargs)
        installer.run = headless
        with patch.dict(os.environ, {"DISPLAY": ""}), self.assertRaisesRegex(setup.SetupError, "No desktop DISPLAY"):
            installer.perform()
        self.assertEqual(self.mutations(), [])
        self.assertFalse(installer.unit_dir.exists())

    def test_malformed_health_is_not_success(self):
        installer = self.installer()
        for value in ("not json", "[]", '{"v":1,"components":[null,null,null,null]}',
                      '{"v":1,"components":[]}'):
            with self.subTest(value=value):
                installer.run = lambda *args, **kwargs: completed(value)
                with self.assertRaisesRegex(setup.SetupError, "Health CLI failed"):
                    installer.health()

    def test_environment_control_character_injection_is_rejected(self):
        with self.assertRaisesRegex(setup.SetupError, "control characters"):
            self.installer("--display", ":0\nExecStart=evil")

    def test_unit_symlinks_are_rejected_without_writing(self):
        installer = self.installer()
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaisesRegex(setup.SetupError, "symlink"):
            installer.render()
        self.assertEqual(self.mutations(), [])


class InstallerCliTests(unittest.TestCase):
    def test_platform_guard_does_not_install_on_windows(self):
        with patch("setup_services.sys.platform", "win32"), patch("setup_services.execute") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main([]), 1)
        run.assert_not_called()

    def test_root_guard(self):
        with patch("setup_services.sys.platform", "linux"), patch("setup_services.os.geteuid", return_value=0, create=True):
            with self.assertRaisesRegex(setup.SetupError, "without sudo"):
                setup.check_platform()

    def test_invalid_option_combinations_never_construct_installer(self):
        for args in (["--check", "--start"], ["--enable"], ["--check", "--enable"]):
            with self.subTest(args=args), patch("setup_services.Installer") as installer, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    setup.main(args)
                self.assertEqual(error.exception.code, 2)
                installer.assert_not_called()

    def test_python38_syntax_and_bounded_commands(self):
        ast.parse((ROOT / "setup_services.py").read_text(), feature_version=(3, 8))
        with patch("setup_services.subprocess.run", return_value=completed()) as run:
            setup.execute(["systemctl", "--user", "daemon-reload"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_timeout_is_reported_without_replaying(self):
        with patch("setup_services.subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 30)) as run:
            with self.assertRaisesRegex(setup.SetupError, "Could not complete"):
                setup.execute(["systemctl", "--user", "restart", "navi.target"])
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
