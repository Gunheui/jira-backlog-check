"""`.env` 값을 한 곳에서 읽는다.

작업 폴더 = 명령을 실행한 폴더. `.env`, `runs/`(실행 결과), `templates/`(팀 양식, 있으면)를 여기서 찾는다.
프롬프트와 기본 양식은 패키지 안에 들어 있어서 `uv tool install`로 설치해도 따라온다.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
WORK_DIR = Path.cwd()
load_dotenv(WORK_DIR / ".env")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name) or default
    if value is None:
        raise SystemExit(f"[설정 없음] .env에 {name}을 채워 주세요 (`pm-agent init`으로 만들 수 있음)")
    return value


# 꼭 있어야 하는 값은 쓸 때 읽는다. `.env`가 아직 없는 상태에서도 `pm-agent init`과 도움말이 돌아야 해서다
_REQUIRED = {
    "SITE": lambda: env("ATLASSIAN_SITE").rstrip("/"),
    "AUTH": lambda: (env("ATLASSIAN_EMAIL"), env("ATLASSIAN_API_TOKEN")),
    "JIRA_PROJECT": lambda: env("JIRA_PROJECT_KEY"),
}


def __getattr__(name: str):
    if name in _REQUIRED:
        return _REQUIRED[name]()
    raise AttributeError(name)


LLM_BACKEND = env("LLM_BACKEND", "claude-cli")
# 모델을 바꿔 실험할 때 여기만 바꾼다. codex-cli는 비워 두면 Codex 기본 모델을 쓴다
LLM_MODEL = os.environ.get("LLM_MODEL") or ("claude-sonnet-5-5" if LLM_BACKEND != "codex-cli" else "")

# 팀 양식: TEMPLATE_DIR로 지정한 폴더, 없으면 작업 폴더의 templates/, 그것도 없으면 패키지의 기본 양식
_team_templates = WORK_DIR / (os.environ.get("TEMPLATE_DIR") or "templates")
TEMPLATE_DIR = _team_templates if _team_templates.is_dir() else PACKAGE_DIR / "default_templates"
PROMPT_DIR = PACKAGE_DIR / "prompts"
RUNS_DIR = WORK_DIR / "runs"

# Confluence 자동화 규칙이 거르는 라벨과, 사람이 '끝났다'는 신호로 바꾸는 페이지 상태.
# 상태는 모든 스페이스에 기본으로 있는 '확인됨'(영어 화면은 Verified)을 쓴다. 상태는 API로 만들 수 없어서다
MEETING_LABEL = os.environ.get("MEETING_LABEL") or "회의록"
REVIEW_LABEL = os.environ.get("REVIEW_LABEL") or "이슈검토"
DONE_STATUS = os.environ.get("DONE_STATUS") or "확인됨"

# 이슈 유형은 이름이 아니라 Jira 계층 번호로 구분한다(jira.py). 버그만 계층으로 알 수 없어 이름으로 찾는다
BUG_TYPES = {os.environ["BUG_ISSUE_TYPE"]} if os.environ.get("BUG_ISSUE_TYPE") else {"버그", "Bug"}
# 회의록에 이 직군이 담당으로 적힌 이슈는 API 토큰 주인을 담당자로 넣는다. 비우면 담당자 칸을 채우지 않는다
SELF_ASSIGN_COMPONENT = os.environ.get("SELF_ASSIGN_COMPONENT") or None
