"""Local web page: the device registry, who holds what, memory, and cache cleaning."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import pool, system
from .db import connect, tx
from .page import PAGE


class Handler(BaseHTTPRequestHandler):
    server_version = "devlease"

    def log_message(self, *args):
        pass

    @property
    def con(self):
        """One connection per request, closed when the request ends."""
        if getattr(self, "_con", None) is None:
            self._con = connect()
        return self._con

    def finish(self):
        if getattr(self, "_con", None) is not None:
            self._con.close()
        super().finish()

    # The page is local, but any website can make the browser send requests to localhost.
    # A custom header forces a CORS preflight that this server never answers, and the Host
    # check stops DNS rebinding.
    def _trusted(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._trusted():
            return self._send(403, {"error": "bad host"})
        if self.path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        con = self.con
        if self.path == "/api/state":
            data = pool.state(con)
            data["memory"] = system.memory()
            data["device_memory"] = system.device_memory()
            data["top"] = system.top_processes()
            return self._send(200, data)
        if self.path.startswith("/api/caches"):
            fresh = "refresh=1" in self.path
            out = [{"key": k, "label": c["label"], "note": c["note"], "size": system.cache_size(k, 0 if fresh else 3600)}
                   for k, c in system.CACHES.items()]
            return self._send(200, out)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._trusted() or self.headers.get("X-Devlease") != "1":
            return self._send(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        con = self.con
        try:
            if m := re.fullmatch(r"/api/leases/(\d+)/release", self.path):
                return self._send(200, pool.release(con, int(m[1])))
            if m := re.fullmatch(r"/api/requests/(\d+)/(approve|reject)", self.path):
                return self._send(200, pool.decide(con, int(m[1]), m[2] == "approve"))
            if m := re.fullmatch(r"/api/caches/([\w-]+)/clean", self.path):
                if m[1] not in system.CACHES:
                    return self._send(404, {"error": "unknown cache"})
                return self._send(200, system.clean_cache(m[1], bool(body.get("force"))))
            if self.path == "/api/sync":
                for kind in pool.KINDS:
                    try:
                        pool.sync(con, kind, force=True)
                    except Exception as e:
                        return self._send(500, {"error": f"{kind}: {e}"})
                return self._send(200, {"status": "ok"})
        except pool.BadRequest as e:
            return self._send(400, {"error": str(e)})
        self._send(404, {"error": "not found"})


def make_server(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int) -> None:
    server = make_server(port)
    print(f"devlease: http://127.0.0.1:{server.server_address[1]}", flush=True)
    threading.Thread(target=_warm, daemon=True).start()
    server.serve_forever()


def _warm() -> None:
    for kind in pool.KINDS:
        try:
            pool.sync(connect(), kind)
        except Exception:
            pass
