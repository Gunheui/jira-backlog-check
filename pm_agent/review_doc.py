"""④ 사람 확인: Confluence 검토 문서.

도구가 회의록 아래에 검토 문서를 만든다. 사람은 문서에서 결정 칸과 제목·유형·상위·설명을 고치고,
페이지 상태를 '확인됨'(config.DONE_STATUS)으로 바꾸면 도구가 문서를 다시 읽어 검사한 뒤 Jira에 반영한다.

문서 틀(도구가 다시 읽는 부분):
- 확정 후보(AI 제안이 새로 만들기이고 걸린 것이 없음), 확인 요청 후보(나머지) 순서
- 후보마다 체크박스 줄(체크 = 반영, 해제 = 만들지 않음) + 그 아래 펼치기. 체크박스 줄 'C8 제목 · 제안'만 먼저 훑고, 펼치면 칸 표(결정, 제목, 유형, 직군, 상위, 우선순위, 이번 스프린트, 대상 이슈, 연관 이슈, 선행 작업) + 설명 + '근거 보기'
- 처음 체크 상태: 확정 후보는 체크, 확인 요청 후보는 해제(사람이 펼쳐 보고 체크해야 반영)
- 도구는 체크박스 줄과 펼치기 제목 맨 앞의 후보 번호(C8)로 후보를 찾는다
나머지(안내 상자, AI 제안 상자, 근거)는 사람이 고쳐도 도구가 읽지 않는다.
반영이 끝나면 문서를 결과 표(후보, 결정, 이슈 키, 제목, 유형, 상위)로 바꿔 쓴다. 검토하던 모습은 페이지 기록에 남는다.
"""

import re
from html import escape

from . import config, jira, templates
from .extract import PRIORITIES, SPRINT

DECISIONS = ["새로 만들기", "합치기", "다시 열기"]  # 만들지 않음은 체크 해제로
FIELD_ROWS = ["결정", "대상 이슈", "제목", "유형", "직군", "상위", "우선순위", "이번 스프린트", "연관 이슈", "선행 작업"]
RESULT_COLUMNS = ["후보", "결정", "Jira", "제목", "유형", "상위", "우선순위", "이번 스프린트"]
ACTION_TO_DECISION = {"새로 만들기": "새로 만들기", "합치기": "합치기", "재발로 다시 열기": "다시 열기"}


def _e(s: str) -> str:
    return escape(s or "", quote=False)


def _jira_link(key: str) -> str:
    return f'<a href="{config.SITE}/browse/{key}">{key}</a>'


def _link_keys(text: str) -> str:
    """글 속의 이슈 키(ECL-60)를 Jira 링크로. 링크 글자는 키 그대로라 다시 읽을 때 값이 바뀌지 않는다."""
    return re.sub(rf"\b{re.escape(config.JIRA_PROJECT)}-\d+\b", lambda m: _jira_link(m.group(0)), _e(text))


def _macro(name: str, body: str, title: str | None = None) -> str:
    param = f'<ac:parameter ac:name="title">{_e(title)}</ac:parameter>' if title else ""
    return f'<ac:structured-macro ac:name="{name}">{param}<ac:rich-text-body>{body}</ac:rich-text-body></ac:structured-macro>'


def description_storage(text: str, sections: set[str]) -> str:
    """이슈 설명 글을 Confluence 본문으로. 칸 이름은 굵게, '- ' 줄은 목록."""
    out, items, kind = [], [], None

    def flush():
        nonlocal items, kind
        if items:
            out.append(f"<{kind}>" + "".join(f"<li>{_e(i)}</li>" for i in items) + f"</{kind}>")
        items, kind = [], None

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        this = "ul" if s.startswith("- ") else "ol" if s[:1].isdigit() and ". " in s[:4] else None
        if this:
            if kind != this:
                flush()
                kind = this
            items.append(s.split(" ", 1)[1])
            continue
        flush()
        out.append(f"<p><strong>{_e(s)}</strong></p>" if s in sections else f"<p>{_e(s)}</p>")
    flush()
    return "".join(out)


def _parent_label(r, cid: str) -> str:
    c, a = r.candidates[cid], r.assigned[cid]
    if c["parent_id"]:
        return f"{c['parent_id']} {r.candidates[c['parent_id']]['title']}"
    key = a["parent_story_key"] or a["epic_key"]
    return f"{key} {r.backlog[key]['summary']}"


