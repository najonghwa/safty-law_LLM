# -*- coding: utf-8 -*-
"""_rag.py — 질의 파이프라인 (Vercel 서버리스용).

로컬 버전(rag.py)에서 벡터 검색(numpy·임베딩 DB)을 뺀 것. 키워드 검색만으로도
평가셋 35건 검색 정확도 100%가 나왔고, 서버리스는 콜드 스타트가 중요하다.

할루시네이션 차단은 그대로 3중 게이트로 강제한다.
  1. 검색 게이트 — 근거 조문이 없으면 조문 인용 답변을 만들지 않는다(일반 답변으로 전환).
  2. 폐쇄형 생성 — LLM에는 검색으로 확정된 조문만 준다.
  3. 인용 검증 — 제공한 번호 밖의 인용은 코드로 제거하고, 남는 게 없으면 폐기.
"""
import re, json, urllib.parse
from _lib import (OPENAI_KEY, CHAT_URL, CHAT_MODEL, TOP_K, db, _post_json, rank_source)


def _fetch_articles(article_ids):
    if not article_ids:
        return []
    con = db()
    ph = ",".join("?" * len(article_ids))
    rows = {r["article_id"]: dict(r) for r in con.execute(
        f"SELECT a.article_id, a.jo_label, a.jo_short, a.title, a.chapter,"
        f" a.content, l.name law_name, l.tier, l.kind, l.source_url"
        f" FROM articles a JOIN laws l ON l.law_id=a.law_id"
        f" WHERE a.article_id IN ({ph})", article_ids)}
    con.close()
    return [rows[i] for i in article_ids if i in rows]


SYSTEM_PROMPT = """당신은 한국 안전·환경 법규 검색 어시스턴트입니다. 반도체 소재 제조 사업장 실무자의 질문에 답합니다.

절대 규칙:
1. 아래 [근거 조문]에 명시된 내용만 사용해 답하십시오. 당신의 사전 지식, 추측, 일반 상식은 금지됩니다.
2. 답변의 모든 문장과 체크리스트 항목 끝에는 근거 번호를 [1] [2] 형식으로 붙이십시오.
3. 반드시 아래 JSON 형식으로만 출력하십시오:
{"answer": "질문에 대한 답변 (각 문장 끝 [n] 인용)", "checklist": ["실무 확인 항목 [n]", "..."], "citations": [사용한 근거 번호 목록]}
4. 답변은 한국어로 간결하게 작성하십시오.

답변 범위 판단 (매우 중요):
**기본 태도는 "답한다"입니다.** 근거 조문은 이미 이 질문으로 검색된 것이므로 대개 관련이 있습니다.
질문에 딱 맞는 한 문장이 없더라도, 조문이 정하는 의무·절차·기준을 정리해 주는 것이 사용자에게 유용합니다.

- 예시 1) "폐액은 어떻게 버려야 하나?" + [폐기물관리법 제17조 사업장폐기물배출자의 의무]
  → 올바른 답변: "사업장폐기물 배출자는 폐기물의 처리를 위탁할 때 적정 처리 여부를 확인해야 하고,
    지정폐기물은 처리계획을 확인받아야 합니다 [1][7]" (조문에 있는 의무를 정리)
  → 잘못된 답변: "확인 불가" ← 관련 조문이 있는데 이렇게 답하면 안 됩니다.
- 예시 2) "안전보건교육은 몇 시간?" + [교육 시간이 적힌 별표]
  → 별표에 시간이 있으면 그대로 인용. 없으면 "교육 의무는 있으나 시간은 제공된 조문에 없음"처럼
    아는 부분과 모르는 부분을 나눠 답하십시오.
- 질문이 특정 물질의 지정 여부인데 [규제 목록 대조 결과] 항목이 있으면, 그 등재 내역을 근거로
  "등재되어 있음 / 해당 목록에서는 확인되지 않음"을 분명히 답하십시오.
- 다만 질문이 **안전·보건·환경 법규와 무관한 주제**(요리, 날씨, 사내 인사·급여·연차 규정,
  일반 상식 등)이면, 조문이 붙어 있더라도 억지로 엮지 말고 answer를 정확히
  "제공된 조문으로는 확인 불가"로 하십시오. 이 DB는 안전·보건·환경 법령만 담고 있습니다."""

