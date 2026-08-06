# -*- coding: utf-8 -*-
"""_lib.py — 안전법규 검색 공통 로직 (Vercel 서버리스용).

표준 라이브러리만 사용한다(sqlite3 · urllib · json). 외부 패키지가 없으므로
Vercel 함수 번들이 가볍고 콜드 스타트가 빠르다.

DB는 읽기 전용이라 서버리스에서 파일로 함께 배포해도 문제가 없다.
OpenAI 키는 환경변수 OPENAI_API_KEY 로 주입한다(코드에 넣지 않는다).
"""
import os, re, json, sqlite3, urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE, "data", "laws.db")
SYN_PATH = os.path.join(BASE, "data", "synonyms.json")

OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
CHAT_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
CHAT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
TOP_K = 8

_FTS_OK = None          # trigram FTS 사용 가능 여부 (환경마다 다를 수 있어 1회 확인)
_SYN = None


# ── DB ──────────────────────────────────────────────────────────
def db():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def fts_available(con):
    """이 실행 환경의 SQLite가 FTS5(trigram) 질의를 처리할 수 있는지 확인."""
    global _FTS_OK
    if _FTS_OK is None:
        try:
            con.execute("SELECT rowid FROM articles_fts WHERE articles_fts MATCH ? LIMIT 1",
                        ('"안전"',)).fetchone()
            _FTS_OK = True
        except Exception:
            _FTS_OK = False
    return _FTS_OK


def load_synonyms():
    global _SYN
    if _SYN is None:
        try:
            with open(SYN_PATH, encoding="utf-8") as f:
                _SYN = json.load(f)["synonyms"]
        except Exception:
            _SYN = {}
    return _SYN


# ── 검색 ────────────────────────────────────────────────────────
def expand_query(q):
    variants, notes = [q], []
    for term, subs in load_synonyms().items():
        if term in q:
            notes.append({"term": term, "to": subs})
            for s in subs:
                v = q.replace(term, s)
                if v not in variants:
                    variants.append(v)
    return variants[:8], notes


def rank_source(r):
    """근거 우선순위: 법률 조문 > 시행령·규칙 > 별표 > KOSHA 지침.

    같은 등급 안의 순서는 건드리지 않는다(검색어별 라운드로빈 순서를 그대로 살린다).
    """
    tier, label = r.get("tier", ""), r.get("jo_label", "")
    if tier == "KOSHA":
        return 3
    if label.startswith("별표") or label.startswith("별지"):
        return 2
    return 0 if tier == "법률" else 1


def _search_one(con, q, tier="", limit=60):
    terms = [t for t in re.split(r"\s+", q) if t]
    fts_terms = [t for t in terms if len(t) >= 3]
    short = [t for t in terms if 0 < len(t) < 3]
    tier_sql = " AND l.tier=?" if tier else ""
    rows = []
    if fts_terms and fts_available(con):
        match = " AND ".join('"' + t.replace('"', "") + '"' for t in fts_terms)
        short_sql = " AND a.content LIKE ?" * len(short)
        sql = ("SELECT a.article_id, a.jo_label, a.jo_short, a.title, a.chapter,"
               " l.name law_name, l.tier, l.kind,"
               " snippet(articles_fts, 2, '<<', '>>', ' … ', 26) snip"
               " FROM articles_fts f JOIN articles a ON a.article_id=f.rowid"
               " JOIN laws l ON l.law_id=a.law_id"
               " WHERE articles_fts MATCH ?" + short_sql + tier_sql +
               " ORDER BY bm25(articles_fts, 8.0, 4.0, 1.0) LIMIT ?")
        params = [match] + [f"%{t}%" for t in short] + ([tier] if tier else []) + [limit]
        try:
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        except sqlite3.OperationalError:
            rows = []
    if not rows:  # FTS 불가 환경 또는 무결과 → LIKE 폴백
        conds = " AND ".join(["a.content LIKE ?"] * max(len(terms), 1))
        sql = ("SELECT a.article_id, a.jo_label, a.jo_short, a.title, a.chapter,"
               " l.name law_name, l.tier, l.kind,"
               " substr(a.content, 1, 200) snip"
               " FROM articles a JOIN laws l ON l.law_id=a.law_id"
               f" WHERE {conds}" + tier_sql + " LIMIT ?")
        params = [f"%{t}%" for t in (terms or [q])] + ([tier] if tier else []) + [limit]
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    for r in rows:
        r["deep_link"] = deep_link(r)
    return rows


