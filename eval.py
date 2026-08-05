# -*- coding: utf-8 -*-
"""eval.py — AI 질의 품질 평가셋.

질문 하나씩 고쳐서는 전체가 좋아졌는지 알 수 없다. 이 파일로 한 번에 돌려 점수로 본다.
코드를 고칠 때마다 실행해 회귀(다른 게 깨짐)를 잡는다.

  [검색] 기대 법령의 조문이 LLM에 전달되는 8건 안에 들어갔는가
  [답변] 실제로 그 근거를 인용해 답했는가
  [거부] 법규와 무관한 질문을 조문으로 억지로 엮지 않았는가

실행:
  python eval.py            # 전체 (LLM 호출 있음)
  python eval.py --search   # 검색만 (LLM 호출 없음, 무료·빠름)
"""
import sys, io, os, time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))
import _lib
import _rag

# (질문, 기대 법령 키워드, 기대 조문 키워드 or None)
CASES = [
    # ── 화학물질관리법 ──
    ("유해화학물질 영업허가를 받으려면 무엇이 필요한가?", "화학물질관리법", "제28조"),
    ("유해화학물질 취급시설은 설치 후 검사를 받아야 하나요?", "화학물질관리법", "제24조"),
    ("화학사고예방관리계획서는 누가 제출해야 하나요?", "화학물질관리법", "제23조"),
    ("유해화학물질 취급자가 지켜야 할 취급기준은?", "화학물질관리법", None),
    ("화학사고가 나면 즉시 신고해야 하나요?", "화학물질관리법", None),
    ("유해화학물질 표시는 어떻게 해야 하나요?", "화학물질관리법", None),
    ("유해화학물질관리자를 선임해야 하나요?", "화학물질관리법", None),

    # ── 화평법 ──
    ("신규 화학물질을 제조하려면 등록해야 하나요?", "등록 및 평가", "제10조"),
    ("연간 1톤 미만 화학물질도 신고 대상인가요?", "등록 및 평가", None),

    # ── 산업안전보건법 ──
    ("MSDS 작성 의무가 있나요?", "산업안전보건법", "제110조"),
    ("물질안전보건자료를 작업장에 게시해야 하나요?", "산업안전보건법", "제114조"),
    ("공정안전보고서는 언제 제출하나요?", "산업안전보건법", "제44조"),
    ("작업환경측정은 얼마마다 해야 하나요?", "산업안전보건법", None),
    ("특수건강진단 대상은 누구인가요?", "산업안전보건법", None),
    ("안전보건교육은 몇 시간 해야 하나요?", "산업안전보건법", None),
    ("관리대상 유해물질 취급 시 국소배기장치가 필요한가요?", "안전보건기준", None),

    # ── 중대재해처벌법 ──
    ("중대산업재해가 발생하면 경영책임자는 어떤 책임을 지나요?", "중대재해", None),
    ("안전보건확보의무는 무엇인가요?", "중대재해", None),

    # ── 위험물·고압가스 ──
    ("위험물 저장소를 설치하려면 허가가 필요한가요?", "위험물안전관리법", "제6조"),
    ("지정수량 이상 위험물을 임시로 저장할 수 있나요?", "위험물안전관리법", None),
    ("독성가스를 창고에 보관하려면 어떤 허가가 필요한가?", "고압가스", None),
    ("고압가스 저장소 설치 신고 대상은?", "고압가스", None),
    ("특정고압가스 사용신고는 어떻게 하나요?", "고압가스", None),

    # ── 환경 ──
    ("지정폐기물은 어떻게 처리해야 하나요?", "폐기물관리법", "제17조"),
    ("폐기물 처리를 위탁할 때 확인할 것은?", "폐기물관리법", None),
    ("폐수 배출시설 신고는 어떻게 하나요?", "물환경보전법", None),
    ("대기오염물질 배출시설 설치 허가 대상은?", "대기환경보전법", None),
    ("비산배출 시설 관리기준이 있나요?", "대기환경보전법", None),

    # ── 연구실·소방 ──
    ("연구실 정밀안전진단은 얼마마다 받나요?", "연구실", None),
    ("소방시설 자체점검은 누가 하나요?", "소방시설", None),

    # ── 물질 조회 (substances 경로) ──
    ("디메틸아민은 유해화학물질인가요?", "규정수량", None),
    ("불화수소는 사고대비물질인가요?", "화학물질관리법", None),
    ("톨루엔의 노출기준은 얼마인가요?", "노출기준", None),

    # ── 현장 용어 (동의어 확장 확인) ──
    ("폐액은 어떻게 버려야 하나요?", "폐기물관리법", None),
    ("MSDS 게시 안 하면 처벌받나요?", "산업안전보건법", None),
]

