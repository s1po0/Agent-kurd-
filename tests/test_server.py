import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


class ServerHelpersTest(unittest.TestCase):
    def test_safe_url_accepts_http_and_https(self):
        self.assertEqual(server.safe_url(" http://localhost:11434/ "), "http://localhost:11434")
        self.assertEqual(server.safe_url("https://example.test/api"), "https://example.test/api")

    def test_safe_url_rejects_invalid_schemes(self):
        for value in ("localhost:11434", "file:///etc/passwd", "javascript:alert(1)", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.safe_url(value)

    def test_private_json_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            with patch.object(server, "DATA_DIR", Path(folder)):
                server.write_json_private(path, {"model": "qwen2.5:7b"})
            self.assertEqual(json.loads(path.read_text()), {"model": "qwen2.5:7b"})
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_settings_redact_api_key(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "settings.json"
            config.write_text(json.dumps({"provider": "openai-compatible", "api_key": "do-not-return", "model": "test"}))
            with patch.object(server, "CONFIG_PATH", config):
                result = server.settings()
            self.assertNotIn("api_key", result)
            self.assertEqual(result["model"], "test")


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name)
        self.config = folder / "settings.json"
        self.secrets = folder / "secrets.json"
        for name, value in (("DATA_DIR", folder), ("CONFIG_PATH", self.config), ("SECRETS_PATH", self.secrets)):
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.temp.cleanup)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def call_api(self, path, data=None, method=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        req = Request(self.base + path, data=body, method=method or ("POST" if body is not None else "GET"),
                      headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_settings_api_stores_secret_but_does_not_return_it(self):
        status, result = self.call_api("/api/settings", {"provider": "openai-compatible", "base_url": "http://localhost:1234/v1",
            "model": "test-model", "api_key": "secret-value"})
        self.assertEqual(status, 200)
        self.assertTrue(result["has_api_key"])
        self.assertNotIn("api_key", result["settings"])
        self.assertEqual(json.loads(self.config.read_text())["api_key"], "secret-value")

    def test_chat_proxies_to_ollama(self):
        self.config.write_text(json.dumps({"provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": "local-test"}))
        with patch.object(server, "request_json", return_value={"message": {"content": "باشە"}}) as request:
            status, result = self.call_api("/api/chat", {"messages": [{"role": "user", "content": "سڵاو"}]})
        self.assertEqual(status, 200)
        self.assertEqual(result["reply"], "باشە")
        self.assertEqual(request.call_args.args[0], "http://127.0.0.1:11434/api/chat")

    def test_cross_origin_settings_write_is_rejected(self):
        status, result = self.call_api("/api/settings", {"model": "bad"}, headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        self.assertNotIn("model", json.loads(self.config.read_text()) if self.config.exists() else {})

    def test_huggingface_model_pull_requires_safe_repo_and_uses_ollama_name(self):
        with patch.object(server.Handler, "start_hf_pull", return_value="job-test") as start:
            status, result = self.call_api("/api/models/pull", {"repo": "org/example-GGUF", "quant": "Q4_K_M"})
        self.assertEqual(status, 202)
        self.assertEqual(result["job_id"], "job-test")
        self.assertEqual(start.call_args.args[0], "hf.co/org/example-GGUF:Q4_K_M")
        status, error = self.call_api("/api/models/pull", {"repo": "../../bad", "quant": "Q4_K_M"})
        self.assertEqual(status, 400)

    def test_agent_uses_connected_read_only_github_tools(self):
        self.config.write_text(json.dumps({"provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": "tool-model"}))
        first = {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "github_list_repositories", "arguments": {}}}]}}
        final = {"message": {"role": "assistant", "content": "I found your repositories."}}
        with (
            patch.object(server, "secrets_data", return_value={"github_token": "test-token"}),
            patch.object(server.Handler, "execute_agent_tool", return_value=[{"name": "org/repo"}]),
            patch.object(server, "request_json", side_effect=[first, final]) as request,
        ):
            status, result = self.call_api("/api/chat", {"messages": [{"role": "user", "content": "List my repos"}]})
        self.assertEqual(status, 200)
        self.assertEqual(result["reply"], "I found your repositories.")
        self.assertEqual(result["tools_used"], ["github_list_repositories"])
        followup = request.call_args_list[1].kwargs["data"]["messages"]
        self.assertTrue(any(message["role"] == "tool" for message in followup))


if __name__ == "__main__":
    unittest.main()
