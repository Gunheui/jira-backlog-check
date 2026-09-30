"""명령 모음. 설치했으면 `pm-agent <명령>`, 레포 폴더에서는 `uv run pm-agent <명령>`.

analyze는 ①읽기 ②추출 ③대조 ④근거 확인까지 하고 결과를 runs/에 JSON으로 남긴다.
Jira와 Confluence는 읽기만 하므로 평가할 때 여러 번 돌려도 된다.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import config, confluence, extract, jira, match, templates, verify

sys.stdout.reconfigure(line_buffering=True)


def _cost(usage: dict) -> str:
    return f"${usage['cost_usd']:.4f}" if usage.get("cost_usd") is not None else "비용 정보 없음"


def analyze(page_id: str, reuse_extract: Path | None = None) -> dict:
    page = confluence.get_page(page_id)
    print(f"[읽기] {page.title} (v{page.version}), {len(page.text)}자, 액션 아이템 {len(page.action_items)}개")

    rules, tpls = templates.load()
    meta = jira.project_meta()
    unknown = [t for tpl in tpls for t in tpl.issue_types if t not in meta["issue_types"]]
    if unknown:
        print(f"[주의] 템플릿의 유형 중 Jira 프로젝트에 없는 것은 빼고 진행: {', '.join(unknown)}")

    usages = []
    if reuse_extract:
        # 대조만 고쳐 가며 돌릴 때 추출 비용을 아낀다. 같은 페이지 같은 판일 때만 쓴다
        previous = json.loads(reuse_extract.read_text(encoding="utf-8"))
        if previous["page"]["id"] != page.id or previous["page"]["version"] != page.version:
            raise SystemExit("[중단] 재사용하려는 결과가 다른 페이지이거나 회의록이 그 뒤에 바뀜")
        result = previous["extract"]
        print(f"[추출] {reuse_extract.name}의 추출 결과 재사용, 후보 {len(result['candidates'])}건")
    else:
        print(f"[추출] 모델 호출 중 ({config.LLM_BACKEND}, {config.LLM_MODEL or '기본 모델'})")
        result, usage = extract.run(page, rules, tpls, meta)
        usage["step"] = "추출"
        usages.append(usage)
        print(f"[추출] 후보 {len(result['candidates'])}건, 제외 {len(result['excluded'])}건, {usage['seconds']}초, {_cost(usage)}")
    report = verify.check(result, page, rules, tpls, meta)

    backlog = jira.all_issues()
    print(f"[대조] 기존 이슈 {len(backlog)}건과 대조 중")
    matched, match_usages = match.run(page.text, result["candidates"], backlog, meta)
    usages += match_usages
    for u in match_usages:
        print(f"[대조] {u['step']}: {u['seconds']}초, {_cost(u)}")
    match_report = verify.check_match(matched, result["candidates"], backlog, page, meta)
    notes = {
        m["key"]: verify.resolution_notes(matched["details"][m["key"]])
        for j in matched["judge"]["results"]
        for m in j["matches"]
        if m["relation"] == "재발" and m["key"] in matched["details"]
    }

    run = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "page": {"id": page.id, "title": page.title, "url": page.url, "version": page.version},
        "usage": usages,
        "extract": result,
        "verify": report,
        "match": matched,
        "match_verify": match_report,
        "resolution_notes": notes,
    }
    config.RUNS_DIR.mkdir(exist_ok=True)
    path = config.RUNS_DIR / f"{page.id}_{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    run["file"] = path.name

    print_summary(run, backlog)
    costs = [u["cost_usd"] for u in usages if u.get("cost_usd") is not None]
    cost = f"${sum(costs):.4f}" if costs else "비용 정보 없음(구독 로그인)"
    print(f"\n합계 {sum(u['seconds'] for u in usages):.0f}초, {cost}")
    print(f"결과 파일: {path.relative_to(config.WORK_DIR)}")
    return run


def print_summary(run: dict, backlog: dict) -> None:
    result, report, match_report = run["extract"], run["verify"], run["match_verify"]
    assigned = {a["id"]: a for a in run["match"]["shortlist"]["assignments"]}
    judged = {j["id"]: j for j in run["match"]["judge"]["results"]}
    all_items = [i for items in report["per_candidate"].values() for i in items]
    all_items += [i for items in match_report["per_candidate"].values() for i in items]
    errors = sum(i["level"] == "오류" for i in all_items)
    print(f"\n[근거 확인] 오류 {errors}건, 빠진 액션 아이템 {len(report['missed_action_items'])}건")

    for c in result["candidates"]:
        a, j = assigned.get(c["id"]), judged.get(c["id"])
        print(f"\n{c['id']} [{c['issue_type']}/{c['component']}] {c['title']}")
        if a:
            epic = backlog[a["epic_key"]]["summary"]
            parent = c["parent_id"] or (f"{a['parent_story_key']} {backlog[a['parent_story_key']]['summary']}" if a["parent_story_key"] in backlog else a["parent_story_key"])
            print(f"   위치: {epic} 에픽" + (f" > {parent}" if parent else ""))
        if j:
            target = f" → {j['target_key']}" if j["target_key"] else ""
            print(f"   제안: {j['suggested_action']}{target} ({j['reason']})")
            for m in j["matches"]:
                summary = backlog[m["key"]]["summary"] if m["key"] in backlog else "?"
                print(f"   - {m['relation']} {m['key']} {summary}: {m['reason']}")
                for note in run["resolution_notes"].get(m["key"], []) if m["relation"] == "재발" else []:
                    print("     과거 " + note.replace("\n", "\n     "))
            for b in j["blocked_by"]:
                print(f"   - 선행: {b['ref']} (\"{b['quote']}\")")
        for item in report["per_candidate"].get(c["id"], []) + match_report["per_candidate"].get(c["id"], []):
            mark = "✗" if item["level"] == "오류" else "?"
            print(f"   {mark} {item['level']}: {item['message']}")
    for item in report["missed_action_items"]:
        print(f"\n✗ 후보가 없는 액션 아이템: {item}")
    if report["duplicated_ids"]:
        print(f"\n✗ 겹치는 후보 id: {', '.join(report['duplicated_ids'])}")
    for ex in result["excluded"]:
        print(f"\n- 제외: {ex['text']} ({ex['reason']})")


def _pages_index() -> tuple[Path, dict]:
    path = config.RUNS_DIR / "review_pages.json"
    return path, (json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})


def doc_create(run_path: Path) -> None:
    from . import notify, review_doc
    from .review import Review

    r = Review(run_path)
    meeting = confluence.get_storage(r.run["page"]["id"])
    title = f"이슈 검토: {r.run['page']['title']}"
    if confluence.title_exists(meeting["space_id"], title):
        # 같은 회의록을 다시 돌린 경우. 먼저 만든 검토 문서(반영 기록)는 그대로 두고 시각을 붙여 새로 만든다
        title += f" ({datetime.now():%m-%d %H:%M})"
    page = confluence.create_page(meeting["space_id"], r.run["page"]["id"], title, review_doc.render(r))
    confluence.add_label(page["id"], config.REVIEW_LABEL)  # 자동화 규칙 2가 이 라벨이 붙은 페이지만 서버로 보낸다
    path, index = _pages_index()
    index[page["id"]] = {"run": run_path.name}
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[검토 문서] {page['url']}")
    judge = sum(map(r.needs_judgement, r.candidates))
    notify.send(
        f"📋 *회의록 이슈 후보 {len(r.candidates)}건* 검토 문서가 준비됐습니다\n<{page['url']}|{title}>\n"
        f"확정 후보 {len(r.candidates) - judge}건(체크됨), 확인 요청 후보 {judge}건(체크 해제). "
        f"체크하고 고친 뒤 페이지 상태를 '{config.DONE_STATUS}'으로 바꾸면 반영됩니다."
    )


def doc_apply(page_id: str, run_path: Path | None, dry_run: bool) -> None:
    from . import notify, review_doc
    from .review import Review

    if run_path is None:
        _, index = _pages_index()
        if page_id not in index:
            raise SystemExit(f"[중단] {page_id}는 도구가 만든 검토 문서가 아님(runs/review_pages.json에 없음)")
        run_path = config.RUNS_DIR / index[page_id]["run"]
    r = Review(run_path, dry_run=dry_run)
    if r.state.get("applied_page") == page_id:
        print("[반영] 이미 반영한 문서라 건너뜀")
        return
    doc = confluence.get_storage(page_id)
    rules, _ = templates.load()
    plan, errors, warnings = review_doc.check(r, review_doc.parse(doc["storage"]), r.meta, rules)
    for w in warnings:
        print(f"[참고] {w}")
    if errors:
        for e in errors:
            print(f"[오류] {e}")
        if not dry_run:
            confluence.update_page(page_id, review_doc.with_errors(doc["storage"], errors, warnings), "반영 전 검사 오류 표시")
            notify.send(f"⚠ 검토 문서에 고칠 곳이 {len(errors)}건 있어 반영하지 않았습니다\n<{doc['url']}|{doc['title']}>")
        return
    rows = review_doc.apply(r, plan)
    counts = {}
    for row in rows:
        counts[row["decision"]] = counts.get(row["decision"], 0) + 1
    summary = ", ".join(f"{k} {v}건" for k, v in counts.items())
    for row in rows:
        print(f"  {row['id']} {row['decision']} {row['key'] or ''} {row.get('note') or ''}")
    print(f"[반영] {summary}")
    if dry_run:
        print("[시험 실행] Jira와 문서는 바꾸지 않았습니다")
        return
    confluence.update_page(page_id, review_doc.render_result(r, rows), "Jira 반영 결과")
    r.state["applied_page"] = page_id
    r.save()
    notify.send(f"✅ *Jira 반영 완료*: {summary}\n<{doc['url']}|{doc['title']}>")


def main() -> None:
    parser = argparse.ArgumentParser(prog="pm-agent", description="회의록에서 이슈 후보를 뽑아 기존 Jira와 대조하고, 사람이 검토 문서에서 확인한 것만 Jira에 반영한다")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="질문에 답하면 이 폴더에 .env를 만든다(처음 한 번)")
    p = sub.add_parser("check", help="설정과 Jira, Confluence, Slack, 모델 연결을 확인한다(읽기만 함)")
    p.add_argument("--send-test", action="store_true", help="Slack 채널에 시험 메시지 한 건을 보낸다")
    p.add_argument("--skip-llm", action="store_true", help="모델 호출 확인을 건너뛴다")
    p = sub.add_parser("analyze", help="회의록에서 이슈 후보를 뽑고 기존 Jira와 대조한다(Jira, Confluence 변경 없음)")
    p.add_argument("--page", required=True, help="Confluence 페이지 ID")
    p.add_argument("--reuse-extract", type=Path, help="이전 결과 파일의 추출을 재사용(대조만 다시 돌릴 때)")
    p = sub.add_parser("doc", help="Confluence 검토 문서: create(만들기), apply(검사 후 반영)")
    p.add_argument("action", choices=["create", "apply"])
    p.add_argument("--run", type=Path, help="analyze 결과 파일(create에 필요)")
    p.add_argument("--page", help="검토 문서 페이지 ID(apply에 필요)")
    p.add_argument("--dry-run", action="store_true", help="Jira와 문서에 쓰지 않고 할 일만 출력")
    p = sub.add_parser("serve", help="Confluence 자동화의 웹 요청을 받아 analyze, 검토 문서, 반영을 실행")
    p.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    if args.command == "init":
        from .init import run as init

        init()
    elif args.command == "check":
        from .check import run as check

        raise SystemExit(check(send_test=args.send_test, skip_llm=args.skip_llm))
    elif args.command == "analyze":
        analyze(args.page, args.reuse_extract)
    elif args.command == "doc" and args.action == "create":
        if not args.run:
            parser.error("doc create에는 --run이 필요함")
        doc_create(args.run)
    elif args.command == "doc":
        if not args.page:
            parser.error("doc apply에는 --page가 필요함")
        doc_apply(args.page, args.run, args.dry_run)
    elif args.command == "serve":
        from .server import serve

        serve(port=args.port)


if __name__ == "__main__":
    main()
