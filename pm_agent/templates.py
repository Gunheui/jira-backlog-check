"""이슈 양식 템플릿(`templates/*.md`)을 읽는다.

GitLab의 `.gitlab/issue_templates/`처럼 양식을 레포 파일로 둔다. Jira에는 유형별 템플릿 기능이 기본으로 없어서다.
파일 맨 위 `---` 사이의 설정은 도구가 검사에 쓰고, 본문은 그대로 AI에게 준다.
"""

from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass
class Template:
    name: str
    issue_types: list[str]
    required_sections: list[str]
    needs_assignee: bool  # 아니면 회의록에 담당이 없어도 확인 요청으로 올리지 않는다(스토리처럼 묶음인 유형)
    body: str

    @property
    def sections(self) -> set[str]:
        """설명 양식(코드 블록) 안의 칸 이름. 목록 줄이 아니고 뒤의 (조건) 설명을 뗀 줄"""
        block = self.body.split("```")[1] if self.body.count("```") >= 2 else ""
        names = set()
        for line in block.splitlines():
            s = line.strip()
            if s and not s.startswith("-") and not s[:1].isdigit():
                names.add(s.split("(")[0].strip())
        return names


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    head, _, body = text[3:].partition("\n---")
    meta = {}
    for line in head.strip().splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, body.lstrip("\n")


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def load(template_dir: Path = config.TEMPLATE_DIR) -> tuple[dict, list[Template]]:
    """(공통 규칙 설정, 공통 규칙 본문 포함), 유형별 템플릿 목록을 돌려준다."""
    meta, body = _split_front_matter((template_dir / "rules.md").read_text(encoding="utf-8"))
    rules = {
        "title_max_length": int(meta.get("title_max_length", 40)),
        "title_forbidden": _list(meta.get("title_forbidden", "")),
        "bug_title_prefixes": _list(meta.get("bug_title_prefixes", "")),
        "body": body,
    }
    templates = []
    for path in sorted(template_dir.glob("*.md")):
        if path.name == "rules.md":
            continue
        meta, body = _split_front_matter(path.read_text(encoding="utf-8"))
        templates.append(
            Template(
                name=path.stem,
                issue_types=_list(meta.get("issue_types", "")),
                required_sections=_list(meta.get("required_sections", "")),
                needs_assignee=meta.get("needs_assignee", "yes") != "no",
                body=body,
            )
        )
    return rules, templates


def for_type(templates: list[Template], issue_type: str) -> Template | None:
    return next((t for t in templates if issue_type in t.issue_types), None)
