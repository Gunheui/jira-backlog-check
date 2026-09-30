"""사람이 내린 결정 하나를 Jira에 반영한다(새로 만들기, 합치기, 다시 열기, 만들지 않음).

회의록 한 편(analyze 결과 파일 하나)이 Review 하나다. 어떤 결정을 할지는 검토 문서(review_doc)가 읽어 온다.
결정 상태는 결과 파일 옆 `.state.json`에 저장해서, 같은 요청이 두 번 와도 이미 반영한 후보를 다시 만들지 않는다.
"""

import itertools
import json
import threading
from datetime import datetime
from pathlib import Path

from . import config, jira, templates


class NotReady(Exception):
    """지금은 반영할 수 없는 경우(상위 후보가 아직 반영되지 않음 등). 반영 전 검사가 먼저 걸러 주므로 보통은 나지 않는다."""


class DryJira:
    """시험 실행용. Jira에 쓰지 않고 할 일을 출력만 한다."""

    _n = itertools.count(1)

    def create_issue(self, fields):
        key = f"DRY-{next(self._n)}"
        print(f"  (시험) 생성 {key}: [{fields['issuetype']['name']}] {fields['summary']} ← {fields['parent']['key']}")
        return key

    def add_comment(self, key, body):
        print(f"  (시험) 댓글 {key}")

    def link(self, link_type, a, b):
        print(f"  (시험) 링크 {a} {link_type} {b}")

    def reopen(self, key):
        print(f"  (시험) 다시 열기 {key}")
        return "해야 할 일"

    def myself_id(self):
        return "dry-run"


