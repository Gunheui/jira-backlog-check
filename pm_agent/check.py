"""`pm-agent check`: 설정과 연결을 확인한다. 읽기만 한다(`--send-test`를 주면 Slack에 시험 메시지 한 건).

도구가 실제로 기대는 것만 본다. 예: 후보를 배정할 에픽이 있는가, 직군으로 쓸 컴포넌트가 있는가,
'확인됨' 상태가 스페이스에 있는가. 설치한 사람이 첫 실행에서 헤매지 않게 하려는 것이다.
"""

import os

import requests

from . import config, extract, jira, llm, notify, templates

LINK_TYPES = ["Relates", "Blocks"]  # 반영할 때 쓰는 Jira 링크 유형


class Report:
    def __init__(self):
        self.failed = 0

    def ok(self, message: str) -> None:
        print(f"  [OK]   {message}")

    def warn(self, message: str) -> None:
        print(f"  [주의] {message}")

    def fail(self, message: str) -> None:
        self.failed += 1
        print(f"  [실패] {message}")


def _settings(r: Report) -> None:
    env_path = config.WORK_DIR / ".env"
    (r.ok if env_path.exists() else r.warn)(f"작업 폴더 {config.WORK_DIR}" + ("" if env_path.exists() else " (.env 없음, `pm-agent init`으로 만들 수 있음)"))
    default = config.TEMPLATE_DIR == config.PACKAGE_DIR / "default_templates"
    r.ok("이슈 양식: " + ("패키지 기본 양식" if default else str(config.TEMPLATE_DIR)))
    r.ok(f"라벨 '{config.MEETING_LABEL}'(회의록), '{config.REVIEW_LABEL}'(검토 문서), 끝났다는 상태 '{config.DONE_STATUS}'")


def _jira(r: Report) -> None:
    r.ok(f"Jira 인증: {jira._get('/myself')['displayName']}")
    meta = jira.project_meta()
    levels = meta["levels"]

    def names(level: int) -> list[str]:
        return [n for n, lv in levels.items() if lv == level]

    (r.ok if names(jira.EPIC) else r.fail)(f"에픽 계층 유형: {', '.join(names(jira.EPIC)) or '없음'}")
    (r.ok if names(jira.STANDARD) else r.fail)(f"일반 계층 유형: {', '.join(names(jira.STANDARD)) or '없음'}")
    (r.ok if names(jira.SUBTASK) else r.warn)(f"하위 작업 계층 유형: {', '.join(names(jira.SUBTASK)) or '없음(스토리 아래에 작업을 둘 수 없음)'}")

    _, tpls = templates.load()
    in_templates = [t for tpl in tpls for t in tpl.issue_types]
    unknown = [t for t in in_templates if t not in meta["issue_types"]]
    if not set(in_templates) - set(unknown):
        r.fail(f"양식의 유형({', '.join(in_templates)})이 Jira 프로젝트에 하나도 없음. 양식 파일의 issue_types를 Jira 유형 이름으로 고쳐야 함")
    elif unknown:
        r.warn(f"양식에는 있는데 Jira 프로젝트에 없는 유형(빼고 진행): {', '.join(unknown)}")
    else:
        r.ok(f"양식의 유형이 모두 Jira 프로젝트에 있음: {', '.join(in_templates)}")
    bugs = [n for n in levels if jira.is_bug(n)]
    (r.ok if bugs else r.warn)(f"버그 유형: {', '.join(bugs)}" if bugs else "버그 유형을 찾지 못함. 이름이 '버그', 'Bug'가 아니면 .env의 BUG_ISSUE_TYPE에 적어야 재발 판단과 버그 제목 검사가 동작")

    components = meta["components"]
    (r.ok if components else r.fail)(f"컴포넌트(직군): {', '.join(components)}" if components else "컴포넌트가 없음. 직군을 컴포넌트로 구분하므로 하나 이상 만들어야 함(프로젝트 설정 → 컴포넌트)")
    mine = config.SELF_ASSIGN_COMPONENT
    if mine and mine not in components:
        r.fail(f"SELF_ASSIGN_COMPONENT '{mine}'이 컴포넌트에 없음")

    backlog = jira.all_issues()
    epics = [i for i in backlog.values() if i["level"] == jira.EPIC]
    (r.ok if epics else r.fail)(f"백로그 {len(backlog)}건, 에픽 {len(epics)}개" + ("" if epics else ". 후보를 에픽에 배정하므로 에픽이 하나 이상 있어야 함"))

    link_types = [t["name"] for t in jira._get("/issueLinkType")["issueLinkTypes"]]
    missing = [t for t in LINK_TYPES if t not in link_types]
    (r.fail if missing else r.ok)(f"링크 유형 없음: {', '.join(missing)}" if missing else f"링크 유형: {', '.join(LINK_TYPES)}")
    priorities = [p["name"] for p in jira._get("/priority")]
    missing = [p for p in extract.PRIORITIES if p not in priorities]
    (r.warn if missing else r.ok)(f"우선순위 이름이 기본값과 다름(없는 것: {', '.join(missing)}). 우선순위 칸은 비워 두고 쓰기" if missing else f"우선순위: {', '.join(extract.PRIORITIES)}")