# 정해진 값만 쓰는 칸은 색깔 배지(status 매크로)로. 노션의 선택 칸처럼 보이지만 목록에서 고르는 기능은 없어
# 사람이 글자를 고치고, 틀린 값은 반영 전 검사가 막는다
LOZENGE_ROWS = {"결정", "유형", "직군", "우선순위", "이번 스프린트"}
LOZENGE_COLOUR = {
    "새로 만들기": "Green", "합치기": "Blue", "다시 열기": "Yellow",
    "Highest": "Red", "High": "Red", "Medium": "Yellow", "Low": "Blue", "Lowest": "Blue",
    "포함": "Green", "미정": "Grey", "제외": "Red",
}


def _lozenge(value: str) -> str:
    colour = LOZENGE_COLOUR.get(value, "Grey")
    return (
        '<ac:structured-macro ac:name="status">'
        f'<ac:parameter ac:name="colour">{colour}</ac:parameter>'
        f'<ac:parameter ac:name="title">{_e(value)}</ac:parameter>'
        "</ac:structured-macro>"
    )


def _suggestion_panel(r, cid: str) -> str:
    j = r.judged[cid]
    target = f" {_jira_link(j['target_key'])}" if j["target_key"] else ""
    lines = [f"<p><strong>AI 제안: {_e(j['suggested_action'])}</strong>{target} ({_e(j['short_reason'])})</p>"]
    warns = []
    if j["caution"]:
        warns.append(_e(j["caution"]))
    for b in j["blocked_by"]:
        ref = b["ref"]
        label = f"{ref} {_e(r.candidates[ref]['title'])}" if ref in r.candidates else _jira_link(ref)
        warns.append(f"먼저 끝날 일: {label}")
    for f in r.flags[cid]:
        warns.append(("검사 오류: " if f["level"] == "오류" else "") + _e(f["message"]))
    if warns:
        lines.append("<ul>" + "".join(f"<li>{w}</li>" for w in warns) + "</ul>")
    related = [m for m in j["matches"] if m["key"] != j["target_key"]]
    if related:
        lines.append("<p>관련 이슈: " + ", ".join(
            f"{'' if m['relation'] == '관련' else m['relation'] + ' '}{_jira_link(m['key'])} {_e(r.details.get(m['key'], {}).get('summary', ''))}"
            for m in related) + "</p>")
    return _macro("note" if warns else "info", "".join(lines))


def _evidence(r, cid: str) -> str:
    c, j = r.candidates[cid], r.judged[cid]
    parts = [
        "<p><strong>회의록 근거</strong></p>",
        "<blockquote>" + "".join(f"<p>{_e(q)}</p>" for q in c["evidence"]) + "</blockquote>",
        f"<p>출처: {_e(c['source'])} / 회의 결정: {_e(c['decided'])} / 담당(회의록): {_e(c['assignee_in_notes'] or '없음')} / 시점(회의록): {_e(c['due_in_notes'] or '없음')}</p>",
        f"<p><strong>AI 판단 이유</strong></p><p>{_e(j['reason'])}</p>",
    ]
    if c["priority"]:
        parts.append(f"<p><strong>우선순위 {_e(c['priority'])} 근거</strong></p><blockquote><p>{_e(c['priority_quote'])}</p></blockquote>")
    if c["sprint_quote"]:
        parts.append(f"<p><strong>이번 스프린트 {_e(c['sprint'])} 근거</strong></p><blockquote><p>{_e(c['sprint_quote'])}</p></blockquote>")
    for m in j["matches"]:
        summary = r.details.get(m["key"], {}).get("summary", "")
        parts.append(f"<p><strong>{_e(m['relation'])}</strong> {_jira_link(m['key'])} {_e(summary)}: {_e(m['reason'])}</p>")
        parts.append(f"<blockquote><p>{_e(m['quote'])}</p></blockquote>")
    for key, notes in r.run.get("resolution_notes", {}).items():
        if notes and any(m["key"] == key and m["relation"] == "재발" for m in j["matches"]):
            parts.append(f"<p><strong>{_jira_link(key)} 과거 해결 기록</strong></p>")
            parts.append("<blockquote>" + "".join(f"<p>{_e(line)}</p>" for n in notes for line in n.splitlines()) + "</blockquote>")
    flags = r.flags[cid]
    parts.append("<p><strong>코드 검사</strong>: " + (", ".join(_e(f"{f['level']} {f['message']}") for f in flags) if flags else "걸린 것 없음") + "</p>")
    return _macro("expand", "".join(parts), title="근거 보기")


