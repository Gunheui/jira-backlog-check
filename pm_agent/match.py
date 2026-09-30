"""② 기존 Jira와 대조. AI 호출 두 번으로 나눈다.

1단계(훑기): 백로그 전체의 제목 목록만 주고 에픽을 정하고, 본문을 열어 볼 이슈를 고르게 한다
2단계(판단): 고른 이슈의 본문과 댓글(해결 기록)만 가져와 중복, 재발, 관련을 판단하게 한다

한 번에 전부 주지 않는 이유: 백로그 전체의 본문과 댓글을 넣으면 길어져 비용이 늘고 판단이 흐려진다.
백로그가 수천 건으로 늘면 1단계 앞에 검색(JQL)으로 좁히는 단계를 더하면 된다.
"""

import json

from . import config, jira, llm

RELATIONS = ["중복", "재발", "관련"]
ACTIONS = ["새로 만들기", "합치기", "재발로 다시 열기"]


def _prompt_file(name: str) -> str:
    return (config.PROMPT_DIR / name).read_text(encoding="utf-8")


def backlog_text(backlog: dict[str, dict]) -> str:
    lines = []
    for i in backlog.values():
        if i["level"] == jira.EPIC:
            continue
        where = " > ".join(backlog[k]["summary"] for k in (i["epic"], i["story"]) if k)
        # 해결 값이 상태 이름과 같으면(완료/완료) 되풀이하지 않는다. Won't Do처럼 다를 때만 보여 준다
        resolution = f"({i['resolution']})" if i["resolution"] and i["resolution"] != i["status"] else ""
        lines.append(f"{i['key']} | {i['type']} | {i['status']}{resolution} | {where} | {i['summary']}")
    return "\n".join(lines)


def _candidate_view(c: dict) -> dict:
    fields = ("id", "title", "issue_type", "component", "parent_id", "description", "backlog_hint")
    return {f: c[f] for f in fields}


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def shortlist_schema(epic_keys: list[str]) -> dict:
    item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "epic_key": {"type": "string", "enum": epic_keys},
            "parent_story_key": {"type": ["string", "null"]},
            "look_keys": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        "required": ["id", "epic_key", "parent_story_key", "look_keys", "reason"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"assignments": {"type": "array", "items": item}},
        "required": ["assignments"],
    }


def judge_schema() -> dict:
    match = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "key": {"type": "string"},
            "relation": {"type": "string", "enum": RELATIONS},
            "reason": {"type": "string"},
            "quote": {"type": "string"},
        },
        "required": ["key", "relation", "reason", "quote"],
    }
    blocked = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"ref": {"type": "string"}, "quote": {"type": "string"}},
        "required": ["ref", "quote"],
    }
    item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "matches": {"type": "array", "items": match},
            "suggested_action": {"type": "string", "enum": ACTIONS},
            "target_key": {"type": ["string", "null"]},
            "reason": {"type": "string"},
            "short_reason": {"type": "string"},
            "caution": {"type": ["string", "null"]},
            "blocked_by": {"type": "array", "items": blocked},
        },
        "required": ["id", "matches", "suggested_action", "target_key", "reason", "short_reason", "caution", "blocked_by"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"results": {"type": "array", "items": item}},
        "required": ["results"],
    }


def shortlist_prompt(meta: dict) -> str:
    """훑기 프롬프트. 유형 이름은 Jira 프로젝트에 있는 것으로 채운다."""
    names = {level: ", ".join(t for t in meta["issue_types"] if meta["levels"][t] == level) for level in (jira.STANDARD, jira.SUBTASK)}
    return (
        _prompt_file("match_shortlist.md")
        .replace("{subtask_types}", names[jira.SUBTASK])
        .replace("{standard_types}", names[jira.STANDARD])
    )


def run(page_text: str, candidates: list[dict], backlog: dict[str, dict], meta: dict) -> tuple[dict, list[dict]]:
    epics = [i for i in backlog.values() if i["level"] == jira.EPIC]
    epic_lines = []
    for e in epics:
        desc = jira.issue_detail(e["key"])["description"].replace("\n", " ")
        epic_lines.append(f"{e['key']} {e['summary']}: {desc[:200]}")

    # 1단계: 훑기
    prompt = (
        f"<에픽>\n" + "\n".join(epic_lines) + "\n</에픽>\n\n"
        f"<백로그>\n키 | 유형 | 상태 | 에픽 > 스토리 | 제목\n{backlog_text(backlog)}\n</백로그>\n\n"
        f"<후보>\n{_json([_candidate_view(c) for c in candidates])}\n</후보>"
    )
    shortlist, usage1 = llm.call(shortlist_prompt(meta), prompt, shortlist_schema([e["key"] for e in epics]))

    # 코드가 본문을 가져온다. 백로그에 없는 키는 여기서 버리고 검사 단계에서 오류로 알린다
    keys = sorted({k for a in shortlist["assignments"] for k in a["look_keys"] if k in backlog})
    details = {k: jira.issue_detail(k) for k in keys}

    # 2단계: 판단
    look = {a["id"]: a["look_keys"] for a in shortlist["assignments"]}
    views = [_candidate_view(c) | {"look_keys": look.get(c["id"], [])} for c in candidates]
    detail_block = "\n\n".join(jira.detail_text(d) for d in details.values()) or "(없음)"
    prompt = (
        f"<회의록>\n{page_text}\n</회의록>\n\n"
        f"<후보>\n{_json(views)}\n</후보>\n\n"
        f"<기존 이슈 본문>\n{detail_block}\n</기존 이슈 본문>"
    )
    judged, usage2 = llm.call(_prompt_file("match_judge.md"), prompt, judge_schema())

    usage1["step"], usage2["step"] = "대조 1단계(훑기)", "대조 2단계(판단)"
    return {"shortlist": shortlist, "judge": judged, "details": details}, [usage1, usage2]
