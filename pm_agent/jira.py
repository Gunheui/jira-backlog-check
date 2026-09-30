"""Jira REST 호출."""

import requests

from . import config

DONE = "done"  # Jira 상태 범주 키. 완료, Won't Do 모두 여기에 속한다

# 이슈 유형의 계층 번호. 유형 이름은 Jira 언어와 팀 설정마다 달라서(스토리/Story) 구조 검사는 이 번호로 한다
EPIC, STANDARD, SUBTASK = 1, 0, -1


def _get(path: str, **params) -> dict:
    r = requests.get(f"{config.SITE}/rest/api/3{path}", auth=config.AUTH, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def project_meta() -> dict:
    """프로젝트에 실제로 있는 이슈 유형(이름 → 계층 번호)과 컴포넌트. AI 출력의 선택지를 여기에 맞춘다."""
    project = _get(f"/project/{config.JIRA_PROJECT}")
    levels = {t["name"]: t.get("hierarchyLevel", STANDARD) for t in project["issueTypes"]}
    return {
        "levels": levels,
        "issue_types": [name for name, level in levels.items() if level <= STANDARD],  # 후보로 만들 수 있는 유형
        "components": [c["name"] for c in project.get("components", [])],
    }


def is_subtask(meta: dict, issue_type: str) -> bool:
    return meta["levels"].get(issue_type) == SUBTASK


def is_bug(issue_type: str) -> bool:
    return issue_type in config.BUG_TYPES


def all_issues() -> dict[str, dict]:
    """프로젝트의 모든 이슈를 가볍게(제목, 유형, 상태, 상위) 가져온다. 대조 1단계에서 AI가 훑는 목록이다."""
    issues, token = [], None
    while True:
        params = {
            "jql": f"project = {config.JIRA_PROJECT} ORDER BY key",
            "fields": "summary,issuetype,status,resolution,parent",
            "maxResults": 100,
        }
        if token:
            params["nextPageToken"] = token
        data = _get("/search/jql", **params)
        issues += data["issues"]
        token = data.get("nextPageToken")
        if not token or data.get("isLast"):
            break
    result = {}
    for i in issues:
        f = i["fields"]
        result[i["key"]] = {
            "key": i["key"],
            "summary": f["summary"],
            "type": f["issuetype"]["name"],
            "level": f["issuetype"].get("hierarchyLevel", STANDARD),
            "status": f["status"]["name"],
            "done": f["status"]["statusCategory"]["key"] == DONE,
            "resolution": (f.get("resolution") or {}).get("name"),
            "parent": (f.get("parent") or {}).get("key"),
        }
    # 에픽까지 거슬러 올라가 소속 에픽과 스토리를 채운다(하위 작업 → 스토리 → 에픽).
    # 여기서 스토리는 하위 작업을 담는 일반 계층 이슈를 말한다(스토리가 보통이지만 작업·버그일 수도 있다)
    for issue in result.values():
        story, epic, cur = None, None, issue
        while cur and cur["parent"] and cur["parent"] in result:
            cur = result[cur["parent"]]
            if cur["level"] == STANDARD:
                story = cur["key"]
            elif cur["level"] == EPIC:
                epic = cur["key"]
        issue["story"], issue["epic"] = story, epic if issue["level"] != EPIC else None
    return result


def issue_detail(key: str) -> dict:
    """대조 2단계에서 AI와 코드가 함께 보는 본문: 설명, 댓글(해결 기록 포함), 링크."""
    f = _get(f"/issue/{key}", fields="summary,issuetype,status,resolution,resolutiondate,description,comment,issuelinks")["fields"]
    comments = [
        {"created": c["created"][:10], "text": adf_to_text(c["body"])}
        for c in f.get("comment", {}).get("comments", [])
    ]
    links = []
    for link in f.get("issuelinks", []):
        if "outwardIssue" in link:
            links.append(f"{link['type']['outward']} {link['outwardIssue']['key']}")
        else:
            links.append(f"{link['type']['inward']} {link['inwardIssue']['key']}")
    return {
        "key": key,
        "summary": f["summary"],
        "type": f["issuetype"]["name"],
        "status": f["status"]["name"],
        "resolution": (f.get("resolution") or {}).get("name"),
        "resolved": (f.get("resolutiondate") or "")[:10] or None,
        "description": adf_to_text(f.get("description")),
        "comments": comments,
        "links": links,
    }


def detail_text(d: dict) -> str:
    """AI에게 주는 모양이자, 코드가 인용을 확인할 때 대조하는 글."""
    lines = [f"## {d['key']} {d['summary']}", f"유형: {d['type']} / 상태: {d['status']}"]
    if d["resolution"]:
        lines[-1] += f" / 해결: {d['resolution']} {d['resolved'] or ''}".rstrip()
    lines += ["", d["description"] or "(설명 없음)"]
    for c in d["comments"]:
        lines += ["", f"댓글 {c['created']}", c["text"]]
    if d["links"]:
        lines += ["", "링크: " + ", ".join(d["links"])]
    return "\n".join(lines)


def adf_to_text(node) -> str:
    """Jira 설명·댓글 형식(ADF, JSON 트리)을 글자로 바꾼다."""
    if not node:
        return ""
    lines: list[str] = []

    def inline(n) -> str:
        if n.get("type") == "text":
            return n.get("text", "")
        if n.get("type") == "hardBreak":
            return "\n"
        if n.get("type") in ("mention", "emoji", "date", "status", "inlineCard"):
            attrs = n.get("attrs", {})
            return attrs.get("text") or attrs.get("url") or attrs.get("shortName") or ""
        return "".join(inline(c) for c in n.get("content", []))

    def block(n, indent: str = "", bullet: str | None = None):
        t = n.get("type")
        if t in ("paragraph", "heading"):
            text = inline(n)
            prefix = "#" * n.get("attrs", {}).get("level", 0) + " " if t == "heading" else ""
            if text.strip():
                lines.append(indent + (bullet or "") + prefix + text)
                bullet = None
        elif t in ("bulletList", "orderedList"):
            for idx, item in enumerate(n.get("content", []), start=1):
                mark = "- " if t == "bulletList" else f"{idx}. "
                for j, child in enumerate(item.get("content", [])):
                    block(child, indent + ("  " if j else ""), mark if j == 0 else None)
        elif t == "codeBlock":
            lines.append(indent + inline(n))
        else:
            for child in n.get("content", []):
                block(child, indent, bullet)

    block(node)
    return "\n".join(lines)


# ---- 쓰기. 사람이 검토 문서에서 결정한 뒤에만 부른다 ----


def _send(method: str, path: str, body: dict | None = None) -> dict:
    r = requests.request(method, f"{config.SITE}/rest/api/3{path}", auth=config.AUTH, json=body, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Jira {method} {path} 실패 {r.status_code}: {r.text[:500]}")
    return r.json() if r.text else {}


def myself_id() -> str:
    return _get("/myself")["accountId"]


def create_issue(fields: dict) -> str:
    return _send("POST", "/issue", {"fields": {"project": {"key": config.JIRA_PROJECT}, **fields}})["key"]


def add_comment(key: str, body_adf: dict) -> None:
    _send("POST", f"/issue/{key}/comment", {"body": body_adf})


def link(link_type: str, from_key: str, to_key: str) -> None:
    """from_key가 링크 이름의 주어가 된다. 예: link("Blocks", A, B) → A blocks B."""
    # Jira API에서는 inwardIssue 쪽이 바깥 방향 이름(blocks)의 주어로 표시된다
    _send("POST", "/issueLink", {"type": {"name": link_type}, "inwardIssue": {"key": from_key}, "outwardIssue": {"key": to_key}})


def reopen(key: str) -> str:
    """완료된 이슈를 '해야 할 일' 범주 상태로 되돌린다. 옮긴 상태 이름을 돌려준다."""
    transitions = _get(f"/issue/{key}/transitions")["transitions"]
    target = next((t for t in transitions if t["to"]["statusCategory"]["key"] == "new"), None)
    if target is None:
        raise RuntimeError(f"{key}를 되돌릴 전환이 워크플로에 없음")
    _send("POST", f"/issue/{key}/transitions", {"transition": {"id": target["id"]}})
    return target["to"]["name"]


def text_to_adf(text: str, sections: set[str] = frozenset()) -> list[dict]:
    """템플릿 모양의 설명 글(칸 이름 줄, '- ' 줄, '1. ' 줄)을 Jira 설명 형식 블록으로 바꾼다. 칸 이름은 굵게."""
    blocks: list[dict] = []

    def para(s: str, strong: bool = False) -> dict:
        node = {"type": "text", "text": s}
        if strong:
            node["marks"] = [{"type": "strong"}]
        return {"type": "paragraph", "content": [node]}

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        kind = "bulletList" if s.startswith("- ") else "orderedList" if s[:1].isdigit() and ". " in s[:4] else None
        if kind:
            item = {"type": "listItem", "content": [para(s.split(" ", 1)[1])]}
            if blocks and blocks[-1]["type"] == kind:
                blocks[-1]["content"].append(item)
            else:
                blocks.append({"type": kind, "content": [item]})
        else:
            blocks.append(para(s, strong=s in sections))
    return blocks


def adf_doc(*block_lists: list[dict]) -> dict:
    return {"type": "doc", "version": 1, "content": [b for blocks in block_lists for b in blocks]}


def source_blocks(title: str, url: str, quotes: list[str]) -> list[dict]:
    """이슈 설명·댓글 끝에 붙이는 회의록 근거. AI가 쓰지 않고 코드가 붙인다."""
    return [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "근거: "},
            {"type": "text", "text": title, "marks": [{"type": "link", "attrs": {"href": url}}]},
        ]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": q}]}]} for q in quotes
        ]},
    ]
