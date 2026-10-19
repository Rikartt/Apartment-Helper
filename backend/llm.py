"""Shared Anthropic helpers: client, model ids, JSON parsing, clean error mapping."""
import json
import os
import re

import anthropic

# Current ids (claude-api skill, 2026-10): Haiku for bulk extraction, Sonnet for drafting.
EXTRACT_MODEL = os.environ.get("WGP_EXTRACT_MODEL", "claude-haiku-5-5")
DRAFT_MODEL = os.environ.get("WGP_DRAFT_MODEL", "claude-sonnet-5-5")
BEST_MODEL = os.environ.get("WGP_BEST_MODEL", EXTRACT_MODEL)

_client: anthropic.Anthropic | None = None


class LLMError(Exception):
    """A user-presentable failure talking to Claude. `status` is the HTTP status the
    API layer should return; `fatal` means retrying/continuing a batch is pointless."""

    def __init__(self, message: str, status: int = 502, fatal: bool = False):
        super().__init__(message)
        self.status = status
        self.fatal = fatal


def has_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def get_client() -> anthropic.Anthropic:
    global _client
    if not has_key():
        raise LLMError("ANTHROPIC_API_KEY is not set (add it to .env).", 503, True)
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"], timeout=60.0, max_retries=3)
    return _client


def translate_error(e: Exception) -> LLMError:
    """Map SDK exceptions onto a clean message (never leak a stack trace to the UI)."""
    if isinstance(e, LLMError):
        return e
    if isinstance(e, anthropic.AuthenticationError):
        return LLMError("Anthropic API key was rejected (invalid or revoked). "
                        "Check ANTHROPIC_API_KEY in .env.", 401, True)
    if isinstance(e, anthropic.PermissionDeniedError):
        return LLMError("Anthropic API key lacks permission for this request.", 403, True)
    if isinstance(e, anthropic.RateLimitError):
        return LLMError("Anthropic rate limit hit; try again in a minute.", 429, False)
    if isinstance(e, anthropic.BadRequestError):
        msg = str(getattr(e, "message", e))
        if re.search(r"credit|billing|balance|quota", msg, re.I):
            return LLMError("Anthropic credit balance is exhausted. Top up billing "
                            "in the Anthropic console, then retry.", 402, True)
        return LLMError(f"Anthropic rejected the request: {msg[:200]}", 400, False)
    if isinstance(e, anthropic.APITimeoutError):
        return LLMError("Anthropic request timed out; try again.", 504, False)
    if isinstance(e, anthropic.APIConnectionError):
        return LLMError("Could not reach the Anthropic API (network).", 503, False)
    if isinstance(e, anthropic.APIStatusError):
        msg = str(getattr(e, "message", e))
        if e.status_code == 402 or re.search(r"credit|billing|balance", msg, re.I):
            return LLMError("Anthropic credit balance is exhausted. Top up billing "
                            "in the Anthropic console, then retry.", 402, True)
        return LLMError(f"Anthropic API error {e.status_code}.", 502, False)
    return LLMError(f"Unexpected error calling Claude: {type(e).__name__}", 500, False)


_FENCE = re.compile(r"^```[a-zA-Z]*\s*(.*?)\s*```$", re.DOTALL)


def parse_json(text: str):
    """Tolerant JSON parse: strips fences, then falls back to the first balanced
    {...} or [...] in the text. Raises ValueError if nothing parses."""
    text = (text or "").strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return obj
            except ValueError:
                continue
    raise ValueError("no JSON found in model output")


def message_text(msg) -> str:
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
