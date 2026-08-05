# -*- coding: utf-8 -*-
"""dev_server.py — 로컬에서 Vercel과 같은 라우팅으로 띄워 보는 개발 서버.

배포 전 확인용. Vercel에서는 public/이 정적 서빙되고 /api/*가 api/index.py로
모이는데, 이 파일이 그 구조를 그대로 흉내 낸다.

  set OPENAI_API_KEY=sk-...   (PowerShell: $env:OPENAI_API_KEY="sk-...")
  python dev_server.py        → http://localhost:8788
"""
import os, sys, io, json
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "api"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
import index as api  # noqa: E402


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(BASE, "public"), **kw)

    def _api(self, body=None):
        u = urlparse(self.path)
        try:
            out = api.route(u.path.rstrip("/"), parse_qs(u.query), body or {})
            code = 200
        except Exception as e:
            out, code = {"error": f"{type(e).__name__}: {e}"}, 500
        payload = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._api()
        super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            body = {}
        self._api(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8788
    print(f"http://localhost:{port}  (LLM key: {'설정됨' if os.environ.get('OPENAI_API_KEY') else '없음'})")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
