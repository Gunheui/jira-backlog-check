"""① 회의록 → 이슈 후보. AI가 하는 단계."""

from . import config, llm
from .confluence import Page
from .templates import Template

PRIORITIES = ["Highest", "High", "Medium", "Low", "Lowest"]  # Jira 우선순위 이름
SPRINT = ["포함", "제외", "미정"]


def schema(issue_types: list[str], components: list[str]) -> dict:
    # 모든 칸을 required로 두고 없는 값은 null로 받는다. 백엔드마다 구조화 출력 규칙이 달라 가장 엄격한 쪽에 맞췄다
    nullable = {"type": ["string", "null"]}
    candidate = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "issue_type": {"type": "string", "enum": issue_types},
            "component": {"type": "string", "enum": components},
            "parent_id": nullable,
            "description": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "source": {"type": "string", "enum": ["액션 아이템", "논의"]},
            "assignee_in_notes": nullable,
            "due_in_notes": nullable,
            "decided": {"type": "string", "enum": ["결정", "미정"]},
            "issue_worthy": {"type": "boolean"},
            "issue_worthy_reason": nullable,
            "mentioned_issue_keys": {"type": "array", "items": {"type": "string"}},
            "backlog_hint": nullable,
            "priority": {"type": ["string", "null"], "enum": PRIORITIES + [None]},
            "priority_quote": nullable,
            "sprint": {"type": "string", "enum": SPRINT},
            "sprint_quote": nullable,
        },
        "required": [
            "id", "title", "issue_type", "component", "parent_id", "description", "evidence", "source",
            "assignee_in_notes", "due_in_notes", "decided", "issue_worthy", "issue_worthy_reason",
            "mentioned_issue_keys", "backlog_hint", "priority", "priority_quote", "sprint", "sprint_quote",
        ],
    }
    excluded = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"text": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["text", "reason"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "candidates": {"type": "array", "items": candidate},
            "excluded": {"type": "array", "items": excluded},
        },
        "required": ["candidates", "excluded"],
    }


def system_prompt(rules: dict, templates: list[Template]) -> str:
    parts = [(config.PROMPT_DIR / "extract.md").read_text(encoding="utf-8"), "# 작성 규칙\n\n" + rules["body"]]
    for t in templates:
        parts.append(f"# 양식: {', '.join(t.issue_types)}\n\n{t.body}")
    return "\n\n".join(parts)


def run(page: Page, rules: dict, templates: list[Template], meta: dict) -> tuple[dict, dict]:
    issue_types = [t for tpl in templates for t in tpl.issue_types if t in meta["issue_types"]]
    prompt = f"다음 회의록에서 이슈 후보를 뽑아 줘.\n\n<회의록>\n{page.text}\n</회의록>"
    return llm.call(system_prompt(rules, templates), prompt, schema(issue_types, meta["components"]))