def allowed_values(meta: dict) -> dict[str, str]:
    """칸 표 셋째 열. 사람이 고칠 때 옆에서 보는 안내이고, 도구는 이 열을 읽지 않는다.

    Confluence 기본 상태 배지는 칸별로 선택지를 나눌 수 없어서(공식 문서, 2026-09-30 리서치) 값을 글로 보여 준다."""
    types = meta["issue_types"]
    top = [t for t in types if not jira.is_subtask(meta, t)]
    sub = [t for t in types if jira.is_subtask(meta, t)]
    return {
        "결정": " · ".join(DECISIONS),
        "대상 이슈": "합치기, 다시 열기일 때 이슈 키",
        "제목": "40자 안팎, 버그만 [영역] 머리말",
        "유형": " · ".join(top) + " / 스토리 아래: " + " · ".join(sub),
        "직군": " · ".join(meta["components"]),
        "상위": f"이슈 키({config.JIRA_PROJECT}-7) 또는 후보 번호(C1)",
        "우선순위": " · ".join(PRIORITIES) + " · 빈칸",
        "이번 스프린트": " · ".join(SPRINT),
        "연관 이슈": "이슈 키, 후보 번호 (쉼표로 나눔)",
        "선행 작업": "먼저 끝나야 할 이슈 키, 후보 번호",
    }


def _field_table(r, cid: str) -> str:
    c, j = r.candidates[cid], r.judged[cid]
    values = {
        "결정": ACTION_TO_DECISION[j["suggested_action"]],
        "제목": c["title"],
        "유형": c["issue_type"],
        "직군": c["component"],
        "상위": _parent_label(r, cid),
        "우선순위": c["priority"] or "",
        "이번 스프린트": c["sprint"],
        "대상 이슈": j["target_key"] or "",
        "연관 이슈": ", ".join(m["key"] for m in j["matches"] if m["key"] != j["target_key"]),
        "선행 작업": ", ".join(b["ref"] for b in j["blocked_by"]),
    }
    def cell(k: str) -> str:
        return _lozenge(values[k]) if k in LOZENGE_ROWS and values[k] else _link_keys(values[k])

    hints = r.allowed_values
    rows = "".join(
        f"<tr><th><p>{k}</p></th><td><p>{cell(k)}</p></td>"
        f'<td><p><span style="color: #6B778C;">{_e(hints[k])}</span></p></td></tr>'
        for k in FIELD_ROWS
    )
    # 칸 이름은 좁게, 값은 넓게, 안내는 중간. 한글은 기울임꼴이 없어 안내는 회색 보통 글씨로
    cols = '<colgroup><col style="width: 110.0px;" /><col style="width: 360.0px;" /><col style="width: 290.0px;" /></colgroup>'
    return f'<table data-table-width="760" data-layout="default">{cols}<tbody>{rows}</tbody></table>'



def _card(r, cid: str, checked: bool) -> str:
    """후보 하나 = 체크박스 줄 + 펼치기. 체크박스 줄만 봐도 AI 제안과 확인할 건수를 알 수 있게.

    펼치기 제목에는 체크박스를 넣을 수 없어(글자만 받는 칸) 바로 위 줄에 둔다."""
    c, j = r.candidates[cid], r.judged[cid]
    template = templates.for_type(r.templates, c["issue_type"])
    sections = template.sections if template else set()
    label = f"{cid} {c['title']} · {ACTION_TO_DECISION[j['suggested_action']]}"
    if j["target_key"]:
        label += f" → {j['target_key']}"
    if j["blocked_by"]:  # 선행 작업은 정보일 뿐 확인 요청이 아니다
        label += " · 선행 " + ", ".join(b["ref"] for b in j["blocked_by"])
    checks = len(r.flags[cid]) + (1 if j["caution"] else 0)
    if checks:
        label += f" · ⚠ 확인 {checks}건"
    body = (
        _suggestion_panel(r, cid)
        + _field_table(r, cid)
        + "<p><strong>Jira에 들어갈 설명</strong> (바로 고쳐도 됩니다)</p>"
        + description_storage(c["description"], sections)
        + _evidence(r, cid)
    )
    task = (
        "<ac:task-list><ac:task>"
        f"<ac:task-status>{'complete' if checked else 'incomplete'}</ac:task-status>"
        f"<ac:task-body>{_link_keys(label)}</ac:task-body>"
        "</ac:task></ac:task-list>"
    )
    return task + _macro("expand", body, title=f"{cid} 자세히")


