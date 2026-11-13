"""Check that origin metadata survives upload retries without network access."""

import importlib.util
import io
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch
from test_logging import LoggingFixture


class NetworkMetadataTests(LoggingFixture):
    def setUp(self):
        super().setUp()
        self.requests = types.ModuleType("requests")
        self.requests.ConnectionError = type("ConnectionError", (Exception,), {})
        self.requests.Timeout = type("Timeout", (Exception,), {})
        self.requests.post = Mock()
        path = Path(__file__).resolve().parents[1] / "Network/Client.py"
        with patch.dict("sys.modules", {"Logging": self.logs, "requests": self.requests}):
            spec = importlib.util.spec_from_file_location("network_client_test", path)
            self.client = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.client)
        self.client_type = self.client.ServerClient
        self.client = self.client_type(Path(self.directory.name) / "server_ip")
        self.client.transcribe_url = "http://old/transcribe"

    def test_server_cache_survives_restart_without_sharing_instance_state(self):
        self.client._save_server_ip("192.0.2.7")
        restored = self.client_type(self.client.cache_file)
        self.assertEqual(restored.transcribe_url, "http://192.0.2.7:5000/transcribe")
        restored.transcribe_url = "http://changed/transcribe"
        self.assertEqual(self.client.transcribe_url, "http://old/transcribe")

    def test_invalid_cached_address_falls_back_to_discovery(self):
        self.client.cache_file.write_text("not an IP", encoding="utf-8")
        self.assertIsNone(self.client_type(self.client.cache_file).transcribe_url)

    def test_upload_includes_origin(self):
        files = {"file": io.BytesIO(b"audio")}
        response = self.client.send_audio(files, source="microphone", trigger="typed_listen")
        self.assertIs(response, self.requests.post.return_value)
        metadata = self.requests.post.call_args.kwargs["data"]
        self.assertEqual(metadata["source"], "microphone")
        self.assertEqual(metadata["trigger"], "typed_listen")
        self.assertEqual(metadata["client_session_id"], self.logs.SESSION_ID)
        self.assertEqual(metadata["client_name"], self.logs.CLIENT_NAME)
        self.assertEqual(len(metadata["interaction_id"]), 32)

    def test_retry_preserves_origin_and_rewinds_audio(self):
        audio = io.BytesIO(b"audio")
        response = Mock(status_code=200, headers={})
        metadata = []

        def upload(url, **kwargs):
            self.assertEqual(kwargs["data"]["source"], "microphone")
            self.assertEqual(kwargs["data"]["trigger"], "wakeword")
            metadata.append(dict(kwargs["data"]))
            self.assertEqual(audio.read(), b"audio")
            if url == "http://old/transcribe":
                raise self.requests.Timeout("Timed out")
            self.assertEqual(url, "http://new/transcribe")
            return response

        self.requests.post.side_effect = upload
        with patch.object(self.client, "_discover_server") as discover:
            discover.side_effect = lambda: setattr(self.client, "transcribe_url", "http://new/transcribe")
            self.assertIs(self.client.send_audio({"file": audio}, trigger="wakeword"), response)
        self.assertEqual(self.requests.post.call_count, 2)
        self.assertEqual(metadata[0], metadata[1])
        runtime = self.read("navi.log")
        self.assertIn("stage=upload_attempt_1 status=failed elapsed_ms=", runtime)
        self.assertIn("stage=upload_attempt_2 status=completed elapsed_ms=", runtime)
        self.assertIn("interaction=" + metadata[0]["interaction_id"], runtime)


if __name__ == "__main__":
    unittest.main()
