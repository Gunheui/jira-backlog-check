"""Confluence 회의록 페이지를 읽어 글자만 남긴다.

Confluence 본문은 storage 형식(HTML 비슷한 XML)이다. AI에게 주는 글과 근거 확인에 쓰는 글이
같아야 인용 검사가 맞으므로, 변환은 여기 한 곳에서만 한다.
"""

from dataclasses import dataclass, field
from html.parser import HTMLParser

import requests

from . import config


@dataclass
class Page:
    id: str
    title: str
    url: str
    version: int
    text: str
    action_items: list[str] = field(default_factory=list)


BLOCK_TAGS = {"p", "div", "table", "tr", "blockquote", "pre", "ac:task-list"}


class _StorageToText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.current = ""
        self.list_depth = 0
        self.in_task_status = False
        self.task_done = False
        self.task_body: str | None = None
        self.action_items: list[str] = []

    def _flush(self):
        if self.current.strip():
            self.lines.append(self.current.rstrip())
        self.current = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self.lines.append("")
            self.current = "#" * int(tag[1]) + " "
        elif tag in ("ul", "ol"):
            self._flush()
            self.list_depth += 1
        elif tag == "li":
            self._flush()
            self.current = "  " * (self.list_depth - 1) + "- "
        elif tag == "ac:task":
            self._flush()
            self.task_done = False
        elif tag == "ac:task-status":
            self.in_task_status = True
        elif tag == "ac:task-body":
            self.current = "- [x] " if self.task_done else "- [ ] "
            self.task_body = ""
        elif tag == "time":
            self._text(attrs.get("datetime", ""))
        elif tag == "br":
            self._flush()
        elif tag in ("td", "th"):
            self.current += " | "
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in ("ul", "ol"):
            self._flush()
            self.list_depth = max(0, self.list_depth - 1)
        elif tag == "ac:task-status":
            self.in_task_status = False
        elif tag == "ac:task-body":
            if self.task_body is not None and self.task_body.strip():
                self.action_items.append(self.task_body.strip())
            self.task_body = None
            self._flush()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "li") or tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self.in_task_status:
            self.task_done = data.strip() == "complete"
            return
        self._text(data)

    def _text(self, data: str):
        self.current += data
        if self.task_body is not None:
            self.task_body += data


def storage_to_text(storage: str) -> tuple[str, list[str]]:
    parser = _StorageToText()
    parser.feed(storage)
    parser._flush()
    text = "\n".join(parser.lines).strip()
    return text, parser.action_items


def get_page(page_id: str) -> Page:
    r = requests.get(
        f"{config.SITE}/wiki/api/v2/pages/{page_id}",
        auth=config.AUTH,
        params={"body-format": "storage"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    text, action_items = storage_to_text(data["body"]["storage"]["value"])
    return Page(
        id=str(data["id"]),
        title=data["title"],
        url=f"{config.SITE}/wiki{data['_links']['webui']}",
        version=data["version"]["number"],
        text=f"# {data['title']}\n\n{text}",
        action_items=action_items,
    )


# ---- 쓰기: 검토 문서 ----


def get_storage(page_id: str) -> dict:
    r = requests.get(f"{config.SITE}/wiki/api/v2/pages/{page_id}", auth=config.AUTH, params={"body-format": "storage"}, timeout=30)
    r.raise_for_status()
    d = r.json()
    return {"title": d["title"], "version": d["version"]["number"], "storage": d["body"]["storage"]["value"],
            "space_id": d["spaceId"], "url": f"{config.SITE}/wiki{d['_links']['webui']}"}


def title_exists(space_id: str, title: str) -> bool:
    """같은 스페이스에 같은 제목의 페이지가 있는가. Confluence는 한 스페이스에 같은 제목을 둘 수 없다."""
    r = requests.get(f"{config.SITE}/wiki/api/v2/spaces/{space_id}/pages", auth=config.AUTH, params={"title": title, "limit": 1}, timeout=30)
    r.raise_for_status()
    return bool(r.json()["results"])


def create_page(space_id: str, parent_id: str, title: str, storage: str) -> dict:
    r = requests.post(f"{config.SITE}/wiki/api/v2/pages", auth=config.AUTH, timeout=30, json={
        "spaceId": space_id, "parentId": parent_id, "status": "current", "title": title,
        "body": {"representation": "storage", "value": storage},
    })
    if not r.ok:
        raise RuntimeError(f"Confluence 페이지 생성 실패 {r.status_code}: {r.text[:300]}")
    d = r.json()
    return {"id": str(d["id"]), "url": f"{config.SITE}/wiki{d['_links']['webui']}"}


def get_labels(page_id: str) -> list[str]:
    r = requests.get(f"{config.SITE}/wiki/rest/api/content/{page_id}/label", auth=config.AUTH, timeout=30)
    r.raise_for_status()
    return [x["name"] for x in r.json()["results"]]


def add_label(page_id: str, label: str) -> None:
    r = requests.post(f"{config.SITE}/wiki/rest/api/content/{page_id}/label", auth=config.AUTH, timeout=30,
                      json=[{"prefix": "global", "name": label}])
    if not r.ok:
        raise RuntimeError(f"Confluence 라벨 추가 실패 {r.status_code}: {r.text[:300]}")


def update_page(page_id: str, storage: str, message: str) -> None:
    cur = get_storage(page_id)
    r = requests.put(f"{config.SITE}/wiki/api/v2/pages/{page_id}", auth=config.AUTH, timeout=30, json={
        "id": page_id, "status": "current", "title": cur["title"],
        "body": {"representation": "storage", "value": storage},
        "version": {"number": cur["version"] + 1, "message": message},
    })
    if not r.ok:
        raise RuntimeError(f"Confluence 페이지 수정 실패 {r.status_code}: {r.text[:300]}")
