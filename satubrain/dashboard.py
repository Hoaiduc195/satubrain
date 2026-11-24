"""Serve the authenticated dashboard on the IPv4 loopback interface."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .scope import label
from .store import MAX_BODY, json_bytes

ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8")}
WEB = Path(__file__).with_name("web")


def handler_for(store, workspace, task, all_projects, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SatuBrain"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, value):
            self.send(status, json_bytes(value))

        def scope(self, params):
            project = label(params.get("project", workspace.project), "project")
            if not all_projects and project != workspace.project:
                raise PermissionError
            return project, label(params.get("task", task), "task"), label(
                params.get("branch", workspace.branch), "branch"
            )

        def access(self, api=False, write=False):
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get_all("Host") != [host]:
                return False
            origins = self.headers.get_all("Origin", [])
            if (write or origins) and origins != [f"http://{host}"]:
                return False
            if api:
                keys = self.headers.get_all("X-SatuBrain-Key", [])
                return len(keys) == 1 and secrets.compare_digest(keys[0].encode("utf-8"), token.encode())
            return True

        def do_GET(self):
            try:
                target = urlsplit(self.path)
                if not self.access(api=target.path.startswith("/api/")):
                    return self.send_json(403, {"error": "Dashboard access denied"})
                if target.path in ASSETS:
                    name, content_type = ASSETS[target.path]
                    return self.send(200, (WEB / name).read_bytes(), content_type)
                if target.path == "/api/scopes":
                    return self.send_json(200, {
                        "project": workspace.project, "branch": workspace.branch, "task": task,
                        "scopes": store.scopes(None if all_projects else workspace.project),
                    })
                if target.path == "/api/memories":
                    params = {key: values[0] for key, values in
                              parse_qs(target.query, keep_blank_values=True, max_num_fields=8).items()}
                    rows = store.search(*self.scope(params), query=params.get("query", ""),
                                        kind=params.get("kind") or None, limit=100)
                    return self.send_json(200, {"items": [row | {"text": row["text"][:400]} for row in rows]})
                if target.path == "/api/memory":
                    params = {key: values[0] for key, values in
                              parse_qs(target.query, keep_blank_values=True, max_num_fields=8).items()}
                    row = store.get(*self.scope(params), params.get("id"))
                    return self.send_json(200 if row else 404, {"item": row})
                self.send_json(404, {"error": "Page not found"})
            except PermissionError:
                self.send_json(403, {"error": "Project access denied"})
            except (ValueError, RecursionError):
                self.send_json(400, {"error": "Invalid request"})
            except (OSError, sqlite3.Error):
                self.send_json(500, {"error": "Could not read dashboard data"})

        def do_POST(self):
            # Close after one request to avoid interpreting a rejected body as a new request.
            self.close_connection = True
            try:
                if not self.access(api=True, write=True):
                    return self.send_json(403, {"error": "Dashboard access denied"})
                if self.path not in ("/api/memories", "/api/forget"):
                    return self.send_json(404, {"error": "Page not found"})
                if self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding"):
                    return self.send_json(415, {"error": "Expected a JSON request"})
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                    return self.send_json(411, {"error": "Content-Length required"})
                size = int(lengths[0])
                if not 0 < size <= MAX_BODY:
                    return self.send_json(413, {"error": "Request exceeds the size limit"})
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError("Expected a JSON object")
                project, selected_task, branch = self.scope(payload)
                if self.path == "/api/forget":
                    deleted = store.delete(project, selected_task, branch, payload.get("id"))
                    return self.send_json(200, {"deleted": deleted})
                kind = payload.get("kind")
                if kind not in ("note", "decision", "handoff"):
                    raise ValueError("Choose a note, decision or handoff")
                result = store.save(project=project, task=selected_task, branch=branch, kind=kind,
                                    text=payload.get("text"), agent="dashboard")
                self.send_json(200, result)
            except PermissionError:
                self.send_json(403, {"error": "Project access denied"})
            except (ValueError, RecursionError) as error:
                self.send_json(400, {"error": str(error)})
            except (OSError, sqlite3.Error):
                self.send_json(500, {"error": "Could not update local memory"})

    return Handler


def make_dashboard(store, workspace, task=None, port=0, all_projects=False):
    task = label(task, "task") if task is not None else None
    token = secrets.token_urlsafe(32)
    return ThreadingHTTPServer(("127.0.0.1", port),
                               handler_for(store, workspace, task, all_projects, token)), token


def run_dashboard(store, workspace, task, port, no_browser, all_projects):
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    server, token = make_dashboard(store, workspace, task, port, all_projects)
    with server:
        url = f"http://127.0.0.1:{server.server_port}/#{token}"
        print(url, flush=True)
        if not no_browser:
            webbrowser.open(url)
        server.serve_forever()
