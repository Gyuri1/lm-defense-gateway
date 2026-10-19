#!/usr/bin/env python3
"""
llm-defense-gateway
===================
OpenAI-compatible LLM gateway for /v1/chat/completions with optional
Cisco AI Defense inspection of every LLM response.

Flow: client -> llm-defense-gateway -> upstream LLM -> Cisco AI Defense -> client

All settings are read from environment variables (see .env.example).
"""

import json
import logging
import os
import re
import secrets
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Optional: load .env located next to this script (does not override real env vars)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

APP_NAME = "llm-defense-gateway"
__version__ = "1.0.0"


def env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ---------------- Configuration ----------------
LISTEN_HOST        = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT        = int(os.getenv("LISTEN_PORT", "8100"))
PROXY_API_KEY      = os.getenv("PROXY_API_KEY", "")
UPSTREAM_BASE_URL  = os.getenv("UPSTREAM_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/")
UPSTREAM_API_KEY   = os.getenv("UPSTREAM_API_KEY", "")
UPSTREAM_TIMEOUT   = float(os.getenv("UPSTREAM_TIMEOUT", "300"))
DEFAULT_MODEL      = os.getenv("DEFAULT_MODEL", "")
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "0"))
RESPONSE_PATH      = os.getenv("RESPONSE_PATH", "choices[0].message.content")
LOG_LEVEL          = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_CONTENT        = env_bool("LOG_CONTENT", True)

AI_DEFENSE_ENABLED   = env_bool("AI_DEFENSE_ENABLED", True)
AI_DEFENSE_URL       = os.getenv("AI_DEFENSE_URL", "")
AI_DEFENSE_API_KEY   = os.getenv("AI_DEFENSE_API_KEY", "")
AI_DEFENSE_PROXY     = os.getenv("AI_DEFENSE_PROXY", "")
AI_DEFENSE_TIMEOUT   = float(os.getenv("AI_DEFENSE_TIMEOUT", "30"))
AI_DEFENSE_FAIL_OPEN = env_bool("AI_DEFENSE_FAIL_OPEN", False)
BLOCK_MODE           = os.getenv("BLOCK_MODE", "message").strip().lower()

# Headers accepted for client authentication ("Bearer <token>" or raw token)
AUTH_HEADERS = ("authorization", "authentication", "x-api-key", "api-key")

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(APP_NAME)


def validate_config() -> None:
    errors = []
    if not PROXY_API_KEY:
        errors.append("PROXY_API_KEY is not set")
    if not UPSTREAM_BASE_URL.startswith(("http://", "https://")):
        errors.append("UPSTREAM_BASE_URL must start with http:// or https://")
    if AI_DEFENSE_ENABLED and not AI_DEFENSE_URL:
        errors.append("AI_DEFENSE_URL is not set (or set AI_DEFENSE_ENABLED=false)")
    if AI_DEFENSE_ENABLED and not AI_DEFENSE_API_KEY:
        errors.append("AI_DEFENSE_API_KEY is not set (or set AI_DEFENSE_ENABLED=false)")
    if BLOCK_MODE not in ("message", "error"):
        errors.append("BLOCK_MODE must be 'message' or 'error'")
    for e in errors:
        log.error("Configuration error: %s", e)
    if errors:
        sys.exit(1)


# ---------------- Helpers ----------------
def preview(text: str) -> str:
    """Short, log-safe representation of prompt/answer text."""
    return repr(text[:80]) if LOG_CONTENT else f"<{len(text)} chars>"


def openai_error(status: int, message: str, err_type: str = "invalid_request_error",
                 code=None, extra: dict | None = None) -> JSONResponse:
    """Return an error in OpenAI API format."""
    content = {"error": {"message": message, "type": err_type, "param": None,
                         "code": code if code is not None else status}}
    if extra:
        content.update(extra)
    return JSONResponse(status_code=status, content=content)


def check_auth(request: Request):
    """Validate the client token. Returns an error response or None."""
    expected = PROXY_API_KEY.encode()
    for header in AUTH_HEADERS:
        value = request.headers.get(header, "").strip()
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        if value and secrets.compare_digest(value.encode(), expected):
            return None
    client_ip = request.client.host if request.client else "?"
    log.warning("Auth failed from %s | path=%s | header names=%s",
                client_ip, request.url.path, list(request.headers.keys()))
    return openai_error(401, "Invalid API key", "authentication_error")