def render(r) -> str:
    r.allowed_values = allowed_values(r.meta)
    page = r.run["page"]
    judge = [cid for cid in r.candidates if r.needs_judgement(cid)]
    bundle = [cid for cid in r.candidates if cid not in judge]
    guide = (
        f'<p>회의록: <a href="{page["url"]}">{_e(page["title"])}</a> · 후보 {len(r.candidates)}건</p>'
        "<ol>"
        "<li><strong>체크한 후보만 반영</strong>합니다. 체크를 풀면 만들지 않습니다. 확정 후보는 체크돼 있고, 확인 요청 후보는 펼쳐서 확인한 뒤 직접 체크해야 반영됩니다</li>"
        "<li>'자세히'를 누르면 펼쳐집니다. <strong>결정</strong> 칸은 반영 방식입니다. 쓸 수 있는 값: " + ", ".join(DECISIONS) + "</li>"
        "<li>색깔 배지(결정, 유형, 직군, 우선순위, 이번 스프린트)는 배지를 눌러 글자를 바꿉니다. 칸마다 쓸 수 있는 값은 표 오른쪽에 있고, 그 밖의 값은 반영 전 검사에서 막힙니다</li>"
        f"<li>제목, 유형, 직군, 상위, 설명도 이 문서에서 바로 고칩니다. 직군은 {_e(', '.join(r.meta['components']))} 중 하나. 상위 칸은 들어갈 에픽이나 스토리로, 이슈 키({config.JIRA_PROJECT}-7)나 후보 번호(C1)로 시작하면 됩니다</li>"
        "<li>합치기나 다시 열기는 <strong>대상 이슈</strong> 칸에 이슈 키가 있어야 합니다</li>"
        "<li>우선순위는 Highest, High, Medium, Low, Lowest 중 하나이고 비워 둘 수 있습니다. AI는 회의록에 근거가 있을 때만 채웠습니다. 이번 스프린트는 포함, 제외, 미정 중 하나</li>"
        "<li>연관 이슈와 선행 작업은 쉼표로 나눈 이슈 키나 후보 번호입니다. 지우면 링크하지 않고, 더하면 반영 전에 있는 키인지 검사합니다</li>"
        f"<li>다 되면 페이지 상태를 <strong>{_e(config.DONE_STATUS)}</strong>으로 바꿉니다. 도구가 문서를 다시 검사하고, 문제가 없을 때만 Jira에 반영합니다</li>"
        "</ol>"
        "<p>AI 제안 상자와 '근거 보기'는 참고용이라 고쳐도 반영되지 않습니다. 체크박스 줄과 '자세히' 맨 앞의 번호(C1 등)는 지우지 마세요. 도구가 후보를 찾는 표시입니다.</p>"
    )
    parts = [_macro("info", guide, title="검토 방법")]
    if bundle:
        parts.append(f"<h1>확정 후보 ({len(bundle)}건)</h1>")
        parts.append("<p>AI 제안이 새로 만들기이고 코드 검사와 AI 모두 걸린 것이 없는 후보입니다. 체크돼 있으니 빼고 싶은 것만 체크를 풀면 됩니다.</p>")
        parts += [_card(r, cid, checked=True) for cid in bundle]
    if judge:
        parts.append(f"<h1>확인 요청 후보 ({len(judge)}건)</h1>")
        parts.append("<p>기존 이슈와 겹치거나, 회의록만으로 정할 수 없는 것이 있는 후보입니다. 체크가 풀려 있으니 펼쳐 확인하고 반영할 것만 체크하세요.</p>")
        parts += [_card(r, cid, checked=False) for cid in judge]
    return "".join(parts)


