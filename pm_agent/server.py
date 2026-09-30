"""Confluence 자동화의 웹 요청을 받아 도구를 실행한다.

- POST /hooks/meeting-published  {"page_id": "..."}  회의록 상태 '확인됨' → analyze → 검토 문서 만들기
- POST /hooks/review-done        {"page_id": "..."}  검토 문서 상태 '확인됨' → 검사 후 반영

어떤 페이지를 보낼지는 Confluence 자동화 규칙의 라벨 조건이 먼저 거른다(회의록 → 규칙 1, 이슈검토 → 규칙 2).
서버도 한 번 더 확인한다. 규칙은 스페이스 관리자가 고칠 수 있어서, 조건이 빠져도 엉뚱한 페이지를 처리하지 않게 하려는 것이다.
- 회의록: 회의록 라벨이 있고 이슈검토 라벨이 없어야 한다
- 검토 문서: 도구가 만든 문서 목록(runs/review_pages.json)에 있어야 한다(doc_apply가 확인)
- GET  /health

Confluence는 응답을 30초까지만 기다려서, 요청은 곧바로 202로 받고 일은 뒤에서 하나씩 처리한다.
요청이 Confluence 자동화에서 온 것인지는 비밀 헤더(X-PM-Agent-Secret)로 확인한다. 서버 주소가 공개돼 있어서다.
"""

import hmac
import json
import queue
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config

SECRET_HEADER = "X-PM-Agent-Secret"

jobs: "queue.Queue[tuple[str, str]]" = queue.Queue()
pending: set[tuple[str, str]] = set()  # 같은 페이지에 대한 같은 일이 대기 중이면 또 넣지 않는다
pending_lock = threading.Lock()


def _worker() -> None:
    from . import __main__ as cli
    from . import confluence, notify

    while True:
        kind, page_id = jobs.get()
        try:
            if kind == "meeting":
                labels = confluence.get_labels(page_id)
                if config.MEETING_LABEL not in labels or config.REVIEW_LABEL in labels:
                    print(f"[건너뜀] 회의록 라벨이 없거나 검토 문서임: {page_id} {labels}")
                    continue
                run = cli.analyze(page_id)
                cli.doc_create(config.RUNS_DIR / run["file"])
            elif kind == "review":
                cli.doc_apply(page_id, None, dry_run=False)
        except SystemExit as e:
            print(f"[중단] {kind} {page_id}: {e}")
        except Exception as e:
            traceback.print_exc()
            try:
                notify.send(f"✗ 도구 실행 실패 ({kind}, 페이지 {page_id}): {e}")
            except Exception:
                traceback.print_exc()
        finally:
            with pending_lock:
                pending.discard((kind, page_id))
            jobs.task_done()


class Handler(BaseHTTPRequestHandler):
    routes = {"/hooks/meeting-published": "meeting", "/hooks/review-done": "review"}

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True, "queued": jobs.qsize()})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        kind = self.routes.get(self.path)
        if kind is None:
            return self._reply(404, {"error": "not found"})
        if not hmac.compare_digest(self.headers.get(SECRET_HEADER, ""), config.env("WEBHOOK_SECRET")):
            return self._reply(401, {"error": "bad secret"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            page_id = str(body["page_id"]).strip()
            if not page_id.isdigit():
                raise ValueError(page_id)
        except Exception:
            return self._reply(400, {"error": "page_id가 필요함"})
        with pending_lock:
            if (kind, page_id) in pending:
                return self._reply(200, {"status": "이미 대기 중"})
            pending.add((kind, page_id))
        jobs.put((kind, page_id))
        print(f"[받음] {kind} {page_id}")
        self._reply(202, {"status": "받음", "kind": kind, "page_id": page_id})

    def log_message(self, fmt, *args):  # 기본 접근 로그 대신 위의 print만 남긴다
        pass


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    config.env("WEBHOOK_SECRET")  # 없으면 여기서 멈춘다
    threading.Thread(target=_worker, daemon=True).start()
    print(f"[서버] {host}:{port} 대기 (백엔드 {config.LLM_BACKEND})")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
