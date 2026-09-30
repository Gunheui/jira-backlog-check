"""모델 호출을 한 곳에 모은다. 백엔드는 `.env`의 LLM_BACKEND로 고른다.

- claude-cli: Claude Code 구독 로그인(`claude -p`). 실행한 컴퓨터의 계정을 쓴다
- api: Anthropic API 키. 서버에 올리거나 여러 사람이 쓸 때
- codex-cli: Codex 로그인(`codex exec`). 비용 수치는 나오지 않는다

어느 백엔드든 (JSON 결과, 사용량) 두 가지를 돌려준다.
"""

import json
import subprocess
import tempfile
import time
from pathlib import Path

from . import config

# API 백엔드에서 비용을 계산할 때 쓰는 백만 토큰당 가격(USD). claude-cli는 CLI가 비용을 알려 준다
API_PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}


class LLMError(RuntimeError):
    pass


def call(system: str, prompt: str, schema: dict) -> tuple[dict, dict]:
    started = time.monotonic()
    backend = {"claude-cli": _claude_cli, "api": _api, "codex-cli": _codex_cli}.get(config.LLM_BACKEND)
    if backend is None:
        raise LLMError(f"LLM_BACKEND 값이 이상함: {config.LLM_BACKEND}")
    data, usage = backend(system, prompt, schema)
    usage.update(backend=config.LLM_BACKEND, seconds=round(time.monotonic() - started, 1))
    return data, usage


def _claude_cli(system: str, prompt: str, schema: dict) -> tuple[dict, dict]:
    # --safe-mode: 이 레포의 CLAUDE.md, 플러그인, MCP를 읽지 않게 한다(프롬프트에 섞이지 않도록)
    # --tools "": 파일 읽기, 명령 실행 같은 도구를 모두 끈다. 입력은 프롬프트에 준 글뿐이다
    cmd = [
        "claude", "-p",
        "--safe-mode", "--tools", "", "--no-session-persistence",
        "--model", config.LLM_MODEL,
        "--system-prompt", system,
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False),
    ]
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd, timeout=600)
    if proc.returncode != 0:
        raise LLMError(f"claude -p 실패({proc.returncode}): {proc.stderr[-800:] or proc.stdout[-800:]}")
    out = json.loads(proc.stdout)
    if out.get("is_error") or "structured_output" not in out:
        raise LLMError(f"claude -p 결과에 구조화 출력이 없음: {str(out.get('result'))[:800]}")
    u = out.get("usage", {})
    return out["structured_output"], {
        "model": next(iter(out.get("modelUsage", {})), config.LLM_MODEL),
        "input_tokens": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
        "cost_usd": out.get("total_cost_usd"),
    }


def _api(system: str, prompt: str, schema: dict) -> tuple[dict, dict]:
    import anthropic

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=config.LLM_MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
        # 안전 분류기가 거절하면 서버가 권장 모델로 다시 돌린다
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},
    )
    if response.stop_reason == "refusal":
        raise LLMError(f"모델이 거절함: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise LLMError("출력이 max_tokens에서 잘림")
    text = next(b.text for b in response.content if b.type == "text")
    price_in, price_out = API_PRICES.get(response.model, (None, None))
    u = response.usage
    tokens_in = u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
    cost = None if price_in is None else round((tokens_in * price_in + u.output_tokens * price_out) / 1e6, 6)
    return json.loads(text), {
        "model": response.model,
        "input_tokens": tokens_in,
        "output_tokens": u.output_tokens,
        "cost_usd": cost,
    }


def _codex_cli(system: str, prompt: str, schema: dict) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory() as cwd:
        schema_path = Path(cwd) / "schema.json"
        out_path = Path(cwd) / "out.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        cmd = [
            "codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
            "--output-schema", str(schema_path), "-o", str(out_path),
        ]
        if config.LLM_MODEL:
            cmd += ["--model", config.LLM_MODEL]
        # codex exec에는 시스템 프롬프트 칸이 따로 없어 앞에 붙인다
        proc = subprocess.run(cmd + ["-"], input=f"{system}\n\n---\n\n{prompt}", capture_output=True, text=True, cwd=cwd, timeout=900)
        if proc.returncode != 0 or not out_path.exists():
            raise LLMError(f"codex exec 실패({proc.returncode}): {proc.stderr[-800:]}")
        data = json.loads(out_path.read_text(encoding="utf-8"))
    return data, {"model": config.LLM_MODEL or "codex 기본값", "input_tokens": None, "output_tokens": None, "cost_usd": None}