def render_result(r, rows: list[dict]) -> str:
    """반영이 끝난 뒤의 문서. rows: 후보마다 {id, decision, key, title, issue_type, parent, note}."""
    page = r.run["page"]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["decision"]] = counts.get(row["decision"], 0) + 1
    summary = ", ".join(f"{k} {v}건" for k, v in counts.items())
    head = "<tr>" + "".join(f"<th><p>{h}</p></th>" for h in RESULT_COLUMNS) + "</tr>"
    body = []
    for row in rows:
        key = _jira_link(row["key"]) if row.get("key") and not row["key"].startswith("DRY-") else _e(row.get("key") or "")
        note = f"<br/>{_e(row['note'])}" if row.get("note") else ""
        cells = [row["id"], _e(row["decision"]), key + note, _e(row["title"]), _e(row["issue_type"]), _e(row["parent"]),
                 _e(row.get("priority") or ""), _e(row.get("sprint") or "")]
        body.append("<tr>" + "".join(f"<td><p>{c}</p></td>" for c in cells) + "</tr>")
    info = (
        f'<p>회의록: <a href="{page["url"]}">{_e(page["title"])}</a> · 후보 {len(rows)}건 · {summary}</p>'
        "<p>검토하던 문서는 페이지 기록(오른쪽 위 ⋯ → 페이지 기록)에서 볼 수 있습니다. 이슈 설명은 Jira에 들어가 있습니다.</p>"
    )
    return _macro("tip", info, title="Jira 반영 완료") + f"<table><tbody>{head}{''.join(body)}</tbody></table>"


# ---- 다시 읽기: 사람이 고친 문서 → 후보별 값 ----

from html.parser import HTMLParser

CANDIDATE_ID = re.compile(r"^\s*(C\d+)\b")
ISSUE_URL = re.compile(r"/browse/([A-Z][A-Z0-9_]*-\d+)")
VOID_TAGS = {"br", "hr", "img", "col", "input", "meta", "link"}


