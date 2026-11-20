#!/usr/bin/env python3
"""Kurd Agent: a small, local-first UI for Ollama and OpenAI-compatible models."""
from __future__ import annotations

import json
import hashlib
import base64
import html
import os
import re
import shutil
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("KURD_AGENT_HOME", Path.home() / ".kurd-agent"))
CONFIG_PATH = DATA_DIR / "settings.json"
SECRETS_PATH = DATA_DIR / "secrets.json"
LOCK = threading.Lock()
PULL_JOBS = {}
OAUTH_DEVICE_JOBS = {}
APP_MANIFEST_JOBS = {}
OAUTH_CLIENT_IDS = {
    # GitHub value is the client ID for a GitHub App with device flow enabled, not an OAuth App.
    "github": os.environ.get("KURD_AGENT_GITHUB_APP_CLIENT_ID", ""),
    "huggingface": os.environ.get("KURD_AGENT_HF_OAUTH_CLIENT_ID", ""),
}
GITHUB_APP_SLUG = os.environ.get("KURD_AGENT_GITHUB_APP_SLUG", "")
DEFAULTS = {"provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": "", "temperature": 0.7}
HF_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,120}")
AGENT_TOOLS = [
    {"type": "function", "function": {"name": "github_list_repositories", "description": "List the connected user's most recently updated GitHub repositories.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "github_search_repositories", "description": "Search repositories on GitHub by keywords. This is read-only.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "A short GitHub repository search query"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "github_repository_info", "description": "Get public metadata for a GitHub repository using owner/name.", "parameters": {"type": "object", "properties": {"full_name": {"type": "string", "description": "Repository in owner/name form"}}, "required": ["full_name"]}}},
    {"type": "function", "function": {"name": "github_read_file", "description": "Read a text file from a connected GitHub repository branch.", "parameters": {"type": "object", "properties": {"repo": {"type": "string"}, "path": {"type": "string"}, "branch": {"type": "string"}}, "required": ["repo", "path"]}}},
    {"type": "function", "function": {"name": "github_create_branch", "description": "Create a new non-default feature branch from the repository's default branch. Only do this when the user explicitly asks for repository changes.", "parameters": {"type": "object", "properties": {"repo": {"type": "string"}, "branch": {"type": "string"}}, "required": ["repo", "branch"]}}},
    {"type": "function", "function": {"name": "github_commit_file", "description": "Create or update one text file on an existing non-default feature branch. This creates a Git commit. Use only for a change explicitly requested by the user; never write to the default branch or merge a pull request.", "parameters": {"type": "object", "properties": {"repo": {"type": "string"}, "branch": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}, "message": {"type": "string"}}, "required": ["repo", "branch", "path", "content", "message"]}}},
    {"type": "function", "function": {"name": "github_create_pull_request", "description": "Open a pull request from a feature branch to the repository default branch. Only do this when explicitly requested; never merge it.", "parameters": {"type": "object", "properties": {"repo": {"type": "string"}, "head": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}}, "required": ["repo", "head", "title"]}}},
    {"type": "function", "function": {"name": "github_list_workflows", "description": "List GitHub Actions workflows in a repository.", "parameters": {"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]}}},
    {"type": "function", "function": {"name": "github_run_workflow", "description": "Dispatch a workflow that supports workflow_dispatch. Use only when the user explicitly asks to run it. This starts a workflow run and does not change source code.", "parameters": {"type": "object", "properties": {"repo": {"type": "string"}, "workflow": {"type": "string"}, "ref": {"type": "string"}, "inputs": {"type": "object", "additionalProperties": {"type": "string"}}}, "required": ["repo", "workflow", "ref"]}}},
]


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default.copy()


def write_json_private(path: Path, value):
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    temp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def settings():
    value = DEFAULTS | read_json(CONFIG_PATH, {})
    # Credentials are never returned to the browser.
    value.pop("api_key", None)
    return value


def secrets_data():
    return read_json(SECRETS_PATH, {})


def github_app_secret_data():
    saved = secrets_data()
    if saved.get("github_token") and saved.get("github_auth_type") != "github_app":
        for key in ("github_token", "github_username", "github_refresh_token", "github_token_expires_at", "github_refresh_token_expires_at"):
            saved.pop(key, None)
        write_json_private(SECRETS_PATH, saved)
    return saved


def github_app_config():
    saved = github_app_secret_data()
    client_id = saved.get("github_app_client_id") or OAUTH_CLIENT_IDS.get("github", "")
    client_secret = saved.get("github_app_client_secret", "")
    slug = saved.get("github_app_slug") or GITHUB_APP_SLUG
    installation_id = saved.get("github_app_installation_id")
    return client_id, client_secret, slug, installation_id


def huggingface_oauth_client_id():
    saved = secrets_data()
    return str(saved.get("hf_oauth_client_id") or OAUTH_CLIENT_IDS.get("huggingface", ""))


def safe_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL must start with http:// or https://")
    return url


def request_json(url, *, method="GET", data=None, headers=None, timeout=90):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def request_form_json(url, data, *, headers=None, timeout=30):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


class Handler(BaseHTTPRequestHandler):
    server_version = "KurdAgent/0.1"

    def log_message(self, fmt, *args):
        # Manifest handshakes carry a one-time code in the query string; never log it.
        if urllib.parse.urlparse(self.path).path in ("/api/github/app-manifest/callback", "/api/github/app-installed"):
            return
        super().log_message(fmt, *args)

    def send_json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_html(self, status, content):
        raw = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action https://github.com; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
        return json.loads(self.rfile.read(length) or b"{}")

    def app_manifest_page(self, title, message, install_url=""):
        link = (f'<p><a href="{html.escape(install_url, quote=True)}">Install Kurd Agent and choose All repositories</a></p>'
                if install_url else "")
        return ("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                f"<title>{html.escape(title)}</title><body style='font:16px system-ui;max-width:42rem;margin:3rem auto;padding:1rem'>"
                f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>{link}"
                "<p>You can return to the Kurd Agent app after this step.</p></body></html>")

    def begin_github_app_manifest(self):
        if github_app_config()[0]:
            raise ValueError("A GitHub App is already configured. Use its install or login button.")
        state = secrets.token_urlsafe(24)
        install_state = secrets.token_urlsafe(24)
        callback_base = os.environ.get("KURD_AGENT_CALLBACK_BASE_URL", f"http://127.0.0.1:{os.environ.get('PORT', '8000')}").rstrip("/")
        if not callback_base.startswith("http://127.0.0.1:") and not callback_base.startswith("http://localhost:"):
            raise ValueError("The GitHub App setup callback must use this device's local server.")
        redirect_url = callback_base + "/api/github/app-manifest/callback"
        setup_url = callback_base + "/api/github/app-installed?state=" + urllib.parse.quote(install_state)
        manifest = {
            "name": "Kurd Agent",
            "url": "https://github.com/s1po0/Agent-kurd-",
            "description": "Kurd Agent integration for repository files, pull requests, and GitHub Actions.",
            "redirect_url": redirect_url,
            "setup_url": setup_url,
            "public": False,
            "request_oauth_on_install": False,
            "default_events": [],
            "default_permissions": {
                "contents": "write", "workflows": "write", "actions": "write", "pull_requests": "write"
            },
        }
        expires_at = time.time() + 3600
        with LOCK:
            APP_MANIFEST_JOBS[state] = {"install_state": install_state, "expires_at": expires_at}
        saved = secrets_data()
        saved.update({"github_app_manifest_state": state, "github_app_manifest_install_state": install_state,
                      "github_app_manifest_expires_at": expires_at})
        write_json_private(SECRETS_PATH, saved)
        return {"state": state, "action_url": "https://github.com/settings/apps/new?state=" + urllib.parse.quote(state),
                "manifest": manifest}

    def handle_github_app_manifest_callback(self, query):
        state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        if query.get("error"):
            self.send_html(400, self.app_manifest_page("GitHub App not created", "GitHub did not complete app creation. Return to Kurd Agent and try again."))
            return
        saved = secrets_data()
        persisted_state = str(saved.get("github_app_manifest_state", ""))
        with LOCK:
            pending = APP_MANIFEST_JOBS.get(state)
            valid_state = pending is not None or bool(persisted_state and secrets.compare_digest(state, persisted_state))
            if pending is not None:
                APP_MANIFEST_JOBS.pop(state, None)
        if pending is None and valid_state:
            pending = {"install_state": saved.get("github_app_manifest_install_state", ""),
                       "expires_at": float(saved.get("github_app_manifest_expires_at") or 0)}
        if not valid_state:
            self.send_html(400, self.app_manifest_page("Setup expired", "The GitHub setup link expired or was invalid. Start again from Kurd Agent."))
            return
        if persisted_state and secrets.compare_digest(state, persisted_state):
            for key in ("github_app_manifest_state", "github_app_manifest_install_state", "github_app_manifest_expires_at"):
                saved.pop(key, None)
            write_json_private(SECRETS_PATH, saved)
        if (not pending or time.time() > pending["expires_at"] or
                not re.fullmatch(r"[A-Za-z0-9]{20,100}", code)):
            self.send_html(400, self.app_manifest_page("Setup expired", "The GitHub setup link expired or was invalid. Start again from Kurd Agent."))
            return
        try:
            app = request_json("https://api.github.com/app-manifests/" + urllib.parse.quote(code, safe="") + "/conversions",
                               method="POST", headers={"Accept": "application/vnd.github+json", "User-Agent": "KurdAgent"}, timeout=30)
            client_id = str(app.get("client_id", ""))
            client_secret = str(app.get("client_secret", ""))
            slug = str(app.get("slug", ""))
            if not client_id or not client_secret or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", slug):
                raise ValueError("GitHub did not return a complete App registration.")
            saved = secrets_data()
            for key in ("github_token", "github_username", "github_auth_type", "github_refresh_token",
                        "github_token_expires_at", "github_refresh_token_expires_at", "github_app_installation_id"):
                saved.pop(key, None)
            saved.update({"github_app_client_id": client_id, "github_app_client_secret": client_secret,
                          "github_app_slug": slug, "github_app_install_state": pending["install_state"]})
            # The manifest response also contains a private key and webhook secret; neither is persisted or returned.
            write_json_private(SECRETS_PATH, saved)
            install_url = f"https://github.com/apps/{urllib.parse.quote(slug, safe='-')}/installations/new"
            self.send_html(200, self.app_manifest_page("Kurd Agent GitHub App created",
                "The App was created in your GitHub account. In its Developer settings, enable Device Flow, install it and select All repositories, then return to Kurd Agent to log in.", install_url))
        except Exception:
            self.send_html(502, self.app_manifest_page("Could not finish GitHub setup", "GitHub App creation could not be completed. Return to Kurd Agent and try again."))

    def handle_github_app_install_callback(self, query):
        saved = secrets_data()
        expected = str(saved.get("github_app_install_state", ""))
        state = query.get("state", [""])[0]
        install_id = query.get("installation_id", [""])[0]
        action = query.get("setup_action", [""])[0]
        if (not expected or not secrets.compare_digest(state, expected) or
                not install_id.isdigit() or action not in ("install", "update")):
            self.send_html(400, self.app_manifest_page("Installation not confirmed", "GitHub did not confirm this App installation. Return to Kurd Agent and retry."))
            return
        saved["github_app_installation_id"] = install_id
        write_json_private(SECRETS_PATH, saved)
        self.send_html(200, self.app_manifest_page("Kurd Agent installed",
            "GitHub App installation was saved. Return to Kurd Agent and press GitHub Login to authorize the connection."))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/api/github/app-manifest/start-page":
            try:
                setup = self.begin_github_app_manifest()
                manifest_value = html.escape(json.dumps(setup["manifest"], separators=(",", ":")), quote=True)
                action = html.escape(setup["action_url"], quote=True)
                page = ("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                        "<title>Create Kurd Agent GitHub App</title><body style='font:16px system-ui;max-width:42rem;margin:3rem auto;padding:1rem'>"
                        "<h1>Create Kurd Agent GitHub App</h1><p>Continue to GitHub, create the App, then install it and choose All repositories.</p>"
                        f"<form method='post' action='{action}'><input type='hidden' name='manifest' value='{manifest_value}'>"
                        "<button type='submit'>Continue to GitHub</button></form></body></html>")
                self.send_html(200, page)
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
            return
        if path == "/api/github/app-manifest/callback":
            self.handle_github_app_manifest_callback(query)
            return
        if path == "/api/github/app-installed":
            self.handle_github_app_install_callback(query)
            return
        if path == "/api/oauth/config":
            client_id, _client_secret, slug, installation_id = github_app_config()
            valid_slug = slug if re.fullmatch(r"[A-Za-z0-9-]{1,100}", slug) else ""
            self.send_json(200, {"github_registered": bool(client_id and valid_slug),
                                 "github_installed": bool(installation_id),
                                 "github_ready": bool(client_id and valid_slug and installation_id),
                                 "github_install_url": f"https://github.com/apps/{valid_slug}/installations/new" if valid_slug else "",
                                 "huggingface_ready": bool(huggingface_oauth_client_id())})
            return
        if path == "/api/local-ai/status":
            cfg = read_json(CONFIG_PATH, {})
            local = cfg.get("local_model") or {}
            model_path = DATA_DIR / "models" / str(local.get("local_filename", "")) if local.get("local_filename") else None
            secret = secrets_data()
            hf_token = secret.get("hf_token")
            self.send_json(200, {"provider": cfg.get("provider", "ollama"), "has_hf_token": bool(hf_token),
                                 "hf_username": secret.get("hf_username", ""),
                                 "model_ready": bool(model_path and model_path.is_file()),
                                 "repo": local.get("repo", ""), "filename": local.get("filename", ""),
                                 "model_path": str(model_path) if model_path and model_path.is_file() else ""})
            return
        if path == "/api/local-ai/files":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            repo = (query.get("repo", [""])[0]).strip()
            if not HF_REPO_RE.fullmatch(repo):
                self.send_json(400, {"error": "Enter a Hugging Face repository in owner/name format."})
                return
            try:
                url = "https://huggingface.co/api/models/" + urllib.parse.quote(repo, safe="/") + "/tree/main?recursive=true&expand=true"
                headers = {"User-Agent": "KurdAgent/0.1"}
                if secrets_data().get("hf_token"):
                    headers["Authorization"] = "Bearer " + secrets_data()["hf_token"]
                files = request_json(url, headers=headers, timeout=30)
                gguf = [{"path": item.get("path"), "size": item.get("size", 0)} for item in files
                        if isinstance(item, dict) and str(item.get("path", "")).lower().endswith(".gguf")]
                self.send_json(200, {"files": gguf[:100]})
            except Exception as exc:
                self.send_json(502, {"error": f"Could not list model files. For gated/private repositories, connect a Hugging Face read token. ({exc})"})
            return
        if path == "/api/status":
            cfg = settings()
            github_secret = github_app_secret_data()
            github_expiry = float(github_secret.get("github_token_expires_at") or 0)
            github = bool(github_secret.get("github_token") and github_secret.get("github_auth_type") == "github_app" and (not github_expiry or github_expiry > time.time()))
            saved_cfg = read_json(CONFIG_PATH, {})
            if cfg["provider"] == "android-local":
                local = saved_cfg.get("local_model") or {}
                model_path = DATA_DIR / "models" / str(local.get("local_filename", "")) if local.get("local_filename") else None
                ready = bool(model_path and model_path.is_file())
                models, available, model_error = [], ready, None if ready else "Download a GGUF model in Settings first."
                selected_model = local.get("filename", "")
            else:
                try:
                    models = self.get_models(cfg)
                    available, model_error = True, None
                except Exception as exc:
                    models, available, model_error = [], False, str(exc)
                selected_model = cfg["model"]
            self.send_json(200, {"provider": cfg["provider"], "base_url": cfg["base_url"], "model": selected_model,
                                 "temperature": cfg.get("temperature", 0.7), "has_api_key": bool(saved_cfg.get("api_key")),
                                 "available": available, "model_error": model_error, "models": models,
                                 "github_connected": github})
            return
        if path == "/api/github/state":
            secret = github_app_secret_data()
            expiry = float(secret.get("github_token_expires_at") or 0)
            connected = bool(secret.get("github_token") and secret.get("github_auth_type") == "github_app" and (not expiry or expiry > time.time()))
            self.send_json(200, {"connected": connected, "username": secret.get("github_username", "")})
            return
        if path == "/api/huggingface/search":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0].strip()[:100]
            if len(query) < 2:
                self.send_json(400, {"error": "Type at least two characters to search Hugging Face."})
                return
            try:
                search_url = "https://huggingface.co/api/models?" + urllib.parse.urlencode({"search": query, "filter": "gguf", "sort": "downloads", "direction": "-1", "limit": "12"})
                req = urllib.request.Request(search_url, headers={"User-Agent": "KurdAgent/0.1"})
                with urllib.request.urlopen(req, timeout=20) as response:
                    found = json.loads(response.read())
                self.send_json(200, {"models": [{"id": m.get("id"), "downloads": m.get("downloads", 0), "likes": m.get("likes", 0), "pipeline_tag": m.get("pipeline_tag")}
                                                for m in found if m.get("id")]})
            except Exception as exc:
                self.send_json(502, {"error": f"Hugging Face search failed: {exc}"})
            return
        if path == "/api/models/pull":
            job_id = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id", [""])[0]
            with LOCK:
                job = PULL_JOBS.get(job_id)
            if not job:
                self.send_json(404, {"error": "Download job was not found."})
            else:
                self.send_json(200, {"job": job.copy()})
            return
        if path == "/api/models":
            try:
                self.send_json(200, {"models": self.get_models(settings())})
            except Exception as exc:
                self.send_json(502, {"error": str(exc), "models": []})
            return
        if path == "/api/github/repos":
            github_secret = github_app_secret_data()
            token = github_secret.get("github_token") if github_secret.get("github_auth_type") == "github_app" else None
            if not token:
                self.send_json(401, {"error": "GitHub is not connected yet."})
                return
            try:
                data = self.github_api("/user/repos?sort=updated&per_page=20")
                self.send_json(200, {"repos": [{"name": r.get("full_name"), "description": r.get("description"), "url": r.get("html_url"), "private": r.get("private"), "stars": r.get("stargazers_count", 0)} for r in data]})
            except Exception as exc:
                self.send_json(502, {"error": f"Could not load repositories: {exc}"})
            return
        if path.startswith("/api/"):
            self.send_json(404, {"error": "Unknown API endpoint"})
            return
        self.serve_static(path)

    def get_models(self, cfg):
        if cfg["provider"] == "android-local":
            local = read_json(CONFIG_PATH, {}).get("local_model") or {}
            model_path = DATA_DIR / "models" / str(local.get("local_filename", "")) if local.get("local_filename") else None
            return [str(local.get("filename"))] if model_path and model_path.is_file() else []
        base = safe_url(cfg["base_url"])
        if cfg["provider"] == "ollama":
            data = request_json(base + "/api/tags", timeout=8)
            return [m.get("name") for m in data.get("models", []) if m.get("name")]
        api_base = base if base.endswith("/v1") else base + "/v1"
        api_key = read_json(CONFIG_PATH, {}).get("api_key", "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        data = request_json(api_base + "/models", headers=headers, timeout=8)
        return [m.get("id") for m in data.get("data", []) if m.get("id")]

    def github_access_token(self):
        saved = secrets_data()
        token = saved.get("github_token")
        if token and saved.get("github_auth_type") != "github_app":
            for key in ("github_token", "github_username", "github_refresh_token", "github_token_expires_at"):
                saved.pop(key, None)
            write_json_private(SECRETS_PATH, saved)
            raise ValueError("Previous broad OAuth connection was removed. Reconnect with the GitHub App.")
        if not token:
            raise ValueError("Connect GitHub first to use GitHub agent tools.")
        expires_at = float(saved.get("github_token_expires_at") or 0)
        if expires_at and expires_at <= time.time() + 60:
            refresh_token = saved.get("github_refresh_token")
            client_id, client_secret, _slug, _installation = github_app_config()
            if not refresh_token or not client_id or not client_secret:
                raise ValueError("GitHub sign-in expired. Connect again from the Connections page.")
            result = request_form_json("https://github.com/login/oauth/access_token", {
                "client_id": client_id, "client_secret": client_secret,
                "grant_type": "refresh_token", "refresh_token": refresh_token}, timeout=20)
            if result.get("error") or not result.get("access_token"):
                raise ValueError(result.get("error_description") or "GitHub sign-in expired. Connect again.")
            with LOCK:
                saved = secrets_data()
                saved["github_token"] = result["access_token"]
                if result.get("refresh_token"):
                    saved["github_refresh_token"] = result["refresh_token"]
                saved["github_token_expires_at"] = time.time() + int(result.get("expires_in", 28800)) if result.get("expires_in") else 0
                write_json_private(SECRETS_PATH, saved)
            return result["access_token"]
        return token

    def github_api(self, endpoint, method="GET", data=None):
        token = self.github_access_token()
        return request_json("https://api.github.com" + endpoint, method=method, data=data, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "KurdAgent"}, timeout=30)

    @staticmethod
    def validate_repo_name(repo):
        repo = str(repo or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo):
            raise ValueError("Use a GitHub repository name in owner/repository form.")
        return repo

    @staticmethod
    def validate_git_ref(ref):
        ref = str(ref or "").strip()
        invalid = (not ref or len(ref) > 180 or ref.startswith("/") or ref.endswith("/") or
                   "//" in ref or ".." in ref or "@{" in ref or any(c in ref for c in " ~^:?*[]\\\\"))
        parts = ref.split("/")
        if invalid or any(not part or part.startswith(".") or part.endswith(".") or part.endswith(".lock") for part in parts):
            raise ValueError("Use a valid Git branch or ref name.")
        return ref

    @staticmethod
    def validate_repo_path(path):
        path = str(path or "").strip()
        if (not path or len(path) > 500 or path.startswith("/") or "\\" in path or
                any(ord(c) < 32 for c in path) or any(part in ("", ".", "..") for part in path.split("/"))):
            raise ValueError("Use a valid repository-relative file path.")
        return path

    def github_repo_details(self, repo):
        repo = self.validate_repo_name(repo)
        return self.github_api("/repos/" + urllib.parse.quote(repo, safe="/"))

    def execute_agent_tool(self, name, arguments):
        if name == "github_list_repositories":
            data = self.github_api("/user/repos?sort=updated&per_page=12")
            return [{"name": r.get("full_name"), "description": r.get("description"), "url": r.get("html_url"), "private": r.get("private"), "stars": r.get("stargazers_count", 0)} for r in data]
        if name == "github_search_repositories":
            query = str(arguments.get("query", "")).strip()[:120]
            if not query:
                raise ValueError("A search query is required.")
            data = self.github_api("/search/repositories?" + urllib.parse.urlencode({"q": query, "sort": "stars", "per_page": 6}))
            return [{"name": r.get("full_name"), "description": r.get("description"), "url": r.get("html_url"), "stars": r.get("stargazers_count", 0)} for r in data.get("items", [])]
        if name == "github_repository_info":
            full_name = self.validate_repo_name(arguments.get("full_name"))
            data = self.github_repo_details(full_name)
            return {key: data.get(key) for key in ("full_name", "description", "html_url", "default_branch", "language", "stargazers_count", "forks_count", "open_issues_count", "private", "license")}
        if name == "github_read_file":
            repo = self.validate_repo_name(arguments.get("repo")); path = self.validate_repo_path(arguments.get("path"))
            branch = self.validate_git_ref(arguments.get("branch")) if arguments.get("branch") else self.github_repo_details(repo).get("default_branch")
            item = self.github_api(f"/repos/{urllib.parse.quote(repo, safe='/')}/contents/{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(branch, safe='')}")
            if item.get("type") != "file" or item.get("encoding") != "base64":
                raise ValueError("That path is not a readable text file.")
            raw = base64.b64decode(item.get("content", ""))
            text = raw.decode("utf-8", errors="replace")
            return {"repo": repo, "path": path, "branch": branch, "sha": item.get("sha"), "content": text[:24000], "truncated": len(text) > 24000}
        if name == "github_create_branch":
            repo = self.validate_repo_name(arguments.get("repo")); branch = self.validate_git_ref(arguments.get("branch"))
            details = self.github_repo_details(repo); default_branch = details.get("default_branch")
            if branch == default_branch:
                raise ValueError("Choose a new feature branch; the default branch cannot be overwritten.")
            head = self.github_api(f"/repos/{urllib.parse.quote(repo, safe='/')}/git/ref/heads/{urllib.parse.quote(default_branch, safe='/')}")
            created = self.github_api(f"/repos/{urllib.parse.quote(repo, safe='/')}/git/refs", method="POST",
                                      data={"ref": "refs/heads/" + branch, "sha": head["object"]["sha"]})
            return {"created": created.get("ref"), "sha": created.get("object", {}).get("sha"), "base": default_branch}
        if name == "github_commit_file":
            repo = self.validate_repo_name(arguments.get("repo")); branch = self.validate_git_ref(arguments.get("branch")); path = self.validate_repo_path(arguments.get("path"))
            details = self.github_repo_details(repo)
            if branch == details.get("default_branch"):
                raise ValueError("Direct commits to the default branch are blocked. Create a feature branch first.")
            content = str(arguments.get("content", ""))
            message = str(arguments.get("message", "")).strip()
            if len(content.encode("utf-8")) > 100_000 or not message or len(message) > 200:
                raise ValueError("File content must be under 100 KB and the commit message under 200 characters.")
            ref = urllib.parse.quote(branch, safe="/")
            self.github_api(f"/repos/{urllib.parse.quote(repo, safe='/')}/git/ref/heads/{ref}")
            url = f"/repos/{urllib.parse.quote(repo, safe='/')}/contents/{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(branch, safe='')}"
            try:
                existing = self.github_api(url)
                sha = existing.get("sha")
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                sha = None
            payload = {"message": message, "content": base64.b64encode(content.encode("utf-8")).decode("ascii"), "branch": branch}
            if sha:
                payload["sha"] = sha
            result = self.github_api(f"/repos/{urllib.parse.quote(repo, safe='/')}/contents/{urllib.parse.quote(path, safe='/')}", method="PUT", data=payload)
            commit = result.get("commit", {})
            return {"committed": True, "repo": repo, "branch": branch, "path": path,
                    "commit": commit.get("sha"), "url": commit.get("html_url")}
        if name == "github_create_pull_request":
            repo = self.validate_repo_name(arguments.get("repo")); head = self.validate_git_ref(arguments.get("head"))
            details = self.github_repo_details(repo); base = details.get("default_branch")
            if head == base:
                raise ValueError("The pull request head must be a feature branch, not the default branch.")
            title = str(arguments.get("title", "")).strip()[:200]
            body = str(arguments.get("body", ""))[:5000]
            if not title:
                raise ValueError("A pull request title is required.")
            pr = self.github_api(f"/repos/{urllib.parse.quote(repo, safe='/')}/pulls", method="POST",
                                 data={"title": title, "head": head, "base": base, "body": body})
            return {"number": pr.get("number"), "url": pr.get("html_url"), "state": pr.get("state"), "merged": False}
        if name == "github_list_workflows":
            repo = self.validate_repo_name(arguments.get("repo"))
            data = self.github_api(f"/repos/{urllib.parse.quote(repo, safe='/')}/actions/workflows?per_page=100")
            return [{"id": w.get("id"), "name": w.get("name"), "path": w.get("path"), "state": w.get("state"), "url": w.get("html_url")}
                    for w in data.get("workflows", [])]
        if name == "github_run_workflow":
            repo = self.validate_repo_name(arguments.get("repo")); workflow = str(arguments.get("workflow", "")).strip()
            ref = self.validate_git_ref(arguments.get("ref")); inputs = arguments.get("inputs") or {}
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}(?:\.ya?ml)?", workflow):
                raise ValueError("Use a workflow ID or YAML filename.")
            if not isinstance(inputs, dict) or len(inputs) > 20 or any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 100 or len(v) > 1000 for k, v in inputs.items()):
                raise ValueError("Workflow inputs must be at most 20 short string values.")
            result = self.github_api(f"/repos/{urllib.parse.quote(repo, safe='/')}/actions/workflows/{urllib.parse.quote(workflow, safe='')}/dispatches",
                                     method="POST", data={"ref": ref, "inputs": inputs})
            return {"dispatched": True, "repo": repo, "workflow": workflow, "ref": ref, "result": result or "GitHub accepted the workflow dispatch."}
        raise ValueError("This agent tool is not allowed.")

    def chat_with_tools(self, cfg, messages):
        messages = list(messages)
        github_secret = github_app_secret_data()
        github_expiry = float(github_secret.get("github_token_expires_at") or 0)
        enabled = bool(github_secret.get("github_token") and github_secret.get("github_auth_type") == "github_app" and (not github_expiry or github_expiry > time.time()))
        tools = AGENT_TOOLS if enabled else None
        used = []
        for _ in range(5):
            if cfg["provider"] == "ollama":
                body = {"model": cfg["model"], "messages": messages, "stream": False,
                        "options": {"temperature": cfg.get("temperature", 0.7)}}
                if tools:
                    body["tools"] = tools
                result = request_json(safe_url(cfg["base_url"]) + "/api/chat", method="POST", data=body, timeout=300)
                message = result.get("message", {})
            else:
                base = safe_url(cfg["base_url"])
                api_base = base if base.endswith("/v1") else base + "/v1"
                headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg.get("api_key") else {}
                body = {"model": cfg["model"], "messages": messages, "temperature": cfg.get("temperature", 0.7)}
                if tools:
                    body["tools"] = tools
                result = request_json(api_base + "/chat/completions", method="POST", headers=headers, data=body, timeout=300)
                message = result["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                return str(message.get("content") or ""), used
            messages.append(message)
            for call in calls[:4]:
                fn = call.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                try:
                    output = self.execute_agent_tool(name, args if isinstance(args, dict) else {})
                except Exception as exc:
                    output = {"error": str(exc)}
                used.append(name)
                tool_message = {"role": "tool", "content": json.dumps(output, ensure_ascii=False)[:12000]}
                if call.get("id"):
                    tool_message["tool_call_id"] = call["id"]
                else:
                    tool_message["tool_name"] = name
                messages.append(tool_message)
        return "The agent reached its tool-call limit. Please ask a narrower question.", used

    def start_hf_pull(self, model_name, base_url):
        job_id = secrets.token_urlsafe(9)
        with LOCK:
            active = sum(1 for job in PULL_JOBS.values() if job.get("state") == "running")
            if active >= 2:
                raise ValueError("Two model downloads are already running. Wait for one to finish.")
            PULL_JOBS[job_id] = {"id": job_id, "model": model_name, "state": "running", "status": "Starting download", "percent": 0, "started": threading.current_thread().name}
        thread = threading.Thread(target=self.pull_hf_model, args=(job_id, model_name, base_url), daemon=True)
        thread.start()
        return job_id

    @staticmethod
    def pull_hf_model(job_id, model_name, base_url):
        try:
            req = urllib.request.Request(safe_url(base_url) + "/api/pull", data=json.dumps({"model": model_name, "stream": True}).encode(), method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as response:
                for line in response:
                    if not line.strip():
                        continue
                    update = json.loads(line)
                    with LOCK:
                        job = PULL_JOBS.get(job_id)
                        if not job:
                            return
                        job["status"] = str(update.get("status", "Downloading model"))
                        total, completed = update.get("total"), update.get("completed")
                        if isinstance(total, (int, float)) and total > 0 and isinstance(completed, (int, float)):
                            job["percent"] = min(100, round(completed * 100 / total))
                        if update.get("error"):
                            job["state"] = "error"
                            job["error"] = str(update["error"])
                            return
                        if update.get("status") == "success":
                            job["state"] = "complete"
                            job["percent"] = 100
            with LOCK:
                if PULL_JOBS.get(job_id, {}).get("state") == "running":
                    PULL_JOBS[job_id]["state"] = "complete"
                    PULL_JOBS[job_id]["percent"] = 100
                    PULL_JOBS[job_id]["status"] = "Model ready"
        except Exception as exc:
            with LOCK:
                if job_id in PULL_JOBS:
                    PULL_JOBS[job_id].update({"state": "error", "error": str(exc), "status": "Download failed"})

    def start_local_model_download(self, repo, filename):
        if not HF_REPO_RE.fullmatch(repo):
            raise ValueError("Enter a Hugging Face repository in owner/name format.")
        if not filename or len(filename) > 500 or filename.startswith("/") or ".." in filename.split("/") or not filename.lower().endswith(".gguf"):
            raise ValueError("Choose a valid GGUF file from the repository file list.")
        job_id = secrets.token_urlsafe(9)
        local_name = hashlib.sha256((repo + "/" + filename).encode()).hexdigest()[:20] + ".gguf"
        with LOCK:
            active = sum(1 for job in PULL_JOBS.values() if job.get("state") == "running")
            if active >= 2:
                raise ValueError("Two model downloads are already running. Wait for one to finish.")
            PULL_JOBS[job_id] = {"id": job_id, "model": filename, "state": "running", "status": "Starting model download", "percent": 0}
        thread = threading.Thread(target=self.pull_local_model, args=(job_id, repo, filename, local_name), daemon=True)
        thread.start()
        return job_id, local_name

    @staticmethod
    def pull_local_model(job_id, repo, filename, local_name):
        temp_path = DATA_DIR / "models" / (local_name + ".part")
        final_path = DATA_DIR / "models" / local_name
        try:
            model_dir = DATA_DIR / "models"
            model_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            api_url = "https://huggingface.co/api/models/" + urllib.parse.quote(repo, safe="/") + "/tree/main?recursive=true&expand=true"
            headers = {"User-Agent": "KurdAgent/0.1"}
            token = secrets_data().get("hf_token")
            if token:
                headers["Authorization"] = "Bearer " + token
            with urllib.request.urlopen(urllib.request.Request(api_url, headers=headers), timeout=30) as response:
                available = json.loads(response.read())
            entry = next((item for item in available if isinstance(item, dict) and item.get("path") == filename and filename.lower().endswith(".gguf")), None)
            if not entry:
                raise ValueError("That GGUF file was not found in the repository. Refresh the file list.")
            size = int(entry.get("size") or 0)
            free = shutil.disk_usage(model_dir).free
            if size and free < size + 128 * 1024 * 1024:
                raise ValueError("Not enough free phone storage for this model and temporary download file.")
            download_url = "https://huggingface.co/" + urllib.parse.quote(repo, safe="/") + "/resolve/main/" + urllib.parse.quote(filename, safe="/") + "?download=true"
            request = urllib.request.Request(download_url, headers=headers)
            transferred = 0
            with urllib.request.urlopen(request, timeout=60) as response, temp_path.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    transferred += len(chunk)
                    with LOCK:
                        job = PULL_JOBS.get(job_id)
                        if job:
                            job["percent"] = min(99, round(transferred * 100 / size)) if size else 0
                            job["status"] = f"Downloaded {transferred // (1024 * 1024)} MB"
                output.flush()
                os.fsync(output.fileno())
            if size and transferred != size:
                raise IOError("Model download ended before the full file arrived. Check the connection and retry.")
            temp_path.replace(final_path)
            cfg = read_json(CONFIG_PATH, {})
            cfg["provider"] = "android-local"
            cfg["local_model"] = {"repo": repo, "filename": filename, "local_filename": local_name}
            write_json_private(CONFIG_PATH, cfg)
            # Keep storage bounded: retain the previous model until this replacement is fully downloaded.
            for old_model in model_dir.glob("*.gguf"):
                if old_model.name != local_name:
                    old_model.unlink(missing_ok=True)
            with LOCK:
                PULL_JOBS[job_id].update({"state": "complete", "percent": 100, "status": "GGUF saved on this device", "local_filename": local_name})
        except Exception as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            with LOCK:
                if job_id in PULL_JOBS:
                    PULL_JOBS[job_id].update({"state": "error", "error": str(exc), "status": "Download failed"})

    def start_device_oauth(self, provider):
        if provider == "github":
            client_id, _client_secret, slug, installation_id = github_app_config()
            if not client_id or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", slug):
                raise ValueError("سەرەتا GitHub App ـی Kurd Agent دروست بکە.")
            if not installation_id:
                raise ValueError("سەرەتا GitHub App دابمەزرێنە و All repositories پەسەند بکە.")
            # GitHub App permissions and repository access come from the App installation, not OAuth scopes.
            payload = request_form_json("https://github.com/login/device/code", {
                "client_id": client_id}, timeout=20)
        else:
            client_id = huggingface_oauth_client_id()
            if not client_id:
                raise ValueError("Create a Hugging Face OAuth app and save its public Client ID in Connections first.")
            payload = request_form_json("https://huggingface.co/oauth/device", {
                "client_id": client_id, "scope": "openid profile read-repos gated-repos"}, timeout=20)
        if payload.get("error") or not payload.get("device_code") or not payload.get("user_code"):
            raise ValueError(payload.get("error_description") or payload.get("error") or "OAuth device sign-in could not start.")
        job_id = secrets.token_urlsafe(18)
        interval = max(5 if provider == "github" else 2, int(payload.get("interval", 5)))
        expires_in = max(60, min(1800, int(payload.get("expires_in", 900))))
        with LOCK:
            OAUTH_DEVICE_JOBS[job_id] = {"provider": provider, "client_id": client_id,
                "device_code": payload["device_code"], "interval": interval,
                "expires_at": time.time() + expires_in}
        return {"job_id": job_id, "user_code": payload["user_code"],
                "verification_uri": payload.get("verification_uri") or ("https://github.com/login/device" if provider == "github" else "https://huggingface.co/oauth/device"),
                "expires_in": expires_in, "interval": interval}

    def poll_device_oauth(self, provider, job_id):
        with LOCK:
            job = OAUTH_DEVICE_JOBS.get(job_id)
            if not job or job.get("provider") != provider:
                raise ValueError("Sign-in session expired. Start sign-in again.")
            if time.time() >= job["expires_at"]:
                OAUTH_DEVICE_JOBS.pop(job_id, None)
                raise ValueError("The sign-in code expired. Start again.")
            snapshot = job.copy()
        if provider == "github":
            result = request_form_json("https://github.com/login/oauth/access_token", {
                "client_id": snapshot["client_id"], "device_code": snapshot["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, timeout=20)
        else:
            result = request_form_json("https://huggingface.co/oauth/token", {
                "client_id": snapshot["client_id"], "device_code": snapshot["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, timeout=20)
        error = result.get("error")
        if error == "authorization_pending":
            return {"pending": True, "interval": snapshot["interval"]}
        if error == "slow_down":
            with LOCK:
                if job_id in OAUTH_DEVICE_JOBS:
                    OAUTH_DEVICE_JOBS[job_id]["interval"] += 5
                    snapshot["interval"] = OAUTH_DEVICE_JOBS[job_id]["interval"]
            return {"pending": True, "interval": snapshot["interval"]}
        if error:
            with LOCK:
                OAUTH_DEVICE_JOBS.pop(job_id, None)
            raise ValueError(result.get("error_description") or f"Sign-in was not completed ({error}).")
        access_token = result.get("access_token")
        if not access_token:
            return {"pending": True, "interval": snapshot["interval"]}
        if provider == "github":
            user = request_json("https://api.github.com/user", headers={
                "Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "KurdAgent"}, timeout=20)
            username = user.get("login") or "GitHub user"
            token_key, user_key = "github_token", "github_username"
        else:
            user = request_json("https://huggingface.co/oauth/userinfo", headers={
                "Authorization": f"Bearer {access_token}", "User-Agent": "KurdAgent/0.1"}, timeout=20)
            username = user.get("preferred_username") or user.get("name") or "Hugging Face user"
            token_key, user_key = "hf_token", "hf_username"
        with LOCK:
            saved = secrets_data()
            saved[token_key] = access_token
            saved[user_key] = username
            if provider == "github":
                saved["github_token_expires_at"] = time.time() + int(result.get("expires_in", 28800)) if result.get("expires_in") else 0
                if result.get("refresh_token"):
                    saved["github_refresh_token"] = result["refresh_token"]
                if result.get("refresh_token_expires_in"):
                    saved["github_refresh_token_expires_at"] = time.time() + int(result["refresh_token_expires_in"])
                saved["github_auth_type"] = "github_app"
            if provider == "huggingface" and result.get("refresh_token"):
                saved["hf_refresh_token"] = result["refresh_token"]
                saved["hf_token_expires_at"] = time.time() + int(result.get("expires_in", 0)) if result.get("expires_in") else 0
            write_json_private(SECRETS_PATH, saved)
            OAUTH_DEVICE_JOBS.pop(job_id, None)
        return {"pending": False, "connected": True, "username": username}

    def same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True  # Native local clients may omit Origin; browsers attach it.
        parsed = urllib.parse.urlparse(origin)
        return parsed.netloc.lower() == self.headers.get("Host", "").lower() and parsed.scheme in ("http", "https")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self.same_origin():
            self.send_json(403, {"error": "Cross-origin requests are not allowed"})
            return
        try:
            data = self.body()
            if path == "/api/github/app-manifest/start":
                self.send_json(200, self.begin_github_app_manifest())
                return
            if path == "/api/oauth/huggingface/client-id":
                client_id = str(data.get("client_id", "")).strip()
                if not client_id or not re.fullmatch(r"[A-Za-z0-9._-]{4,255}", client_id):
                    raise ValueError("Enter the public Hugging Face OAuth Client ID (not the Client Secret or an access token).")
                saved = secrets_data()
                saved["hf_oauth_client_id"] = client_id
                write_json_private(SECRETS_PATH, saved)
                self.send_json(200, {"ok": True, "huggingface_ready": True})
                return
            if path == "/api/settings":
                old = read_json(CONFIG_PATH, {})
                provider = data.get("provider", "ollama")
                if provider not in ("ollama", "openai-compatible", "android-local"):
                    raise ValueError("Unsupported model provider")
                cfg = {"provider": provider, "base_url": safe_url(data.get("base_url", DEFAULTS["base_url"])),
                       "model": str(data.get("model", ""))[:200], "temperature": max(0, min(2, float(data.get("temperature", 0.7))))}
                if old.get("local_model"):
                    cfg["local_model"] = old["local_model"]
                # Empty key means keep the previous one; explicit clear_key removes it.
                key = str(data.get("api_key", ""))
                if key:
                    cfg["api_key"] = key
                elif old.get("api_key") and not data.get("clear_key"):
                    cfg["api_key"] = old["api_key"]
                write_json_private(CONFIG_PATH, cfg)
                self.send_json(200, {"ok": True, "settings": settings(), "has_api_key": bool(cfg.get("api_key"))})
                return
            if path == "/api/oauth/device/start":
                provider = str(data.get("provider", ""))
                if provider not in ("github", "huggingface"):
                    raise ValueError("Unknown sign-in provider.")
                self.send_json(200, self.start_device_oauth(provider))
                return
            if path == "/api/oauth/device/poll":
                provider = str(data.get("provider", ""))
                job_id = str(data.get("job_id", ""))
                if provider not in ("github", "huggingface") or not job_id:
                    raise ValueError("Invalid sign-in session.")
                self.send_json(200, self.poll_device_oauth(provider, job_id))
                return
            if path == "/api/local-ai/download":
                repo = str(data.get("repo", "")).strip()
                filename = str(data.get("filename", "")).strip()
                job_id, local_name = self.start_local_model_download(repo, filename)
                self.send_json(202, {"job_id": job_id, "local_filename": local_name})
                return
            if path == "/api/chat":
                cfg = DEFAULTS | read_json(CONFIG_PATH, {})
                messages = data.get("messages", [])
                if not isinstance(messages, list) or not messages or len(messages) > 80:
                    raise ValueError("Send between 1 and 80 chat messages")
                messages = [{"role": m.get("role"), "content": str(m.get("content", ""))[:20000]} for m in messages
                            if isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")]
                if not cfg.get("model"):
                    raise ValueError("Choose a model in Settings first")
                answer, tools_used = self.chat_with_tools(cfg, messages)
                self.send_json(200, {"reply": answer, "tools_used": tools_used})
                return
            if path == "/api/models/pull":
                cfg = DEFAULTS | read_json(CONFIG_PATH, {})
                if cfg.get("provider") != "ollama":
                    raise ValueError("Hugging Face GGUF downloads require the Ollama provider.")
                repo = str(data.get("repo", "")).strip()
                quant = str(data.get("quant", "Q4_K_M")).strip()
                if repo.startswith("hf.co/"):
                    model_name = repo
                    if ":" not in model_name:
                        model_name += ":" + quant
                else:
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,120}", repo):
                        raise ValueError("Enter a Hugging Face repository in owner/name format.")
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,50}", quant):
                        raise ValueError("Invalid quantization tag.")
                    model_name = "hf.co/" + repo + ":" + quant
                if len(model_name) > 260 or not re.fullmatch(r"hf\.co/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,120}:[A-Za-z0-9_.-]{1,50}", model_name):
                    raise ValueError("Invalid Hugging Face GGUF model reference.")
                job_id = self.start_hf_pull(model_name, cfg["base_url"])
                self.send_json(202, {"job_id": job_id, "model": model_name})
                return
            self.send_json(404, {"error": "Unknown API endpoint"})
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(1500).decode("utf-8", "replace")
            except Exception:
                pass
            message = f"Service returned HTTP {exc.code}"
            if exc.code in (401, 403):
                message += " — check the token or API key permissions"
            elif detail:
                message += f": {detail[:350]}"
            self.send_json(502, {"error": message})
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.send_json(502, {"error": f"Could not reach the model service. Check that it is running and the URL is correct. ({exc})"})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc) or "Invalid request"})
        except Exception as exc:
            self.send_json(500, {"error": str(exc) or "Unexpected server error"})

    def do_DELETE(self):
        if not self.same_origin():
            self.send_json(403, {"error": "Cross-origin requests are not allowed"})
            return
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/github/connect", "/api/huggingface/connect"):
            self.send_json(404, {"error": "Unknown API endpoint"})
            return
        saved = secrets_data()
        keys = ("github_token", "github_username", "github_auth_type", "github_refresh_token", "github_token_expires_at",
                "github_refresh_token_expires_at") if path == "/api/github/connect" else (
            "hf_token", "hf_username", "hf_refresh_token", "hf_token_expires_at")
        for key in keys:
            saved.pop(key, None)
        write_json_private(SECRETS_PATH, saved)
        self.send_json(200, {"ok": True})

    def serve_static(self, path):
        if path == "/":
            path = "/index.html"
        target = (ROOT / path.lstrip("/")).resolve()
        if ROOT not in target.parents or not target.is_file():
            self.send_error(404)
            return
        mime = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}.get(target.suffix, "application/octet-stream")
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://github.com")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(raw)


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Kurd Agent is ready at http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