QUERY_PROMPT = """당신은 한국 안전·환경 법령 검색을 돕습니다.
사용자 질문을 보고, 법령 DB(SQLite 전문검색)에서 근거 조문을 찾기 위한 검색어를 만드십시오.

규칙:
1. 현장 용어·약어를 **법령에 실제로 쓰이는 표기**로 바꾸십시오.
   예: 불산 → 불화수소, 플루오르화수소 / MSDS → 물질안전보건자료 /
       폐액 → 지정폐기물, 폐수 / DMA → 디메틸아민 / 가성소다 → 수산화나트륨
2. 물질명은 **국문 정식명칭과 이명(異名)을 모두** 넣으십시오. 알고 있으면 CAS 번호도.
3. 조사·어미·의문사는 빼고 명사 위주로. 각 검색어는 2~5단어 이내.
4. 검색어 3~6개. 넓은 것부터 좁은 것 순으로.
5. JSON만 출력:
{"keywords": ["검색어1", "검색어2", ...], "substance": "질문에 나온 물질의 법령상 정식명칭 또는 null", "cas": "CAS번호 또는 null"}

예시)
질문: "불산은 사고대비물질인가요?"
{"keywords": ["불화수소 사고대비물질", "플루오르화수소", "사고대비물질 지정"], "substance": "불화수소", "cas": "7664-39-3"}

질문: "폐액은 어떻게 버려야 하나요?"
{"keywords": ["지정폐기물 처리", "사업장폐기물 배출자 의무", "폐기물 위탁처리"], "substance": null, "cas": null}"""

GENERAL_PROMPT = """당신은 한국 안전·보건·환경 실무를 돕는 어시스턴트입니다.

지금 이 질문은 법령 DB에서 근거 조문을 찾지 못했습니다. 그래도 도움이 되도록
당신이 아는 일반 지식으로 답하되, 아래를 반드시 지키십시오.

1. 법 조항 번호(예: 제28조)를 구체적으로 인용하지 마십시오. 확인되지 않은 조문 번호는
   사용자를 오도합니다. 법령 이름 수준(화학물질관리법 등)까지만 언급하십시오.
2. 확실하지 않은 부분은 "확인이 필요하다"고 솔직히 쓰십시오.
3. 법규와 무관한 질문(요리·날씨·사내 인사규정 등)이면 답할 수 없다고 안내하십시오.
4. JSON 형식으로만 출력: {"answer": "답변", "checklist": ["확인할 것", "..."]}
5. 한국어로 간결하게."""


def llm_ready():
    return bool(OPENAI_KEY)