# 근거 없어야 정상 (조문으로 억지로 엮지 않아야 함)
REFUSE_CASES = [
    "김치찌개 맛있게 끓이는 법 알려줘",
    "내일 서울 날씨 어때?",
    "회사 연차 규정이 어떻게 되나요?",
]

BAR = "=" * 68


def run(search_only=False):
    ok_s = ok_c = ok_r = 0
    fails = []
    print("\n" + BAR)
    print(f" 평가셋 {len(CASES)}건" +
          ("" if search_only else f" + 거부 {len(REFUSE_CASES)}건"))
    print(BAR)
    t0 = time.time()

    for i, (q, want_law, want_jo) in enumerate(CASES, 1):
        plan = {} if search_only else _rag.plan_query(q)
        fts = _lib.search_for_question(q, plan=plan)
        subs = _lib.find_substance_rows(q, plan.get("substance"), plan.get("cas"))
        arts = _rag._fetch_articles([r["article_id"] for r in _rag.select_sources(fts)])
        names, jos = [a["law_name"] for a in arts], [a["jo_label"] for a in arts]

        hit = (any(want_law in n for n in names) or bool(subs)) and \
              (want_jo is None or any(want_jo in j for j in jos))
        ok_s += hit
        line = f"{i:2d}. {'O' if hit else 'X'} [검색] {q[:34]}"
        if not hit:
            fails.append((q, want_law, want_jo, names[:3]))

        if not search_only:
            d = _rag.ask(q, fts, subs)
            if d.get("mode") == "answer":
                cited = [s for s in d["sources"] if s["idx"] in (d.get("cited_idx") or [])]
                good = any(want_law in s["law_name"] for s in cited) or \
                    (subs and any(s["law_name"] == "규제 목록 대조 결과" for s in cited))
                ok_c += bool(good)
                line += " | O답변" if good else " | X답변(다른근거)"
            else:
                line += f" | X답변({d.get('mode')})"
        print(line)

    if not search_only:
        print("\n--- 거부 테스트 ---")
        for q in REFUSE_CASES:
            fts = _lib.search_for_question(q)
            d = _rag.ask(q, fts, _lib.find_substance_rows(q))
            good = d.get("mode") != "answer"
            ok_r += good
            print(f"  {'O' if good else 'X'} {q[:40]} -> {d.get('mode')}")

    n = len(CASES)
    print("\n" + BAR)
    print(f" 검색 정확도: {ok_s}/{n} ({ok_s / n * 100:.0f}%)")
    if not search_only:
        print(f" 답변 근거정확: {ok_c}/{n} ({ok_c / n * 100:.0f}%)")
        print(f" 거부 정확도:  {ok_r}/{len(REFUSE_CASES)}")
    print(f" 소요: {time.time() - t0:.0f}초")
    print(BAR)

    if fails:
        print("\n[검색 실패 상세]")
        for q, wl, wj, names in fails:
            print(f"  Q: {q}")
            print(f"     기대 {wl} {wj or ''} / 실제 상위 {[x[:16] for x in names]}")


if __name__ == "__main__":
    if not os.environ.get("OPENAI_API_KEY") and "--search" not in sys.argv:
        print("OPENAI_API_KEY 가 없습니다. 검색만 측정하려면 --search 를 붙이세요.")
        sys.exit(1)
    run("--search" in sys.argv)
