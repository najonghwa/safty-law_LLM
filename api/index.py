# -*- coding: utf-8 -*-
"""index.py — 모든 /api/* 요청을 받는 단일 서버리스 함수.

엔드포인트를 파일별로 쪼개면 함수마다 37MB DB가 복사되어 배포가 무거워진다.
그래서 한 함수에서 경로로 분기한다(vercel.json의 rewrites가 여기로 모아준다).

  GET  /api/stats                 DB 현황
  GET  /api/laws                  법령 목록(그룹별)
  GET  /api/law?law_id=           한 법령의 조문 목차
  GET  /api/article?id=           조문 원문
  GET  /api/search?q=&tier=       키워드 검색
  GET  /api/substance?q=          물질(CAS/이름) 규제 목록 대조
  POST /api/ask   {"q": "..."}    AI 질의(근거 조문 기반)
"""
import os, sys, json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _lib
import _rag


def route(path, qs, body):
    if path.endswith("/stats"):
        d = _lib.api_stats()
        d["llm"] = _rag.llm_ready()
        return d
    if path.endswith("/laws"):
        return _lib.api_laws()
    if path.endswith("/law"):
        return _lib.api_law_articles(int(qs.get("law_id", ["0"])[0]))
    if path.endswith("/article"):
        a = _lib.api_article(int(qs.get("id", ["0"])[0]))
        return a if a else {"error": "not found"}
    if path.endswith("/search"):
        return _lib.api_search(qs.get("q", [""])[0], qs.get("tier", [""])[0])
    if path.endswith("/substance"):
        return _lib.api_substance(qs.get("q", [""])[0])
    if path.endswith("/review_topics"):
        return [{"id": t["id"], "label": t["label"]} for t in _rag.REVIEW_TOPICS]
    if path.endswith("/ask") or path.endswith("/review"):
        if path.endswith("/review"):
            q = _rag.build_review_question(body.get("topic", ""), body.get("product") or {})
        else:
            q = (body.get("q") or qs.get("q", [""])[0]).strip()
        if not q:
            return {"mode": "error", "reason": "질문이 비어 있습니다.", "sources": []}
        plan = _rag.plan_query(q)
        fts = _lib.search_for_question(q, plan=plan)
        subs = _lib.find_substance_rows(q, plan.get("substance"), plan.get("cas"))
        d = _rag.ask(q, fts, subs)
        d["plan"] = plan
        return d
    return {"error": "unknown endpoint", "path": path}


class handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self, body=None):
        u = urlparse(self.path)
        try:
            self._send(route(u.path.rstrip("/"), parse_qs(u.query), body or {}))
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            body = {}
        self._handle(body)

    def log_message(self, *a):
        pass