def _confluence(r: Report) -> None:
    key = config.env("CONFLUENCE_SPACE_KEY")
    spaces = requests.get(f"{config.SITE}/wiki/api/v2/spaces", auth=config.AUTH, params={"keys": key}, timeout=30)
    spaces.raise_for_status()
    found = spaces.json()["results"]
    if not found:
        r.fail(f"Confluence 스페이스 '{key}'를 찾지 못함")
        return
    r.ok(f"Confluence 스페이스: {found[0]['name']}")
    # 상태와 자동화 규칙은 API로 만들 수 없다. 상태가 있는지만 읽어서 알려 준다
    states = requests.get(f"{config.SITE}/wiki/rest/api/space/{key}/state", auth=config.AUTH, timeout=30)
    states.raise_for_status()
    state_names = [s["name"] for s in states.json()]
    if config.DONE_STATUS in state_names:
        r.ok(f"페이지 상태 '{config.DONE_STATUS}'가 스페이스에 있음")
    else:
        r.fail(f"페이지 상태 '{config.DONE_STATUS}'가 스페이스에 없음(있는 상태: {', '.join(state_names) or '없음'}). .env의 DONE_STATUS를 있는 이름으로 바꾸거나 스페이스 설정에서 상태를 켜야 함")
    r.warn("자동화 규칙 2개는 확인할 수 없음(API 없음). 스페이스 설정 → 자동화에서 직접 확인")


def _slack(r: Report, send_test: bool) -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        r.warn("SLACK_BOT_TOKEN이 없어 알림 없이 동작")
        return
    from slack_sdk import WebClient

    auth = WebClient(token=token).auth_test()
    r.ok(f"Slack 인증: {auth['team']} 워크스페이스, 봇 {auth['user']}")
    scopes = (auth.headers.get("x-oauth-scopes") or "").split(",")
    (r.ok if "chat:write" in scopes else r.fail)("메시지 쓰기 권한(chat:write) " + ("있음" if "chat:write" in scopes else "없음"))
    channel = config.env("SLACK_CHANNEL")
    if send_test:
        notify.send("🔧 pm-agent check: 연결 확인 메시지입니다. 지워도 됩니다.")
        r.ok(f"#{channel.lstrip('#')} 채널에 시험 메시지를 보냄")
    elif "chat:write.public" not in scopes:
        r.warn(f"봇을 #{channel.lstrip('#')} 채널에 초대했는지는 --send-test로 확인")


def _llm(r: Report, skip: bool) -> None:
    label = f"{config.LLM_BACKEND}, {config.LLM_MODEL or '기본 모델'}"
    if skip:
        r.warn(f"모델 호출 확인을 건너뜀({label})")
        return
    schema = {"type": "object", "additionalProperties": False, "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    data, usage = llm.call("묻는 말에 JSON으로만 답한다.", "ok 칸에 true를 넣어 답해 줘.", schema)
    (r.ok if data.get("ok") is True else r.fail)(f"모델 호출({label}): {usage['seconds']}초, 답 {data}")


def _server(r: Report) -> None:
    if os.environ.get("WEBHOOK_SECRET"):
        r.ok("WEBHOOK_SECRET 있음(pm-agent serve에 필요)")
    else:
        r.warn("WEBHOOK_SECRET이 없음. 손으로 돌리는 명령은 되지만 pm-agent serve는 뜨지 않음")


def run(send_test: bool = False, skip_llm: bool = False) -> int:
    """문제가 있으면 1을 돌려준다(종료 코드)."""
    r = Report()
    sections = [
        ("설정", _settings),
        ("Jira", _jira),
        ("Confluence", _confluence),
        ("Slack", lambda r: _slack(r, send_test)),
        ("모델", lambda r: _llm(r, skip_llm)),
        ("서버", _server),
    ]
    for title, section in sections:
        print(title)
        try:
            section(r)
        except SystemExit as e:  # config.env가 빠진 설정을 알릴 때
            r.fail(str(e))
        except requests.ConnectionError:
            r.fail("사이트에 연결할 수 없음. .env의 ATLASSIAN_SITE 주소와 네트워크를 확인")
        except requests.HTTPError as e:
            r.fail(f"HTTP {e.response.status_code}: {e.response.url.split('?')[0]} (주소, 이메일, API 토큰, 키를 확인)")
        except Exception as e:
            r.fail(f"{type(e).__name__}: {e}")
    print(f"\n{'문제 없음' if not r.failed else f'실패 {r.failed}건'}")
    return 1 if r.failed else 0
