"""Slack 알림. 검토 문서 체계에서 Slack은 알림만 맡는다(결정은 Confluence 문서에서)."""

import os

from slack_sdk import WebClient

from . import config


def send(text: str) -> None:
    if not os.environ.get("SLACK_BOT_TOKEN"):  # Slack 없이도 쓸 수 있게. 검토 문서 주소는 실행 로그에 남는다
        print(f"[알림 건너뜀] SLACK_BOT_TOKEN이 없음: {text.splitlines()[0]}")
        return
    ch = config.env("SLACK_CHANNEL").lstrip("#")
    channel = ch if ch[:1] in ("C", "G") and ch[1:].isalnum() and ch.isupper() else "#" + ch
    WebClient(token=config.env("SLACK_BOT_TOKEN")).chat_postMessage(
        channel=channel, text=text, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    )