class Review:
    def __init__(self, run_path: Path, dry_run: bool = False):
        self.path = run_path
        self.name = run_path.name
        self.run = json.loads(run_path.read_text(encoding="utf-8"))
        self.dry_run = dry_run
        self.state_path = run_path.with_suffix(".dry.state.json" if dry_run else ".state.json")
        self.state = (
            json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state_path.exists()
            else {"decisions": {}}
        )
        self.lock = threading.Lock()
        self.overrides: dict[str, dict] = {}  # 검토 문서에서 고친 연관 이슈·선행 작업
        self.jira = DryJira() if dry_run else jira
        _, self.templates = templates.load()
        self.meta = jira.project_meta()

        self.candidates = {c["id"]: c for c in self.run["extract"]["candidates"]}
        self.assigned = {a["id"]: a for a in self.run["match"]["shortlist"]["assignments"]}
        self.judged = {j["id"]: j for j in self.run["match"]["judge"]["results"]}
        self.details = self.run["match"]["details"]
        self.flags = {
            cid: self.run["verify"]["per_candidate"].get(cid, []) + self.run["match_verify"]["per_candidate"].get(cid, [])
            for cid in self.candidates
        }
        self.backlog = jira.all_issues()  # 검토 문서에 에픽·스토리 이름을 보여 주고, 반영 전 검사에서 키를 확인할 때 쓴다

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 결정 ----

    def decide(self, cid: str, action: str, target: str | None = None, edited: dict | None = None) -> dict:
        """결정 하나를 처리한다. 이미 결정된 후보면 처리하지 않고 기존 결정을 돌려준다."""
        with self.lock:
            done = self.state["decisions"].get(cid)
            if done:
                return {**done, "already": True}
            if action in ("create", "edit"):
                self._parent_for(cid, edited)  # 상위가 준비 안 됐으면 여기서 NotReady
            self.state["decisions"][cid] = {"status": "처리 중"}
            self.save()
        try:
            result = self._apply(cid, action, target, edited)
        except Exception:
            with self.lock:
                del self.state["decisions"][cid]
                self.save()
            raise
        with self.lock:
            result |= {"status": "완료", "at": datetime.now().isoformat(timespec="seconds")}
            self.state["decisions"][cid] = result
            self.save()
        return result

    def _apply(self, cid: str, action: str, target: str | None, edited: dict | None) -> dict:
        c = self.candidates[cid]
        page = self.run["page"]
        description = (edited or {}).get("description") or c["description"]
        if action == "skip":
            return {"action": "만들지 않음", "key": None}
        if action == "merge":
            body = jira.adf_doc(
                [{"type": "paragraph", "content": [{"type": "text", "text": f"회의에서 다시 다룸: {c['title']}"}]}],
                jira.text_to_adf(description),
                jira.source_blocks(page["title"], page["url"], c["evidence"]),
            )
            self.jira.add_comment(target, body)
            return {"action": "합치기", "key": target}
        if action == "reopen":
            status = self.jira.reopen(target)
            body = jira.adf_doc(
                [{"type": "paragraph", "content": [{"type": "text", "text": f"재발 가능성으로 다시 엶: {c['title']}"}]}],
                jira.text_to_adf(description),
                jira.source_blocks(page["title"], page["url"], c["evidence"]),
            )
            self.jira.add_comment(target, body)
            return {"action": "재발로 다시 열기", "key": target, "note": f"{status}로 되돌림"}

        # create, edit
        edited = edited or {}
        title = edited.get("title") or c["title"]
        issue_type = edited.get("issue_type") or c["issue_type"]
        component = edited.get("component") or c["component"]
        template = templates.for_type(self.templates, issue_type)
        fields = {
            "summary": title,
            "issuetype": {"name": issue_type},
            "components": [{"name": component}],
            "parent": {"key": self._parent_for(cid, edited)},
            "description": jira.adf_doc(
                jira.text_to_adf(description, template.sections if template else set()),
                jira.source_blocks(page["title"], page["url"], c["evidence"]),
            ),
        }
        if edited.get("priority"):
            fields["priority"] = {"name": edited["priority"]}
        # 담당자는 API 토큰 주인만 넣을 수 있다. 회의록에 그 직군이 담당으로 적힌 이슈에만 넣고 나머지는 비워 둔다
        mine = config.SELF_ASSIGN_COMPONENT
        if mine and component == mine and mine in (c["assignee_in_notes"] or ""):
            fields["assignee"] = {"accountId": self.jira.myself_id()}
        key = self.jira.create_issue(fields)
        notes = self._link_created(cid, key)
        return {"action": "수정 후 만들기" if action == "edit" else "새로 만들기", "key": key, "note": "; ".join(notes) or None}

    def _parent_for(self, cid: str, edited: dict | None) -> str:
        c, a = self.candidates[cid], self.assigned[cid]
        edited = edited or {}
        if edited.get("parent"):
            return edited["parent"]
        issue_type = edited.get("issue_type") or c["issue_type"]
        if not jira.is_subtask(self.meta, issue_type):
            return a["epic_key"]
        if c["parent_id"]:
            d = self.state["decisions"].get(c["parent_id"])
            if not d or d.get("status") != "완료":
                raise NotReady(f"상위 후보 {c['parent_id']}가 아직 반영되지 않음")
            if d.get("key"):
                return d["key"]
            raise NotReady(f"상위 후보 {c['parent_id']}는 만들지 않기로 해서 들어갈 스토리가 없음")
        if a["parent_story_key"]:
            return a["parent_story_key"]
        raise NotReady("들어갈 스토리가 없음")

    def _resolved_key(self, ref: str) -> str | None:
        """후보 id(C3)면 결정된 이슈 키로, 이슈 키면 그대로."""
        if ref in self.candidates:
            d = self.state["decisions"].get(ref)
            return d.get("key") if d and d.get("status") == "완료" else None
        return ref

    def links_of(self, cid: str) -> tuple[list[str], list[str]]:
        """(연관 이슈, 선행 작업). 검토 문서에서 사람이 고친 값이 있으면 그것, 없으면 AI 판단."""
        if cid in self.overrides:
            o = self.overrides[cid]
            return o["related"], o["blocked_by"]
        j = self.judged[cid]
        return [m["key"] for m in j["matches"]], [b["ref"] for b in j["blocked_by"]]

    def _link_created(self, cid: str, key: str) -> list[str]:
        """새 이슈에 관련 이슈와 선행 작업을 링크한다. 링크가 실패해도 생성은 되돌리지 않고 메모만 남긴다."""
        notes = []
        related, blocked_by = self.links_of(cid)
        pairs = []
        for ref in related:
            other = self._resolved_key(ref)
            if other and other != key:
                pairs.append(("Relates", key, other))
        for ref in blocked_by:  # 이 후보를 막는 일
            blocker = self._resolved_key(ref)
            if blocker:
                pairs.append(("Blocks", blocker, key))
        for other_id in self.candidates:  # 이 후보가 막는, 먼저 만들어진 일
            if other_id != cid and cid in self.links_of(other_id)[1]:
                blocked = self._resolved_key(other_id)
                if blocked:
                    pairs.append(("Blocks", key, blocked))
        for link_type, a, b in pairs:
            try:
                self.jira.link(link_type, a, b)
            except Exception as e:
                notes.append(f"링크 실패 {a} {link_type} {b}: {e}")
        return notes

    # ---- 분류 ----

    def needs_judgement(self, cid: str) -> bool:
        """확인 요청 후보(체크를 풀어 두는 후보)인가. 코드 검사 표시, AI 주의 표시, 새로 만들기가 아닌 제안, 중복·재발 판단 중 하나라도 있으면."""
        j = self.judged[cid]
        return bool(
            self.flags[cid]
            or j["caution"]
            or j["suggested_action"] != "새로 만들기"
            or any(m["relation"] in ("중복", "재발") for m in j["matches"])
        )
