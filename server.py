#!/usr/bin/env python3
"""Kurd Agent: a small, local-first UI for Ollama and OpenAI-compatible models."""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import secrets
import threading
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
DEFAULTS = {"provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": "", "temperature": 0.7}
HF_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,120}")
AGENT_TOOLS = [
    {"type": "function", "function": {"name": "github_list_repositories", "description": "List the connected user's most recently updated GitHub repositories.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "github_search_repositories", "description": "Search repositories on GitHub by keywords. This is read-only.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "A short GitHub repository search query"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "github_repository_info", "description": "Get public metadata for a GitHub repository using owner/name.", "parameters": {"type": "object", "properties": {"full_name": {"type": "string", "description": "Repository in owner/name form"}}, "required": ["full_name"]}}},
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


class Handler(BaseHTTPRequestHandler):
    server_version = "KurdAgent/0.1"

    def log_message(self, fmt, *args):
        # Keep logs useful but never print request bodies or credentials.
        super().log_message(fmt, *args)

    def send_json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/local-ai/status":
            cfg = read_json(CONFIG_PATH, {})
            local = cfg.get("local_model") or {}
            model_path = DATA_DIR / "models" / str(local.get("local_filename", "")) if local.get("local_filename") else None
            hf_token = secrets_data().get("hf_token")
            self.send_json(200, {"provider": cfg.get("provider", "ollama"), "has_hf_token": bool(hf_token),
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
            github = bool(secrets_data().get("github_token"))
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
            self.send_json(200, {"connected": bool(secrets_data().get("github_token"))})
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
            token = secrets_data().get("github_token")
            if not token:
                self.send_json(401, {"error": "GitHub is not connected yet."})
                return
            try:
                data = request_json("https://api.github.com/user/repos?sort=updated&per_page=20", headers={
                    "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "KurdAgent"
                })
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

    def github_api(self, endpoint):
        token = secrets_data().get("github_token")
        if not token:
            raise ValueError("Connect GitHub first to use GitHub agent tools.")
        return request_json("https://api.github.com" + endpoint, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "KurdAgent"}, timeout=20)

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
            full_name = str(arguments.get("full_name", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", full_name):
                raise ValueError("Use repository format owner/name.")
            data = self.github_api("/repos/" + urllib.parse.quote(full_name, safe="/"))
            return {key: data.get(key) for key in ("full_name", "description", "html_url", "default_branch", "language", "stargazers_count", "forks_count", "open_issues_count", "private", "license")}
        raise ValueError("This agent tool is not allowed.")

    def chat_with_tools(self, cfg, messages):
        messages = list(messages)
        enabled = bool(secrets_data().get("github_token"))
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
            if path == "/api/huggingface/connect":
                token = str(data.get("token", "")).strip()
                if not token or len(token) > 1000:
                    raise ValueError("Enter a Hugging Face user access token.")
                user = request_json("https://huggingface.co/api/whoami-v2", headers={
                    "Authorization": f"Bearer {token}", "User-Agent": "KurdAgent/0.1"}, timeout=20)
                with LOCK:
                    saved = secrets_data()
                    saved["hf_token"] = token
                    write_json_private(SECRETS_PATH, saved)
                self.send_json(200, {"ok": True, "username": user.get("name") or user.get("fullname") or "Hugging Face user"})
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
            if path == "/api/github/connect":
                token = str(data.get("token", "")).strip()
                if not token or len(token) > 500:
                    raise ValueError("Enter a valid GitHub personal access token")
                user = request_json("https://api.github.com/user", headers={"Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json", "User-Agent": "KurdAgent", "X-GitHub-Api-Version": "2022-11-28"}, timeout=15)
                with LOCK:
                    saved = secrets_data()
                    saved["github_token"] = token
                    write_json_private(SECRETS_PATH, saved)
                self.send_json(200, {"ok": True, "login": user.get("login", "GitHub user")})
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
        saved.pop("github_token" if path == "/api/github/connect" else "hf_token", None)
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
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
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
