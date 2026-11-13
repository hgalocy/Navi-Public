"""Listener environment/asset preparation without networking or package installs."""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from Listener import setup_env
from Listener.wakeword import ASSETS


class ListenerSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Listener"
        (self.root / "models").mkdir(parents=True)
        self.model = self.root / "models/hey_navi_v3.onnx"
        self.model.write_bytes(b"unchanged custom model fixture")
        self.feature = b"verified feature fixture"
        self.digest = hashlib.sha256(self.feature).hexdigest()
        fixture = patch.multiple(setup_env, ROOT=self.root, ENV=self.root / ".venv",
                                 MODEL_SHA256=hashlib.sha256(self.model.read_bytes()).hexdigest(),
                                 FEATURES={"melspectrogram.onnx": self.digest})
        fixture.start()
        self.addCleanup(fixture.stop)

    def test_production_v3_model_and_sidecar_remain_unchanged_in_listener(self):
        expected = "68abe08532063dca36a16d7630d6334001ebe8765e08e7b4b8cb2d7d061b3b3e"
        self.assertEqual(ASSETS, Path(__file__).resolve().parents[1] / "Listener/models")
        setup_env.verify(ASSETS / "hey_navi_v3.onnx", expected)
        self.assertEqual((ASSETS / "hey_navi_v3.sha256").read_text().split()[0], expected)

    def test_invalid_custom_model_prevents_environment_install_and_download(self):
        self.model.write_bytes(b"tampered model")
        with patch.object(setup_env, "check_host"), patch.object(setup_env, "install_environment") as install, \
                patch.object(setup_env.urllib.request, "urlopen") as download, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(setup_env.main([]), 1)
        install.assert_not_called()
        download.assert_not_called()

    def test_verified_legacy_feature_is_copied_without_moving_existing_files(self):
        cached = self.root.parent / "wakeword_poc/models/melspectrogram.onnx"
        cached.parent.mkdir(parents=True)
        cached.write_bytes(self.feature)
        with patch.object(setup_env.urllib.request, "urlopen") as download:
            setup_env.prepare_assets()
        download.assert_not_called()
        self.assertEqual(cached.read_bytes(), self.feature)
        self.assertEqual((self.root / "models/melspectrogram.onnx").read_bytes(), self.feature)

    def test_bad_download_never_publishes_an_unverified_feature(self):
        with patch.object(setup_env.urllib.request, "urlopen", return_value=io.BytesIO(b"bad download")):
            with self.assertRaisesRegex(RuntimeError, "invalid Listener asset"):
                setup_env.prepare_assets()
        self.assertFalse((self.root / "models/melspectrogram.onnx").exists())
        self.assertEqual(list((self.root / "models").glob(".feature-*")), [])

    def test_check_only_does_not_install_or_download_or_open_a_microphone(self):
        (self.root / "models/melspectrogram.onnx").write_bytes(self.feature)
        with patch.object(setup_env, "check_host"), patch.object(setup_env, "install_environment") as install, \
                patch.object(setup_env, "prepare_assets") as download, \
                patch.object(setup_env.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup_env.main(["--check"]), 0)
        install.assert_not_called()
        download.assert_not_called()
        self.assertEqual(run.call_args.args[0],
                         [str(self.root / ".venv/bin/python"), "-B", "-m", "Listener.wakeword"])
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.root.parent))

    def test_inherited_pip_settings_cannot_redirect_the_listener_install(self):
        with patch.dict(os.environ, {"PIP_TARGET": "outside", "PIP_PREFIX": "outside", "PIP_USER": "1",
                                     "PYTHONPATH": "outside"}), \
                patch.object(setup_env.venv, "EnvBuilder"), patch.object(setup_env.subprocess, "run") as run:
            setup_env.install_environment()
        for command in run.call_args_list:
            environment = command.kwargs["env"]
            for key in ("PIP_TARGET", "PIP_PREFIX", "PIP_USER", "PYTHONPATH"):
                self.assertNotIn(key, environment)
            self.assertEqual(environment["PIP_REQUIRE_VIRTUALENV"], "true")
            self.assertEqual(command.args[0][0], str(self.root / ".venv/bin/python"))