def get_json_path(data, path: str):
    """Resolve a simple JSON path like 'choices[0].message.content'."""
    try:
        for part in path.split("."):
            m = re.fullmatch(r"(\w+)((?:$$\d+$$)*)", part)
            data = data[m.group(1)]
            for idx in re.findall(r"$$(\d+)$$", m.group(2)):
                data = data[int(idx)]
        return data
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def content_to_text(content) -> str:
    """Normalize OpenAI message content (string or multi-part list) to plain text."""
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type", "text") == "text")
    return "" if content is None else str(content)


def extract_prompt(body: dict) -> str:
    """Return the last user message (for logging)."""
    for msg in reversed(body.get("messages", [])):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return content_to_text(msg.get("content"))
    return ""


def upstream_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if UPSTREAM_API_KEY:
        headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"
    return headers


# ---------------- Cisco AI Defense ----------------
async def inspect_with_ai_defense(messages: list, answer: str) -> dict:
    """Send conversation + LLM answer to AI Defense. Raises on any failure."""
    convo = [{"role": m.get("role"), "content": content_to_text(m.get("content"))}
             for m in messages
             if isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")]
    convo.append({"role": "assistant", "content": answer})
    payload = {"messages": [m for m in convo if m["content"].strip()]}

    r = await app.state.aid_client.post(
        AI_DEFENSE_URL, json=payload,
        headers={"Content-Type": "application/json",
                 "X-Cisco-AI-Defense-API-Key": AI_DEFENSE_API_KEY})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def summarize_verdict(v: dict) -> dict:
    """Keep the relevant fields. A missing 'is_safe' is treated as NOT safe."""
    return {
        "is_safe": v.get("is_safe") is True,
        "classifications": v.get("classifications") or [],
        "rules": [r.get("rule_name") for r in (v.get("rules") or []) if isinstance(r, dict)],
        "action": v.get("action", ""),
        "severity": v.get("severity", ""),
        "event_id": v.get("event_id", ""),
        "explanation": v.get("explanation", ""),
    }


def build_blocked(upstream: dict, s: dict) -> tuple[int, dict]:
    """Build the reply returned when AI Defense reports NOT SAFE."""
    msg = ("Response blocked by Cisco AI Defense. "
           f"Classifications: {', '.join(s['classifications']) or 'UNSPECIFIED'}. "
           f"Rules: {', '.join(s['rules']) or 'n/a'}. "
           f"Event ID: {s['event_id'] or 'n/a'}")
    if BLOCK_MODE == "error":
        return 403, {"error": {"message": msg, "type": "content_policy_violation",
                               "param": None, "code": "ai_defense_block"},
                     "ai_defense": s}
    return 200, {
        "id": upstream.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": upstream.get("created", int(time.time())),
        "model": upstream.get("model", DEFAULT_MODEL),
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": msg},
                     "finish_reason": "content_filter"}],
        "usage": upstream.get("usage") or {"prompt_tokens": 0,
                                           "completion_tokens": 0,
                                           "total_tokens": 0},
        "ai_defense": s,
    }


def to_sse(data: dict):
    """Convert a full chat.completion into an SSE stream (for stream=true clients)."""
    base = {"id": data.get("id", f"chatcmpl-{uuid.uuid4().hex}"),
            "object": "chat.completion.chunk",
            "created": data.get("created", int(time.time())),
            "model": data.get("model", DEFAULT_MODEL)}
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    finish = choice.get("finish_reason", "stop")

    async def gen():
        chunks = [
            {**base, "choices": [{"index": 0, "finish_reason": None,
                                  "delta": {"role": "assistant", "content": content}}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
             "usage": data.get("usage"),
             **({"ai_defense": data["ai_defense"]} if "ai_defense" in data else {})},
        ]
        for c in chunks:
            yield f"data: {json.dumps(c)}\n\n".encode()
        yield b"data: [DONE]\n\n"
    return gen()


# ---------------- App ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Upstream LLM: always direct (ignores proxy environment variables)
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(UPSTREAM_TIMEOUT),
                                         trust_env=False)
    # AI Defense: direct or through AI_DEFENSE_PROXY
    app.state.aid_client = httpx.AsyncClient(timeout=httpx.Timeout(AI_DEFENSE_TIMEOUT),
                                             proxy=AI_DEFENSE_PROXY or None,
                                             trust_env=False)
    log.info("%s %s listening on %s:%s -> upstream %s",
             APP_NAME, __version__, LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE_URL)
    log.info("AI Defense: enabled=%s proxy=%s fail_open=%s block_mode=%s max_tokens_cap=%s",
             AI_DEFENSE_ENABLED, "set" if AI_DEFENSE_PROXY else "direct",
             AI_DEFENSE_FAIL_OPEN, BLOCK_MODE, DEFAULT_MAX_TOKENS or "none")
    yield
    await app.state.client.aclose()
    await app.state.aid_client.aclose()


