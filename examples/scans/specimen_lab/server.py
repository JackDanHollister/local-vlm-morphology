"""Loopback viewer with explicit bounded demo start/stop, no raw motor routes."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

WEB = Path(__file__).with_name("web")


def handler(store, demos=None, *, allow_live=True):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, body, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def json(self, obj, status=200):
            self.respond(status, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode())

        def allowed_host(self):
            port = self.server.server_address[1]
            return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def do_GET(self):
            if not self.allowed_host():
                self.json({"error": "Loopback host required"}, 403)
                return
            path = urlsplit(self.path).path
            try:
                if path == "/api/snapshot":
                    self.json(store.snapshot())
                elif path == "/api/graph":
                    self.json(store.graph())
                elif path == "/api/export.jsonld":
                    self.json(store.jsonld())
                elif path == "/api/demos":
                    from .providers import catalog
                    self.json({"jobs":demos.list() if demos else store.snapshot()['demos'],"providers":catalog(),"enabled":demos is not None,
                               "allow_live":allow_live,"live_blocked":not allow_live or (store.root/'STOP').exists()})
                elif path.startswith("/api/images/"):
                    file = store.image_path(path.removeprefix("/api/images/"), verify=True)
                    self.respond(200, file.read_bytes(), mimetypes.guess_type(file)[0])
                elif path in {"/", "/app.js", "/style.css"}:
                    file = WEB / ({"/": "index.html"}.get(path, path[1:]))
                    self.respond(200, file.read_bytes(), mimetypes.guess_type(file)[0]+"; charset=utf-8")
                else:
                    self.json({"error": "Not found"}, 404)
            except (ValueError, OSError):
                self.json({"error": "Evidence unavailable or failed integrity check"}, 404)

        def do_POST(self):
            origin = self.headers.get("Origin")
            expected = "http://" + self.headers.get("Host", "")
            if not self.allowed_host() or origin != expected:
                self.json({"error": "Same-origin request required"}, 403)
                return
            if self.path not in {"/api/reviews","/api/demos/start","/api/demos/stop"}:
                self.json({"error": "Not found"}, 404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 12000 or self.headers.get("Content-Type") != "application/json":
                    raise ValueError("Expected bounded JSON request")
                data = json.loads(self.rfile.read(size))
                if self.path=="/api/demos/start":
                    if demos is None:raise ValueError('Demo controller unavailable')
                    required={'specimen_id','provider_id','source','mode','max_views','attended'}
                    if not isinstance(data,dict) or not required<=set(data) or set(data)-required-{'resume_job_id','analysis_workers'}:
                        raise ValueError('Unexpected demo fields')
                    if not allow_live and data['source']!='recorded':
                        raise ValueError('This server only accepts saved images')
                    self.json(demos.start(**data),202)
                    return
                if self.path=="/api/demos/stop":
                    if demos is None or not isinstance(data,dict) or set(data)!={'job_id'}:raise ValueError('Invalid stop request')
                    self.json(demos.stop(data['job_id']))
                    return
                if set(data) != {"run_id", "feature_id", "verdict", "note"}:
                    raise ValueError("Unexpected review fields")
                self.json(store.review(**data), 201)
            except (ValueError, TypeError, KeyError) as exc:
                self.json({"error": str(exc)}, 400)
    return Handler


def serve(store, port=8765, *, allow_live=True, view_only=False):
    demos=None
    if not view_only:
        from .demo import DemoManager
        demos=DemoManager(store,allow_live=allow_live)
    with ThreadingHTTPServer(("127.0.0.1", port), handler(store,demos,allow_live=allow_live)) as server:
        print(f"Specimen Lab: http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
