import json
import tempfile
import threading
import unittest
import urllib.parse
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

    def test_huggingface_oauth_client_id_can_be_saved_and_used_for_device_login(self):
        status, result = self.call_api("/api/oauth/huggingface/client-id", {"client_id": "hf-client-public-1234"})
        self.assertEqual(status, 200)
        self.assertTrue(result["huggingface_ready"])
        self.assertEqual(json.loads(self.secrets.read_text())["hf_oauth_client_id"], "hf-client-public-1234")
        status, config = self.call_api("/api/oauth/config")
        self.assertTrue(config["huggingface_ready"])
        self.assertNotIn("huggingface_client_id", config)
        with patch.object(server, "request_form_json", return_value={
                "device_code": "device-code", "user_code": "ABCD-EFGH", "verification_uri": "https://huggingface.co/oauth/device",
                "expires_in": 600, "interval": 5}) as oauth_request:
            status, session = self.call_api("/api/oauth/device/start", {"provider": "huggingface"})
        self.assertEqual(status, 200)
        self.assertEqual(oauth_request.call_args.args[1]["client_id"], "hf-client-public-1234")
        self.assertEqual(session["user_code"], "ABCD-EFGH")

    def test_github_manifest_registration_and_installation_flow(self):
        with urlopen(self.base + "/api/github/app-manifest/start-page", timeout=3) as response:
            self.assertIn("All repositories", response.read().decode())
        status, setup = self.call_api("/api/github/app-manifest/start", {})
        self.assertEqual(status, 200)
        self.assertEqual(setup["action_url"].split("?")[0], "https://github.com/settings/apps/new")
        self.assertEqual(setup["manifest"]["default_permissions"], {
            "contents": "write", "workflows": "write", "actions": "write", "pull_requests": "write"})
        self.assertIn("/api/github/app-installed?state=", setup["manifest"]["setup_url"])
        state = setup["state"]
        req = Request(self.base + "/api/github/app-manifest/callback?" + urllib.parse.urlencode({
            "state": state, "code": "abc123def456ghi789jkl"}))
        with patch.object(server, "request_json", return_value={"client_id": "Iv1.example", "client_secret": "private-secret",
                "slug": "kurd-agent", "pem": "must-not-be-saved", "webhook_secret": "must-not-be-saved"}):
            with urlopen(req, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("Device Flow", response.read().decode())
        saved = json.loads(self.secrets.read_text())
        self.assertEqual(saved["github_app_client_id"], "Iv1.example")
        self.assertEqual(saved["github_app_client_secret"], "private-secret")
        self.assertNotIn("pem", saved)
        install_state = saved["github_app_install_state"]
        with urlopen(self.base + "/api/github/app-installed?" + __import__("urllib.parse").parse.urlencode({
                "state": install_state, "installation_id": "98765", "setup_action": "install"}), timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("Kurd Agent installed", response.read().decode())
        status, config = self.call_api("/api/oauth/config")
        self.assertEqual(status, 200)
        self.assertTrue(config["github_ready"])
        self.assertEqual(json.loads(self.secrets.read_text())["github_app_installation_id"], "98765")

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

    def test_imported_gguf_is_registered_as_android_local_model(self):
        model_dir = Path(self.temp.name) / "models"
        model_dir.mkdir()
        (model_dir / "import_abcd1234.gguf").write_bytes(b"GGUF test file")
        status, result = self.call_api("/api/local-ai/import", {
            "local_filename": "import_abcd1234.gguf", "filename": "my-local-model.gguf"})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        config = json.loads(self.config.read_text())
        self.assertEqual(config["provider"], "android-local")
        self.assertEqual(config["local_model"]["local_filename"], "import_abcd1234.gguf")
        status, models = self.call_api("/api/local-ai/models")
        self.assertEqual(status, 200)
        self.assertEqual(models["models"][0]["local_filename"], "import_abcd1234.gguf")
        self.assertEqual(models["models"][0]["filename"], "my-local-model.gguf")
        status, selected = self.call_api("/api/local-ai/select", {
            "local_filename": "import_abcd1234.gguf", "filename": "my-local-model.gguf"})
        self.assertEqual(status, 200)
        self.assertTrue(selected["ok"])
        status, error = self.call_api("/api/local-ai/select", {
            "local_filename": "../../secrets.json", "filename": "bad.gguf"})
        self.assertEqual(status, 400)
        status, error = self.call_api("/api/local-ai/import", {
            "local_filename": "../secrets.json", "filename": "bad.gguf"})
        self.assertEqual(status, 400)

    def test_huggingface_model_pull_requires_safe_repo_and_uses_ollama_name(self):
        with patch.object(server.Handler, "start_hf_pull", return_value="job-test") as start:
            status, result = self.call_api("/api/models/pull", {"repo": "org/example-GGUF", "quant": "Q4_K_M"})
        self.assertEqual(status, 202)
        self.assertEqual(result["job_id"], "job-test")
        self.assertEqual(start.call_args.args[0], "hf.co/org/example-GGUF:Q4_K_M")
        status, error = self.call_api("/api/models/pull", {"repo": "../../bad", "quant": "Q4_K_M"})
        self.assertEqual(status, 400)

    def test_chat_context_applies_selected_repository_branch_and_thinking(self):
        self.config.write_text(json.dumps({"provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": "test-model"}))
        first = {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "github_read_file", "arguments": {"path": "README.md"}}}]}}
        final = {"message": {"role": "assistant", "content": "Read the file from the selected branch."}}
        with (
            patch.object(server, "secrets_data", return_value={"github_token": "test-token", "github_auth_type": "github_app"}),
            patch.object(server.Handler, "execute_agent_tool", return_value={"content": "ok"}) as execute,
            patch.object(server, "request_json", side_effect=[first, final]) as request,
        ):
            status, result = self.call_api("/api/chat", {
                "messages": [{"role": "system", "content": "system"}, {"role": "user", "content": "Read README"}],
                "context": {"repo": "owner/project", "branch": "feature/docs", "thinking": True}})
        self.assertEqual(status, 200)
        self.assertEqual(result["reply"], "Read the file from the selected branch.")
        self.assertEqual(execute.call_args.args[1], {"path": "README.md", "repo": "owner/project", "branch": "feature/docs"})
        sent = request.call_args_list[0].kwargs["data"]
        self.assertTrue(sent["think"])
        self.assertIn("owner/project", sent["messages"][1]["content"])
        self.assertIn("feature/docs", sent["messages"][1]["content"])

    def test_github_branch_picker_returns_branch_names(self):
        with patch.object(server.Handler, "github_api", return_value=[{"name": "main"}, {"name": "feature/x"}]) as api:
            status, result = self.call_api("/api/github/branches?repo=owner/project")
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in result["branches"]], ["main", "feature/x"])
        self.assertIn("/repos/owner/project/branches?per_page=100", api.call_args.args[0])

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