app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "name": APP_NAME, "version": __version__}


@app.get("/v1/models")
async def list_models(request: Request):
    if (err := check_auth(request)):
        return err
    try:
        r = await app.state.client.get(f"{UPSTREAM_BASE_URL}/models",
                                       headers=upstream_headers())
        return JSONResponse(status_code=r.status_code, content=r.json())
    except Exception as e:
        return openai_error(502, f"Upstream error: {e}", "upstream_error")


async def stream_passthrough(body: dict, t0: float):
    """Raw streaming relay (used only when AI Defense is disabled)."""
    client: httpx.AsyncClient = app.state.client
    try:
        req = client.build_request("POST", f"{UPSTREAM_BASE_URL}/chat/completions",
                                   json=body, headers=upstream_headers())
        upstream = await client.send(req, stream=True)
    except httpx.RequestError as e:
        return openai_error(502, f"Upstream unreachable: {e}", "upstream_error")

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            log.info("Stream finished in %.2fs", time.perf_counter() - t0)

    return StreamingResponse(
        relay(), status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "text/event-stream"))


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if (err := check_auth(request)):
        return err

    try:
        body = await request.json()
    except Exception:
        return openai_error(400, "Request body must be valid JSON")
    if not isinstance(body, dict) or not body.get("messages"):
        return openai_error(400, "'messages' is required")

    if DEFAULT_MODEL:
        body.setdefault("model", DEFAULT_MODEL)
    if DEFAULT_MAX_TOKENS and not body.get("max_tokens") and not body.get("max_completion_tokens"):
        body["max_tokens"] = DEFAULT_MAX_TOKENS

    client_ip = request.client.host if request.client else "?"
    log.info("Request from %s | model=%s | prompt=%s",
             client_ip, body.get("model", "-"), preview(extract_prompt(body)))
    t0 = time.perf_counter()

    wants_stream = bool(body.get("stream"))
    if wants_stream and not AI_DEFENSE_ENABLED:
        return await stream_passthrough(body, t0)
    if wants_stream:  # the full answer must be buffered to inspect it
        body = {k: v for k, v in body.items() if k not in ("stream", "stream_options")}

    # ---- 1) Upstream LLM ----
    try:
        r = await app.state.client.post(f"{UPSTREAM_BASE_URL}/chat/completions",
                                        json=body, headers=upstream_headers())
    except httpx.RequestError as e:
        log.error("Upstream unreachable: %s", e)
        return openai_error(502, f"Upstream unreachable: {e}", "upstream_error")
    try:
        data = r.json()
    except ValueError:
        return openai_error(502, f"Upstream returned non-JSON (HTTP {r.status_code})",
                            "upstream_error")
    if r.status_code != 200:
        log.warning("Upstream HTTP %s: %s", r.status_code, data)
        return JSONResponse(status_code=r.status_code, content=data)

    answer = content_to_text(get_json_path(data, RESPONSE_PATH))
    tokens = (data.get("usage") or {}).get("completion_tokens")
    log.info("LLM response in %.2fs | completion_tokens=%s | answer=%s",
             time.perf_counter() - t0, tokens, preview(answer))

    status = 200
    # ---- 2) AI Defense inspection ----
    if AI_DEFENSE_ENABLED:
        t1 = time.perf_counter()
        verdict = None
        try:
            verdict = await inspect_with_ai_defense(body["messages"], answer)
        except Exception as e:
            log.error("AI Defense inspection failed: %s", e)
            if not AI_DEFENSE_FAIL_OPEN:
                return openai_error(502, f"AI Defense inspection failed: {e}",
                                    "ai_defense_error")
            log.warning("FAIL_OPEN: returning LLM response without inspection")

        if verdict is not None:
            s = summarize_verdict(verdict)
            log.info("AI Defense in %.2fs | is_safe=%s | action=%s | "
                     "classifications=%s | rules=%s | event_id=%s",
                     time.perf_counter() - t1, s["is_safe"], s["action"],
                     s["classifications"], s["rules"], s["event_id"])
            if not s["is_safe"]:
                status, data = build_blocked(data, s)

    log.info("Total %.2fs | status=%s", time.perf_counter() - t0, status)

    # ---- 3) Reply to client ----
    if wants_stream and status == 200:
        return StreamingResponse(to_sse(data), media_type="text/event-stream")
    return JSONResponse(status_code=status, content=data)


if __name__ == "__main__":
    validate_config()
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level=LOG_LEVEL.lower())