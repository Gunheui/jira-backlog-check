"""③ 근거 확인. AI 출력을 코드가 검사한다(AI를 쓰지 않는다).

검사 결과는 두 단계로 나눈다.
- 오류: AI가 틀린 것. 회의록에 없는 문장을 인용했거나, 없는 담당·기한·이슈 번호를 댔거나, 양식을 어겼다
- 확인 필요: AI가 틀린 건 아니지만 회의록만으로 정할 수 없어 사람이 봐야 하는 것
"""

import re

from . import jira
from .confluence import Page
from .templates import Template, for_type

ISSUE_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def _norm(s: str) -> str:
    s = re.sub(r"^\s*(-|\d+\.|\[[ x]\])\s+", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def _in_notes(fragment: str, page_norm: str) -> bool:
    return bool(fragment) and _norm(fragment) in page_norm


def check_candidate(c: dict, page: Page, page_norm: str, rules: dict, templates: list[Template], ids: dict, meta: dict) -> list[dict]:
    issues = []

    def add(level, field, message):
        issues.append({"level": level, "field": field, "message": message})

    # 인용
    if not c["evidence"]:
        add("오류", "evidence", "근거 문장이 없음")
    for quote in c["evidence"]:
        if len(_norm(quote)) < 4:
            add("오류", "evidence", f"근거가 너무 짧음: '{quote}'")
        elif not _in_notes(quote, page_norm):
            add("오류", "evidence", f"회의록에 없는 문장: '{quote}'")

    # 회의록에 있어야 하는 값
    for field in ("assignee_in_notes", "due_in_notes"):
        value = c[field]
        if value and not _in_notes(value, page_norm):
            add("오류", field, f"회의록에 없는 값: '{value}'")
    for key in c["mentioned_issue_keys"]:
        if key not in page.text:
            add("오류", "mentioned_issue_keys", f"회의록에 없는 이슈 번호: {key}")
    for key in set(ISSUE_KEY.findall(c["title"] + "\n" + c["description"])) - set(ISSUE_KEY.findall(page.text)):
        add("오류", "description", f"회의록에 없는 이슈 번호를 씀: {key}")

    # 우선순위와 스프린트는 회의록 근거가 있을 때만
    if c["priority"] and not (c["priority_quote"] and _in_notes(c["priority_quote"], page_norm)):
        add("오류", "priority", f"우선순위 {c['priority']}의 근거 문장이 회의록에 없음: '{c['priority_quote']}'")
    if c["sprint"] != "미정" and not (c["sprint_quote"] and _in_notes(c["sprint_quote"], page_norm)):
        add("오류", "sprint", f"스프린트 {c['sprint']}의 근거 문장이 회의록에 없음: '{c['sprint_quote']}'")

    # 제목 규칙(templates/rules.md 설정)
    title = c["title"]
    prefix = re.match(r"^\[([^\]]+)\]\s*", title)
    bare = title[prefix.end():] if prefix else title
    if len(bare) > rules["title_max_length"]:
        add("오류", "title", f"제목이 {len(bare)}자(기준 {rules['title_max_length']}자)")
    for ch in rules["title_forbidden"]:
        if ch in title:
            add("오류", "title", f"제목에 쓰지 않는 기호 '{ch}'")
    if jira.is_bug(c["issue_type"]):
        if not prefix:
            add("오류", "title", "버그 제목에 영역 머리말이 없음")
        elif prefix.group(1) not in rules["bug_title_prefixes"]:
            add("확인 필요", "title", f"머리말 목록에 없는 영역: [{prefix.group(1)}]")
    elif prefix:
        add("오류", "title", "영역 머리말은 버그에만 붙임")

    # 양식의 필수 칸
    template = for_type(templates, c["issue_type"])
    if template is None:
        add("오류", "issue_type", f"양식이 없는 유형: {c['issue_type']}")
    else:
        lines = {line.strip() for line in c["description"].splitlines()}
        for section in template.required_sections:
            if section not in lines:
                add("오류", "description", f"{template.name} 양식의 '{section}' 칸이 없음")

    # 상위 후보. Jira 계층: 에픽 > 일반 유형(스토리, 작업, 버그) > 하위 작업 유형
    if c["parent_id"]:
        parent = ids.get(c["parent_id"])
        if parent is None:
            add("오류", "parent_id", f"없는 후보를 상위로 지정: {c['parent_id']}")
        elif jira.is_subtask(meta, parent["issue_type"]):
            add("오류", "parent_id", f"상위가 하위 작업 유형이라 그 아래에 둘 수 없음: {c['parent_id']} ({parent['issue_type']})")
        elif not jira.is_subtask(meta, c["issue_type"]):
            subtask_types = ", ".join(t for t in meta["issue_types"] if jira.is_subtask(meta, t))
            add("오류", "parent_id", f"'{c['issue_type']}' 유형은 에픽 바로 아래에만 둘 수 있음(다른 이슈 아래에는 {subtask_types}만 가능)")

    # 사람이 정해야 하는 것
    if c["decided"] == "미정":
        add("확인 필요", "decided", "회의에서 할지 정하지 않은 항목")
    if not c["assignee_in_notes"] and (template is None or template.needs_assignee):  # 스토리는 묶음이라 담당을 두지 않는다(양식 설정)
        add("확인 필요", "assignee_in_notes", "회의록에 담당이 없음")
    if not c["issue_worthy"]:
        add("확인 필요", "issue_worthy", f"이슈로 만들지 않아도 될 수 있음: {c['issue_worthy_reason']}")
    return issues


def _searchable(text: str) -> str:
    # 전체를 한 줄로 이은 글과 줄마다 정규화한 글을 함께 둔다. 인용이 줄 앞 목록 기호를 빼도 찾을 수 있게
    return _norm(" ".join(text.splitlines())) + "\n" + "\n".join(_norm(line) for line in text.splitlines())


def check(result: dict, page: Page, rules: dict, templates: list[Template], meta: dict) -> dict:
    page_norm = _searchable(page.text)
    candidates = result["candidates"]
    ids = {c["id"]: c for c in candidates}
    per_candidate = {c["id"]: check_candidate(c, page, page_norm, rules, templates, ids, meta) for c in candidates}

    duplicated_ids = sorted({c["id"] for c in candidates if sum(x["id"] == c["id"] for x in candidates) > 1})

    # 액션 아이템이 빠지지 않았는지: 어느 후보의 근거에도 인용되지 않은 액션 아이템을 찾는다
    quoted = [_norm(q) for c in candidates for q in c["evidence"]]
    missed = [item for item in page.action_items if not any(_norm(item) in q or q in _norm(item) for q in quoted if len(q) >= 4)]

    return {"per_candidate": per_candidate, "missed_action_items": missed, "duplicated_ids": duplicated_ids}


def check_match(match: dict, candidates: list[dict], backlog: dict[str, dict], page: Page, meta: dict) -> dict:
    """대조 결과 검사. 에픽·스토리 배정이 Jira 구조와 맞는지, 인용이 기존 이슈 본문에 있는지, 관계가 규칙에 맞는지."""
    page_norm = _searchable(page.text)
    details = match["details"]
    detail_norm = {k: _searchable(jira.detail_text(d)) for k, d in details.items()}
    ids = {c["id"]: c for c in candidates}
    assigned = {a["id"]: a for a in match["shortlist"]["assignments"]}
    judged = {j["id"]: j for j in match["judge"]["results"]}
    per_candidate = {}

    for c in candidates:
        issues = []

        def add(level, field, message):
            issues.append({"level": level, "field": field, "message": message})

        a, j = assigned.get(c["id"]), judged.get(c["id"])
        if a is None or j is None:
            add("오류", "id", "대조 결과가 없음")
            per_candidate[c["id"]] = issues
            continue

        # 에픽과 상위 스토리
        story = a["parent_story_key"]
        if story:
            if story not in backlog:
                add("오류", "parent_story_key", f"없는 이슈: {story}")
            elif backlog[story]["level"] != jira.STANDARD:
                add("오류", "parent_story_key", f"하위 작업을 둘 수 있는 이슈가 아님: {story} ({backlog[story]['type']})")
            elif backlog[story]["epic"] != a["epic_key"]:
                add("오류", "parent_story_key", f"{story}는 {backlog[story]['epic']} 에픽 소속인데 후보는 {a['epic_key']}")
            if c["parent_id"]:
                add("오류", "parent_story_key", "새 스토리 후보 아래인데 기존 스토리도 지정함")
        if c["parent_id"] in assigned and assigned[c["parent_id"]]["epic_key"] != a["epic_key"]:
            add("오류", "epic_key", f"상위 후보 {c['parent_id']}와 에픽이 다름")
        if jira.is_subtask(meta, c["issue_type"]) and not c["parent_id"] and not story:
            add("확인 필요", "parent_story_key", "들어갈 스토리가 없음. 새 스토리가 필요한지 사람이 정해야 함")
        for key in a["look_keys"]:
            if key not in backlog:
                add("오류", "look_keys", f"백로그에 없는 키: {key}")

        # 관계 판단
        for m in j["matches"]:
            key = m["key"]
            if key not in details:
                add("오류", "matches", f"본문을 보지 않은 이슈를 판단함: {key}")
                continue
            if not _in_notes(m["quote"], detail_norm[key]):
                add("오류", "matches", f"{key} 본문에 없는 인용: '{m['quote']}'")
            if m["relation"] == "재발":
                if not (jira.is_bug(c["issue_type"]) and jira.is_bug(details[key]["type"])):
                    add("오류", "matches", f"재발은 버그끼리만: {key}")
                elif not backlog[key]["done"]:
                    add("오류", "matches", f"{key}는 아직 열린 버그라 재발이 아니라 중복")
            if m["relation"] == "중복" and backlog[key]["done"]:
                add("확인 필요", "matches", f"완료된 {key}와 겹침. 이미 끝난 일인지 다시 할 일인지 확인")

        # 제안과 대상이 맞는지
        target, action = j["target_key"], j["suggested_action"]
        relation_of = {m["key"]: m["relation"] for m in j["matches"]}
        if action == "합치기" and relation_of.get(target) != "중복":
            add("오류", "suggested_action", f"합치기 대상 {target}이 중복으로 판단된 이슈가 아님")
        if action == "합치기" and target in backlog and backlog[target]["done"]:
            add("오류", "suggested_action", f"완료된 {target}에 합치기를 제안함")
        if action == "재발로 다시 열기" and relation_of.get(target) != "재발":
            add("오류", "suggested_action", f"다시 열 대상 {target}이 재발로 판단된 이슈가 아님")
        if action == "새로 만들기" and target:
            add("오류", "target_key", "새로 만들기인데 대상 이슈가 있음")

        # 선행 작업: 회의록에 근거가 있어야 한다
        for b in j["blocked_by"]:
            if b["ref"] not in ids and b["ref"] not in backlog:
                add("오류", "blocked_by", f"없는 후보나 이슈: {b['ref']}")
            if not _in_notes(b["quote"], page_norm):
                add("오류", "blocked_by", f"회의록에 없는 문장: '{b['quote']}'")
        per_candidate[c["id"]] = issues

    return {"per_candidate": per_candidate}


def resolution_notes(detail: dict) -> list[str]:
    """재발 후보 옆에 보여 줄 과거 해결 기록. AI가 옮겨 적지 않고 코드가 댓글에서 그대로 꺼낸다."""
    return [f"{c['created']} {c['text']}" for c in detail["comments"] if "해결 기록" in c["text"]]