class _Node:
    def __init__(self, tag: str, attrs: dict, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, attrs, parent, []

    def text(self) -> str:
        inner = "".join(ch if isinstance(ch, str) else ch.text() for ch in self.children)
        if self.tag == "a" and "/browse/" in inner:
            # 편집기에서 Jira 주소를 붙여 넣으면 글자가 주소 전체인 스마트 링크가 된다. 키만 남긴다
            m = ISSUE_URL.search(inner)
            return m.group(1) if m else inner
        return inner

    def find_all(self, tag: str) -> list:
        found = []
        for ch in self.children:
            if isinstance(ch, _Node):
                if ch.tag == tag:
                    found.append(ch)
                found += ch.find_all(tag)
        return found

    def child(self, tag: str):
        return next((ch for ch in self.children if isinstance(ch, _Node) and ch.tag == tag), None)


class _TreeBuilder(HTMLParser):
    """Confluence storage(XHTML)를 나무 구조로. 편집기가 속성을 덧붙여도 태그 구조만 보고 읽는다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.cur = _Node("root", {})

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self.cur)
        self.cur.children.append(node)
        if tag not in VOID_TAGS:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(_Node(tag, dict(attrs), self.cur))

    def handle_endtag(self, tag):
        node = self.cur
        while node is not self.root and node.tag != tag:  # 닫는 태그가 어긋나도 버티게
            node = node.parent
        if node is not self.root:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def _tree(storage: str) -> _Node:
    b = _TreeBuilder()
    b.feed(storage)
    return b.root


def _macro_name(node: _Node) -> str:
    return node.attrs.get("ac:name", "")


def _macro_title(node: _Node) -> str:
    for p in node.children:
        if isinstance(p, _Node) and p.tag == "ac:parameter" and p.attrs.get("ac:name") == "title":
            return p.text().strip()
    return ""


def _lines(node: _Node) -> list[str]:
    """설명 본문 노드를 템플릿 모양의 글 줄로(문단 → 줄, 목록 → '- ')."""
    if node.tag in ("ul", "ol"):
        out = []
        for i, li in enumerate((c for c in node.children if isinstance(c, _Node) and c.tag == "li"), start=1):
            nested = [c for c in li.children if isinstance(c, _Node) and c.tag in ("ul", "ol")]
            own = "".join(c if isinstance(c, str) else ("" if c in nested else c.text()) for c in li.children).strip()
            if own:
                out.append(("- " if node.tag == "ul" else f"{i}. ") + own)
            for n in nested:
                out += _lines(n)
        return out
    text = node.text().strip()
    return [text] if text else []


def _cell_value(td: _Node) -> str:
    """칸 값. 상태 배지가 있으면 배지 글자(배지 안의 색 이름 같은 설정 값은 빼고)."""
    badges = [m for m in td.find_all("ac:structured-macro") if _macro_name(m) == "status"]
    if badges:
        return ", ".join(_macro_title(b) for b in badges)
    return "\n".join(s for s in (" ".join(p.split()) for p in td.text().splitlines()) if s).strip()


def parse(storage: str) -> dict:
    """검토 문서에서 후보별 {checked, fields, description}을 꺼낸다. 문서 구조 문제는 problems에 모은다."""
    root = _tree(storage)
    result: dict[str, dict] = {}
    problems: list[str] = []

    for task in root.find_all("ac:task"):
        body = task.child("ac:task-body")
        status = task.child("ac:task-status")
        m = CANDIDATE_ID.match(body.text() if body else "")
        if not m:
            continue
        cid = m.group(1)
        if cid in result:
            problems.append(f"{cid} 체크박스가 두 개 있음")
            continue
        result[cid] = {"checked": (status.text().strip() if status else "") == "complete", "fields": None, "description": ""}

    for macro in root.find_all("ac:structured-macro"):
        if _macro_name(macro) != "expand":
            continue
        # 제목 맨 앞의 후보 번호로 찾는다. 제목 나머지 글자는 편집기나 사람이 바꿀 수 있어 보지 않는다
        m = CANDIDATE_ID.match(_macro_title(macro))
        if not m:
            continue
        cid = m.group(1)
        body = macro.child("ac:rich-text-body")
        blocks = [c for c in body.children if isinstance(c, _Node)] if body is not None else []
        table = next((b for b in blocks if b.tag == "table"), None)
        if table is None:
            continue  # 칸 표가 없는 펼치기는 후보 본문이 아님
        if cid not in result:
            problems.append(f"{cid} 자세히는 있는데 체크박스가 없음")
            continue
        if result[cid]["fields"] is not None:
            problems.append(f"{cid} 자세히(칸 표)가 두 개 있음")
            continue
        fields = {}
        for tr in table.find_all("tr"):
            cells = [c for c in tr.children if isinstance(c, _Node) and c.tag in ("th", "td")]
            if len(cells) >= 2:
                fields[" ".join(cells[0].text().split())] = _cell_value(cells[1])
        # 설명: 표 다음부터 '근거 보기' 펼치기 전까지. 안내 문구 줄('Jira에 들어갈 설명')은 뺀다
        lines = []
        for b in blocks[blocks.index(table) + 1:]:
            if b.tag == "ac:structured-macro" and _macro_name(b) == "expand":
                break
            if b.tag == "ac:structured-macro":
                continue
            lines += _lines(b)
        lines = [l for l in lines if not l.startswith("Jira에 들어갈 설명")]
        result[cid]["fields"] = fields
        result[cid]["description"] = "\n".join(lines)

    for cid, v in result.items():
        if v["fields"] is None:
            problems.append(f"{cid} 체크박스는 있는데 자세히(칸 표)가 없음")
    return {"candidates": result, "problems": problems}


# ---- 반영 전 검사와 반영 ----


REF = re.compile(r"^\s*([A-Z][A-Z0-9]+-\d+|C\d+)\b")
DECISION_TO_ACTION = {"새로 만들기": "create", "합치기": "merge", "다시 열기": "reopen"}


def _first_ref(text: str) -> str | None:
    m = REF.match(text or "")
    return m.group(1) if m else None


def _refs(text: str) -> list[str]:
    return [r for r in (_first_ref(part) for part in (text or "").split(",")) if r]


def check(r, parsed: dict, meta: dict, rules: dict) -> tuple[dict, list[str], list[str]]:
    """문서 값을 검사해 후보별 반영 계획을 만든다. 오류가 하나라도 있으면 반영하지 않는다(일부만 반영되면 어디까지 됐는지 헷갈려서)."""
    errors, warnings = list(parsed["problems"]), []
    found = parsed["candidates"]
    for cid in r.candidates:
        if cid not in found:
            errors.append(f"{cid}: 문서에서 찾지 못함(체크박스 줄 맨 앞 번호가 지워졌는지 확인)")
    for cid in found:
        if cid not in r.candidates:
            errors.append(f"{cid}: 이 회의록에 없는 후보 번호")

    plan: dict[str, dict] = {}
    for cid, v in found.items():
        if cid not in r.candidates or v["fields"] is None:
            continue
        if not v["checked"]:
            plan[cid] = {"action": "skip"}
            continue
        f = v["fields"]
        canon = {x.lower(): x for x in PRIORITIES}
        p = {
            "action": DECISION_TO_ACTION.get(f.get("결정", "").strip()),
            "title": f.get("제목", "").strip(),
            "issue_type": f.get("유형", "").strip(),
            "component": f.get("직군", "").strip(),
            "parent": _first_ref(f.get("상위", "")),
            "priority": canon.get(f.get("우선순위", "").strip().lower(), f.get("우선순위", "").strip()) or None,
            "sprint": f.get("이번 스프린트", "").strip(),
            "target": _first_ref(f.get("대상 이슈", "")),
            "related": _refs(f.get("연관 이슈", "")),
            "blocked_by": _refs(f.get("선행 작업", "")),
            "description": v["description"],
        }
        bad = lambda what: errors.append(f"{cid} {what}")  # noqa: E731
        if p["action"] is None:
            bad(f"결정 칸 값 '{f.get('결정', '')}'은 쓸 수 없음({', '.join(DECISIONS)} 중 하나, 만들지 않으려면 체크 해제)")
        if not p["title"]:
            bad("제목이 비어 있음")
        if p["issue_type"] not in meta["issue_types"]:
            bad(f"유형 '{p['issue_type']}'은 Jira에 없음({', '.join(meta['issue_types'])})")
        if p["component"] not in meta["components"]:
            bad(f"직군 '{p['component']}'은 Jira에 없음({', '.join(meta['components'])})")
        if p["priority"] and p["priority"] not in PRIORITIES:
            bad(f"우선순위 '{p['priority']}'은 쓸 수 없음({', '.join(PRIORITIES)} 또는 빈칸)")
        if p["sprint"] not in SPRINT:
            bad(f"이번 스프린트 '{p['sprint']}'은 쓸 수 없음({', '.join(SPRINT)})")
        if p["action"] in ("merge", "reopen") and not p["target"]:
            bad("합치기나 다시 열기는 대상 이슈 칸에 이슈 키가 필요함")
        if p["action"] == "create" and not p["parent"]:
            bad("상위 칸에 이슈 키나 후보 번호가 없음")
        plan[cid] = p

    # 서로 얽힌 값(상위, 대상, 링크)은 전체 계획을 본 뒤에 검사한다
    backlog = r.backlog
    for cid, p in plan.items():
        if p["action"] in (None, "skip"):
            continue
        bad = lambda what: errors.append(f"{cid} {what}")  # noqa: E731
        if p["action"] == "create" and p["parent"]:
            parent, is_sub = p["parent"], jira.is_subtask(meta, p["issue_type"])
            if parent in r.candidates:
                pp = plan.get(parent)
                if not pp or pp["action"] in (None, "skip"):
                    bad(f"상위 {parent}가 체크 해제돼 있어 들어갈 곳이 없음")
                elif not is_sub:
                    bad(f"{p['issue_type']}는 에픽 아래에만 둘 수 있는데 상위가 후보 {parent}")
                elif pp["action"] == "create" and jira.is_subtask(meta, pp["issue_type"]):
                    bad(f"상위 {parent}도 하위 작업이라 그 아래에 둘 수 없음")
            elif parent not in backlog:
                bad(f"상위 {parent}는 Jira에 없음")
            elif is_sub and backlog[parent]["level"] != jira.STANDARD:
                standard = ", ".join(t for t in meta["issue_types"] if not jira.is_subtask(meta, t))
                bad(f"하위 작업은 {standard} 아래에 둬야 하는데 상위 {parent}는 {backlog[parent]['type']}")
            elif not is_sub and backlog[parent]["level"] != jira.EPIC:
                bad(f"{p['issue_type']}는 에픽 아래에 둬야 하는데 상위 {parent}는 {backlog[parent]['type']}")
        if p["action"] in ("merge", "reopen") and p["target"]:
            t = backlog.get(p["target"])
            if t is None:
                bad(f"대상 이슈 {p['target']}는 Jira에 없음")
            elif p["action"] == "merge" and t["done"]:
                warnings.append(f"{cid} 합칠 대상 {p['target']}는 이미 완료된 이슈(댓글만 남김)")
            elif p["action"] == "reopen" and not t["done"]:
                bad(f"다시 열 대상 {p['target']}는 아직 열려 있음(합치기가 맞음)")
        for field in ("related", "blocked_by"):
            for ref in p[field]:
                if ref in r.candidates:
                    if plan.get(ref, {}).get("action") in (None, "skip"):
                        warnings.append(f"{cid} {'연관 이슈' if field == 'related' else '선행 작업'} {ref}는 반영하지 않는 후보라 링크하지 않음")
                elif ref not in backlog:
                    bad(f"{'연관 이슈' if field == 'related' else '선행 작업'} {ref}는 Jira에 없음")
        # 제목 규칙은 막지 않고 알려만 준다(사람이 고친 제목이라)
        if p["title"]:
            for ch in rules["title_forbidden"]:
                if ch in p["title"]:
                    warnings.append(f"{cid} 제목에 쓰지 않기로 한 기호 '{ch}'")
    return plan, errors, warnings


def apply(r, plan: dict) -> list[dict]:
    """계획대로 반영하고 결과 표의 행을 돌려준다. 상위가 후보인 것은 상위를 먼저 처리한 뒤에."""
    for cid, p in plan.items():
        if p["action"] not in ("skip", None):
            r.overrides[cid] = {"related": p["related"], "blocked_by": p["blocked_by"]}
    order = sorted(plan, key=lambda cid: plan[cid].get("parent") in r.candidates)
    rows = []
    for cid in order:
        p, c = plan[cid], r.candidates[cid]
        if p["action"] == "skip":
            d = r.decide(cid, "skip")
            rows.append({"id": cid, "decision": "만들지 않음", "key": None, "title": c["title"], "issue_type": c["issue_type"],
                         "parent": "", "priority": "", "sprint": ""})
            continue
        edited = {k: p[k] for k in ("title", "issue_type", "component", "priority", "description")}
        if p["action"] == "create":
            parent = r._resolved_key(p["parent"])
            edited["parent"] = parent
            changed = (
                p["title"] != c["title"] or p["issue_type"] != c["issue_type"] or p["component"] != c["component"]
                or p["description"].split() != c["description"].split()
            )
            d = r.decide(cid, "edit" if changed else "create", edited=edited)
        else:
            d = r.decide(cid, p["action"], p["target"], edited=edited)
            parent = ""
        rows.append({"id": cid, "decision": d["action"], "key": d.get("key"), "note": d.get("note"), "title": p["title"],
                     "issue_type": p["issue_type"], "parent": parent or "", "priority": p["priority"] or "", "sprint": p["sprint"]})
    rows.sort(key=lambda row: int(row["id"][1:]))
    return rows


# ---- 문서에 결과·오류 쓰기 ----

ERROR_TITLE = "반영 전 검사에서 문제가 있습니다"
_ERROR_PANEL = re.compile(
    r'<ac:structured-macro[^>]*ac:name="warning"[^>]*>\s*<ac:parameter ac:name="title">' + ERROR_TITLE + r".*?</ac:structured-macro>",
    re.S,
)


def with_errors(storage: str, errors: list[str], warnings: list[str]) -> str:
    """사람이 고친 문서는 그대로 두고 맨 위에 오류 상자만 넣는다(이전 오류 상자는 바꿔 끼운다)."""
    body = f"<p>아래를 고친 뒤 페이지 상태를 다른 상태로 바꿨다가 다시 '{_e(config.DONE_STATUS)}'으로 바꿔 주세요. 아무것도 반영하지 않았습니다.</p>"
    body += "<ul>" + "".join(f"<li>{_e(e)}</li>" for e in errors) + "</ul>"
    if warnings:
        body += "<p>참고(반영은 막지 않음)</p><ul>" + "".join(f"<li>{_e(w)}</li>" for w in warnings) + "</ul>"
    return _macro("warning", body, title=ERROR_TITLE) + _ERROR_PANEL.sub("", storage)