def _chat_json(system, user, temperature=0.1, timeout=60):
    payload = {"model": CHAT_MODEL, "temperature": temperature,
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    data = _post_json(CHAT_URL + "/chat/completions", payload, timeout=timeout)
    return data["choices"][0]["message"]["content"]


def plan_query(question):
    """LLM이 질문을 법령 표기 검색어로 바꾼다. 실패하면 규칙 기반 검색으로 진행."""
    if not llm_ready():
        return {"keywords": [], "substance": None, "cas": None}
    try:
        p = json.loads(_chat_json(QUERY_PROMPT, question, 0, 30))
        return {"keywords": [str(k).strip() for k in p.get("keywords", []) if str(k).strip()][:6],
                "substance": (p.get("substance") or "").strip() or None,
                "cas": (p.get("cas") or "").strip() or None}
    except Exception:
        return {"keywords": [], "substance": None, "cas": None}


def _build_context(articles):
    return "\n\n".join(f"[{i}] {a['law_name']} {a['jo_label']}\n{a['content'][:1800]}"
                       for i, a in enumerate(articles, 1))


def _sources_payload(articles):
    out = []
    for i, a in enumerate(articles, 1):
        if a["kind"] == "sub" or not a["source_url"]:
            link = ""
        elif a["kind"] == "admrul":
            link = a["source_url"]
        else:
            link = a["source_url"] + "/" + urllib.parse.quote(a["jo_short"])
        out.append({"idx": i, "article_id": a["article_id"], "law_name": a["law_name"],
                    "jo_label": a["jo_label"], "tier": a["tier"], "deep_link": link,
                    "excerpt": a["content"][:200]})
    return out


def substance_pseudo_article(rows):
    """별표 대조로 확정된 등재 내역을 근거 [1]로 고정한다(큰 별표에서 행을 놓치는 것 방지)."""
    lines, seen = [], set()
    for r in rows[:20]:
        key = (r["law_name"], r["annex"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {r['law_name']} {r['annex']}: {r['context']}")
    return {"article_id": 0, "jo_label": "물질 규제목록 등재 현황", "jo_short": "",
            "title": "별표 대조 확정 정보", "chapter": "", "law_name": "규제 목록 대조 결과",
            "tier": "확정", "kind": "sub", "source_url": "",
            "content": "다음은 질문의 물질이 규제 목록(별표)에 등재된 확정 내역이다:\n" + "\n".join(lines)}


def _general_answer(question):
    """DB 근거가 없을 때 — 일반 지식으로 답하되 'DB 미확인'임을 표시한다."""
    if not llm_ready():
        return {"mode": "error", "reason": "서버에 OPENAI_API_KEY가 설정되어 있지 않습니다.",
                "sources": []}
    try:
        parsed = json.loads(_chat_json(GENERAL_PROMPT, question, 0.2))
        answer = str(parsed.get("answer", "")).strip()
        checklist = [str(c) for c in parsed.get("checklist", []) if str(c).strip()]
    except Exception as e:
        return {"mode": "refuse", "sources": [],
                "reason": f"법령 DB에서 근거를 찾지 못했고, 일반 답변도 실패했습니다: {e}"}
    if not answer:
        return {"mode": "refuse", "sources": [],
                "reason": "법령 DB에서 근거를 찾지 못했습니다. 질문을 법령 용어로 바꿔보세요."}
    return {"mode": "general", "answer": answer, "checklist": checklist,
            "cited_idx": [], "sources": [],
            "notice": "이 답변은 법령 DB에서 근거를 찾지 못해 AI 일반 지식으로 작성했습니다. "
                      "정확한 조항은 국가법령정보센터에서 확인하거나 담당 부서에 문의하세요."}


REVIEW_TOPICS = [
    {"id": "register", "label": "① 등록·신고 (화평법/산안법)",
     "q": "{name} 을(를) 신규로 제조 또는 수입하려고 한다. 화학물질 등록, 신고, 유해성·위험성 조사 등 필요한 절차는?"},
    {"id": "permit", "label": "② 유해화학물질 영업허가·취급기준 (화관법)",
     "q": "{name} 이(가) 유해화학물질에 해당하는 경우 영업허가, 취급기준, 취급자 요건은?"},
    {"id": "storage", "label": "③ 보관·저장 시설 기준",
     "q": "{name} 을(를) 창고에 보관·저장하려고 한다. 보관시설·저장시설 설치 기준, 보관량 관련 기준, 검사 의무는? {storage}"},
    {"id": "label", "label": "④ 표시·경고표지·MSDS",
     "q": "{name} 의 용기·포장 표시, 경고표지, 물질안전보건자료(MSDS) 작성·제출·게시 의무는?"},
    {"id": "accident", "label": "⑤ 사고 대비 (예방관리계획·신고)",
     "q": "{name} 취급 시 화학사고예방관리계획서 작성·제출 대상 여부와 화학사고 발생 시 신고 의무는?"},
    {"id": "waste", "label": "⑥ 폐기 (폐수·지정폐기물)",
     "q": "{name} 사용 후 발생하는 폐액·폐수·지정폐기물의 처리 기준과 위탁처리 요건은?"},
]


def build_review_question(topic_id, product):
    t = next((t for t in REVIEW_TOPICS if t["id"] == topic_id), None)
    if not t:
        return None
    name, cas = product.get("name", "").strip(), product.get("cas", "").strip()
    usage, storage = product.get("usage", "").strip(), product.get("storage", "").strip()
    q = t["q"].format(name=name + (f"(CAS {cas})" if cas else ""),
                      storage=f"보관 형태: {storage}." if storage else "")
    return (q + (f" (용도: {usage})" if usage else "")).strip()


def select_sources(fts_results, k=TOP_K):
    """검색 순서(검색어별 라운드로빈)는 그대로 두고 법령 위계로만 안정 정렬한다.

    별표는 세부 기준이라 유용하지만 의무의 근거는 조문에 있으므로 조문을 앞에 둔다.
    """
    return sorted(fts_results, key=rank_source)[:k]


def ask(question, fts_results, substance_rows=None):
    """질문 + 검색결과 → 근거 기반 답변, 또는 일반 답변으로 전환."""
    merged = [r["article_id"] for r in select_sources(fts_results)]

    if not merged and not substance_rows:       # 게이트 1
        return _general_answer(question)
    if not llm_ready():
        return {"mode": "error", "reason": "서버에 OPENAI_API_KEY가 설정되어 있지 않습니다.",
                "sources": _sources_payload(_fetch_articles(merged))}

    articles = _fetch_articles(merged)
    if substance_rows:
        articles = [substance_pseudo_article(substance_rows)] + articles[:TOP_K - 1]

    try:                                         # 게이트 2 — 폐쇄형 생성
        raw = _chat_json(SYSTEM_PROMPT, f"[근거 조문]\n{_build_context(articles)}\n\n[질문]\n{question}")
    except Exception as e:
        msg = str(e)
        hint = ("API 키가 올바르지 않습니다." if "401" in msg else
                "API 사용 한도 초과 또는 잔액 부족입니다." if ("429" in msg or "quota" in msg) else
                f"LLM 호출 실패: {msg}")
        return {"mode": "error", "reason": hint, "sources": _sources_payload(articles)}

    try:
        parsed = json.loads(raw)
        answer = str(parsed.get("answer", "")).strip()
        checklist = [str(c) for c in parsed.get("checklist", []) if str(c).strip()]
        cited = parsed.get("citations", [])
    except Exception:
        answer, checklist, cited = raw.strip(), [], []

    valid, used = set(range(1, len(articles) + 1)), set()   # 게이트 3 — 인용 검증
    for n in re.findall(r"\[(\d+)\]", answer + " " + " ".join(checklist)):
        used.add(int(n))
    for c in cited:
        try:
            used.add(int(c))
        except (TypeError, ValueError):
            pass
    invalid = used - valid
    used &= valid
    if invalid:
        bad = "|".join(rf"\[{n}\]" for n in invalid)
        answer = re.sub(bad, "", answer)
        checklist = [re.sub(bad, "", c).strip() for c in checklist]

    if not used:
        g = _general_answer(question)
        if g.get("mode") == "general":
            g["sources"] = _sources_payload(articles)
            g["notice"] = ("검색된 조문에서 질문에 대한 답을 찾지 못해 AI 일반 지식으로 답했습니다. "
                           "아래 참고 조문과 원문을 함께 확인하세요.")
        return g

    return {"mode": "answer", "answer": answer, "checklist": checklist,
            "cited_idx": sorted(used), "sources": _sources_payload(articles)}