def deep_link(r):
    name = r.get("law_name", "")
    if r.get("kind") == "kosha":
        return ""
    import urllib.parse
    base = "https://www.law.go.kr/" + urllib.parse.quote(
        ("행정규칙/" if r.get("kind") == "admrul" else "법령/") + name.replace(" ", ""))
    if r.get("kind") == "admrul":
        return base
    return base + "/" + urllib.parse.quote(r.get("jo_short", ""))


def api_search(q, tier="", limit=60):
    q = (q or "").strip()
    if not q:
        return {"query": q, "results": []}
    con = db()
    variants, notes = expand_query(q)
    seen, merged = set(), []
    for v in variants:
        for r in _search_one(con, v, tier, limit):
            if r["article_id"] not in seen:
                seen.add(r["article_id"])
                merged.append(r)
    con.close()
    return {"query": q, "results": merged[:limit], "expansions": notes}


_STOP = {"무엇", "무엇이", "무엇인가", "어떤", "어떻게", "어디", "언제", "누가", "왜",
         "필요한가", "필요한지", "필요합니까", "필요해", "필요한", "하나요", "인가요",
         "받으려면", "하려면", "하려는데", "있나요", "있는지", "되나요", "될까요",
         "알려줘", "알려주세요", "궁금합니다", "궁금해", "해야", "하는지", "대해",
         "관련", "경우", "때는", "때에", "그리고", "또는", "우리", "저희", "회사"}
_JOSA = re.compile(r"(을|를|이|가|은|는|의|에|에서|으로|로|와|과|도|만|까지|부터|에게|께|"
                   r"이나|나|라도|든지|처럼|보다|마다|조차|밖에)$")
_ENDING = re.compile(r"(하려면|하려는|하려고|해야|하는|한|할|합니다|해도|하고|하면|"
                     r"되는|된|될|시|시에|중인|중)$")


def keywords_from_question(q):
    out = []
    for w in re.split(r"[^\w가-힣]+", q):
        if not w or w in _STOP:
            continue
        base = _ENDING.sub("", _JOSA.sub("", w))
        if len(base) < 2 or base in _STOP or base.isdigit():
            continue
        if base not in out:
            out.append(base)
    return out


