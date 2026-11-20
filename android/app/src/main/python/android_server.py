"""Start the existing Kurd Agent Python API inside the Android app process."""
import os
import sys
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

_SERVER = None
_SERVER_LOCK = threading.Lock()


def start_server(app_files_dir, hf_oauth_client_id=""):
    global _SERVER
    with _SERVER_LOCK:
        if _SERVER is not None:
            return "http://127.0.0.1:8000/"
        app_dir = Path(app_files_dir) / "kurd-agent"
        web_dir = app_dir / "www"
        os.environ["KURD_AGENT_HOME"] = str(app_dir / "data")
        os.environ["HOST"] = "127.0.0.1"
        os.environ["PORT"] = "8000"
        sys.path.insert(0, str(web_dir))

        import server as kurd_server
        kurd_server.ROOT = web_dir
        kurd_server.OAUTH_CLIENT_IDS["huggingface"] = hf_oauth_client_id
        _SERVER = ThreadingHTTPServer(("127.0.0.1", 8000), kurd_server.Handler)
        thread = threading.Thread(target=_SERVER.serve_forever, name="kurd-agent-http", daemon=True)
        thread.start()
    return "http://127.0.0.1:8000/"
