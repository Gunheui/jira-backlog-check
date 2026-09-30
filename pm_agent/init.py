"""`pm-agent init`: 질문에 답하면 작업 폴더에 `.env`를 만든다.

Confluence 자동화 규칙은 API로 만들 수 없어 여기서 대신 해 주지 못한다. 끝에 남은 순서를 안내한다.
"""

import getpass
import secrets
import shutil

from . import config

BACKENDS = ["claude-cli", "codex-cli", "api"]


def _ask(question: str, default: str = "", secret: bool = False) -> str:
    hint = f" [{default}]" if default else ""
    answer = (getpass.getpass if secret else input)(f"{question}{hint}: ").strip()
    return answer or default


def _yes(question: str) -> bool:
    return input(f"{question} [Y/n]: ").strip().lower() in ("", "y", "yes")


def _answers() -> dict[str, str]:
    a = {}
    site = _ask("Atlassian 사이트 주소 (예: https://your-site.atlassian.net)").rstrip("/").removesuffix("/wiki")
    if site:
        a["ATLASSIAN_SITE"] = site if site.startswith("http") else f"https://{site}"
    a["ATLASSIAN_EMAIL"] = _ask("Atlassian 계정 이메일")
    print("API 토큰은 https://id.atlassian.com/manage-profile/security/api-tokens 에서 만듭니다. 입력한 글자는 화면에 보이지 않습니다")
    a["ATLASSIAN_API_TOKEN"] = _ask("Atlassian API 토큰", secret=True)
    a["JIRA_PROJECT_KEY"] = _ask("Jira 프로젝트 키 (이슈 번호 앞 글자, 예: ECL)").upper()
    a["CONFLUENCE_SPACE_KEY"] = _ask("Confluence 스페이스 키 (주소의 /spaces/ 다음 글자)")

    a["SLACK_BOT_TOKEN"] = _ask("Slack 봇 토큰 xoxb-… (알림을 안 쓰면 Enter)", secret=True)
    if a["SLACK_BOT_TOKEN"]:
        a["SLACK_CHANNEL"] = _ask("알림을 받을 Slack 채널 이름", "pm-agent").lstrip("#")

    backend = _ask(f"모델 호출 방식 ({', '.join(BACKENDS)})", "claude-cli")
    while backend not in BACKENDS:
        backend = _ask(f"{', '.join(BACKENDS)} 중 하나", "claude-cli")
    a["LLM_BACKEND"] = backend
    if backend == "api":
        a["ANTHROPIC_API_KEY"] = _ask("Anthropic API 키", secret=True)

    a["SELF_ASSIGN_COMPONENT"] = _ask("내 직군의 Jira 컴포넌트 이름 (회의록에 이 직군이 담당으로 적힌 이슈에 나를 담당자로 넣음. 안 넣으려면 Enter)")
    # 서버 주소가 공개되므로 추측할 수 없는 값으로 만든다. 사람이 정하게 두면 짧은 값을 쓰기 쉽다
    a["WEBHOOK_SECRET"] = secrets.token_urlsafe(32)
    return a


def _write_env(path, answers: dict[str, str]) -> None:
    lines = []
    for line in (config.PACKAGE_DIR / "env.example").read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0]
        lines.append(f"{key}={answers[key]}" if "=" in line and key in answers else line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)  # 토큰이 들어 있어 본인만 읽게


def run() -> None:
    env_path = config.WORK_DIR / ".env"
    if env_path.exists():
        print(f"{env_path} 가 이미 있어 그대로 둡니다. 값을 바꾸려면 파일을 직접 고치거나 지우고 다시 실행하세요")
    else:
        print(f"이 폴더({config.WORK_DIR})에 .env를 만듭니다. 나중에 파일을 열어 고칠 수 있습니다\n")
        _write_env(env_path, _answers())
        print(f"\n[OK] {env_path} 저장 (웹훅 비밀 값 WEBHOOK_SECRET은 자동으로 만들어 넣었습니다)")

    team_templates = config.WORK_DIR / "templates"
    if team_templates.exists():
        print(f"{team_templates} 가 이미 있어 그대로 둡니다")
    elif _yes("\n이슈 양식을 이 폴더의 templates/로 복사할까요? 팀 양식이나 Jira 유형 이름에 맞게 고칠 수 있습니다"):
        shutil.copytree(config.PACKAGE_DIR / "default_templates", team_templates)
        print(f"[OK] {team_templates} 복사")

    print(
        "\n다음 순서\n"
        "1. pm-agent check            설정과 연결 확인\n"
        "2. pm-agent analyze --page <회의록 페이지 ID>   손으로 한 번 돌려 보기(Jira, Confluence에 쓰지 않음)\n"
        "3. 자동 실행을 쓰려면 서버에서 pm-agent serve를 띄우고 Confluence 스페이스 자동화 규칙 2개를 만든다(README의 자동 실행 설정).\n"
        "   규칙의 X-PM-Agent-Secret 헤더에는 .env의 WEBHOOK_SECRET 값을 넣는다"
    )