def search_for_question(q, limit=30, plan=None):
    uniq, buckets = {}, []

    def collect(key, from_plan=False):
        """검색어 하나의 결과를 순위가 있는 묶음으로 담는다.

        묶음끼리 중복을 제거하지 않는다 — 여러 검색어에 공통으로 잡혔다는 사실이
        곧 관련성 신호라서, 뒤에서 merge_buckets가 그 점수를 매긴다.
        """
        bucket = api_search(key, limit=limit).get("results", [])
        for r in bucket:
            r["_plan"] = from_plan
            uniq.setdefault(r["article_id"], r)
        if bucket:
            buckets.append(bucket)

    if plan:
        for key in plan.get("keywords", []):
            collect(key, True)
        if plan.get("substance"):
            collect(plan["substance"], True)

    kws = keywords_from_question(q)
    if not kws:
        collect(q)
    else:
        syn = load_synonyms()
        kws_law = [syn[k][0] if k in syn else k for k in kws]
        combos = []
        if kws_law != kws:
            combos += [kws_law[:i] for i in range(len(kws_law), 0, -1)]
        combos += [kws[:i] for i in range(len(kws), 0, -1)] + [[k] for k in kws]
        tried = []
        for c in combos:
            key = " ".join(c)
            if key in tried:
                continue
            tried.append(key)
            collect(key)
            if sum(1 for r in uniq.values() if r.get("tier") != "KOSHA") >= limit // 2:
                break
        if not uniq:   # 긴 복합어를 잘라 재시도 (안전보건확보의무 → 안전보건/확보의무)
            for k in kws:
                if len(k) < 6:
                    continue
                for piece in (k[:len(k) // 2], k[len(k) // 2:], k[:4], k[-4:]):
                    if len(piece) >= 2:
                        collect(piece)
                if uniq:
                    break

    return merge_buckets(buckets, uniq, limit)


def merge_buckets(buckets, uniq, limit, k=60):
    """검색어별 결과를 RRF(순위 융합)로 합친다.

    한 검색어의 결과로 limit을 다 채우면 나머지 검색어가 통째로 잘려 나가고,
    LLM이 검색어를 한 번 빗나가면(예: "화학물질 관리법") 규칙 기반으로 찾아 둔
    정답 조문이 밀려난다. 검색어마다 순위 점수(1/(k+순위))를 매겨 더하면,
    여러 검색어에 공통으로 잡힌 조문이 위로 올라온다 — 그 자체가 관련성 신호다.
    """
    score = {}
    for b in buckets:
        for rank, r in enumerate(b):
            aid = r["article_id"]
            score[aid] = score.get(aid, 0) + 1.0 / (k + rank + 1)
    ordered = [uniq[a] for a in sorted(score, key=score.get, reverse=True) if a in uniq]
    return ([r for r in ordered if r.get("tier") != "KOSHA"] +
            [r for r in ordered if r.get("tier") == "KOSHA"])[:limit]


# ── 물질 ────────────────────────────────────────────────────────
CAS_RE = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")


def cas_valid(cas):
    d = cas.replace("-", "")
    return sum(int(x) * (i + 1) for i, x in enumerate(reversed(d[:-1]))) % 10 == int(d[-1])


def find_substance_rows(question, *precise):
    """물질의 규제 목록 등재 내역을 찾는다. 근거의 신뢰도 순으로 돌려준다.

    question은 사용자가 쓴 원문이라 약어가 섞인다. precise는 LLM이 확정한
    정식명칭·CAS다. 같은 '이름 부분일치'라도 어느 쪽에서 나왔는지로 신뢰도가 다르다.

      0 CAS 완전일치            2 확정명 부분일치        4 질문어 부분일치
      1 확정명 완전일치          3 질문어 완전일치        5 별표 본문 부분일치

    짧은 약어를 확정명과 똑같이 믿으면 다른 물질이 근거로 올라간다 — "DMA"로 찾으면
    "디엠에이비 [DMAB]"가 걸리고, LLM이 DMA를 디메틸아민 보란으로 설명하게 된다.
    확실한 근거가 충분하면 아래 등급은 아예 버린다.
    """
    con = db()
    seen, buckets = set(), {i: [] for i in range(6)}

    def add(rank, rs):
        for r in rs:
            k = (r["law_name"], r["annex"], r["context"][:80])
            if k not in seen:
                seen.add(k)
                buckets[rank].append(dict(r))

    syn = load_synonyms()
    for t, base in [(question, 3)] + [(p, 1) for p in precise]:
        t = (t or "").strip()
        if not t:
            continue
        for cas in CAS_RE.findall(t):      # CAS는 어디서 나왔든 모호하지 않다
            if cas_valid(cas):
                add(0, con.execute("SELECT law_name,annex,context FROM substances WHERE cas=?",
                                   (cas,)))
        toks = re.split(r"\s+", t)
        for w in list(toks):
            toks += syn.get(re.sub(r"(의|을|를|이|가|은|는|과|와|으로|로)$", "", w), [])
        for tok in toks:
            tok = re.sub(r"(의|을|를|이|가|은|는|과|와|으로|로)$", "", tok)
            if len(tok) < 3 or CAS_RE.search(tok):
                continue
            add(base, con.execute("SELECT law_name,annex,context FROM substances"
                                  " WHERE name = ? LIMIT 30", (tok,)))
            add(base + 1, con.execute("SELECT law_name,annex,context FROM substances"
                                      " WHERE name LIKE ? LIMIT 30", (f"%{tok}%",)))
            add(5, con.execute("SELECT law_name,annex,context FROM substances"
                               " WHERE context LIKE ? LIMIT 30", (f"%{tok}%",)))
    con.close()

    def prio(r):   # 같은 등급 안에서는 지정 목록 > 규정수량 > 그 밖
        a = r.get("annex", "")
        return 0 if ("지정 목록" in a or "지정" in a) else (1 if "규정수량" in a else 2)

    rows = []
    for rank in range(6):                  # 위 등급으로 3건이 차면 아래는 안 본다
        if len(rows) >= 3:
            break
        rows += buckets[rank]
    rows.sort(key=prio)
    return rows[:20]


def api_substance(q):
    q = (q or "").strip()
    if not q:
        return {"query": q, "matches": []}
    con = db()
    if re.fullmatch(r"\d{2,7}-\d{2}-\d", q):
        rows = con.execute("SELECT cas,name,law_name,annex,context FROM substances WHERE cas=?",
                           (q,)).fetchall()
    else:
        rows = con.execute("SELECT cas,name,law_name,annex,context FROM substances"
                           " WHERE name LIKE ? OR context LIKE ? LIMIT 100",
                           (f"%{q}%", f"%{q}%")).fetchall()
    seen, matches = set(), []
    for r in rows:
        key = (r["law_name"], r["annex"], r["cas"])
        if key in seen:
            continue
        seen.add(key)
        d = dict(r)
        label = (r["annex"] or "").split(" ")[0]
        art = con.execute("SELECT a.article_id FROM articles a JOIN laws l ON l.law_id=a.law_id"
                          " WHERE l.name=? AND a.jo_short=? ORDER BY a.jo_branch LIMIT 1",
                          (r["law_name"], label)).fetchone()
        d["article_id"] = art["article_id"] if art else None
        matches.append(d)
    con.close()
    return {"query": q, "matches": matches}


# ── 조회 ────────────────────────────────────────────────────────
def api_stats():
    con = db()
    g = lambda q: con.execute(q).fetchone()[0]
    out = {"laws": g("SELECT COUNT(*) FROM laws"),
           "articles": g("SELECT COUNT(*) FROM articles"),
           "groups": g("SELECT COUNT(DISTINCT grp) FROM laws"),
           "substances": g("SELECT COUNT(DISTINCT cas) FROM substances WHERE cas!=''")}
    r = con.execute("SELECT value FROM meta WHERE key='bundle_created_at'").fetchone()
    out["bundle_created_at"] = r[0] if r else ""
    con.close()
    return out


def api_laws():
    con = db()
    rows = con.execute("SELECT law_id,name,grp,tier,ministry,enforce,source_url,article_count"
                       " FROM laws ORDER BY law_id").fetchall()
    con.close()
    groups, order = {}, []
    for r in rows:
        g = r["grp"]
        if g not in groups:
            groups[g] = {"group": g, "ministry": r["ministry"], "laws": []}
            order.append(g)
        groups[g]["laws"].append(dict(r))
    return [groups[g] for g in order]


def api_law_articles(law_id):
    con = db()
    rows = con.execute("SELECT article_id,jo_label,jo_short,title,chapter FROM articles"
                       " WHERE law_id=? ORDER BY jo_no, jo_branch", (law_id,)).fetchall()
    law = con.execute("SELECT name,source_url FROM laws WHERE law_id=?", (law_id,)).fetchone()
    con.close()
    return {"law": dict(law) if law else None, "articles": [dict(r) for r in rows]}


def api_article(article_id):
    con = db()
    r = con.execute("SELECT a.*, l.name law_name, l.tier, l.kind, l.source_url"
                    " FROM articles a JOIN laws l ON l.law_id=a.law_id"
                    " WHERE a.article_id=?", (article_id,)).fetchone()
    con.close()
    if not r:
        return None
    d = dict(r)
    d["deep_link"] = deep_link(d)
    return d


# ── LLM ─────────────────────────────────────────────────────────
def _post_json(url, payload, timeout=60):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + OPENAI_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))
