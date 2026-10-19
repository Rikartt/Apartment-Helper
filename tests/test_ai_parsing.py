"""extraction.py / drafting.py with the Anthropic client mocked. No network."""
from types import SimpleNamespace

import anthropic
import pytest

from backend import extraction, drafting, llm


def fake_client(text=None, exc=None, stop_reason="end_turn", blocks=None, beta=False):
    def create(**kw):
        create.calls.append(kw)
        if exc:
            raise exc
        content = blocks if blocks is not None else [SimpleNamespace(type="text", text=text)]
        return SimpleNamespace(content=content, stop_reason=stop_reason)
    create.calls = []
    msgs = SimpleNamespace(create=create)
    return SimpleNamespace(messages=msgs, beta=SimpleNamespace(messages=msgs)), create


@pytest.fixture
def patch_extract(monkeypatch):
    def _p(**kw):
        c, create = fake_client(**kw)
        monkeypatch.setattr(llm, "get_client", lambda: c)
        return create
    return _p


@pytest.fixture
def patch_draft(monkeypatch):
    def _p(**kw):
        c, create = fake_client(**kw)
        monkeypatch.setattr(llm, "get_client", lambda: c)
        return create
    return _p


# ---- extraction ----
def test_parse_json_variants():
    f = llm.parse_json
    assert f('{"a":1}') == {"a": 1}
    assert f('```json\n{"a":1}\n```') == {"a": 1}
    assert f('```\n{"a":1}\n```') == {"a": 1}
    assert f('  ```json\n{"a":1}\n```  ') == {"a": 1}
    assert f('Here you go: {"a": 1} hope it helps') == {"a": 1}
    with pytest.raises(ValueError):
        f("no json")


def test_extract_plain_json(patch_extract):
    patch_extract(text='{"language":"de","red_flags":[]}')
    r = extraction.extract("some ad")
    assert r["language"] == "de" and r["red_flags"] == [] and r["rent_chf"] is None


def test_extract_fenced_json(patch_extract):
    patch_extract(text='```json\n{"language":"en"}\n```')
    assert extraction.extract("ad")["language"] == "en"


def test_extract_invalid_json_returns_none(patch_extract):
    patch_extract(text="Sorry, here is prose")
    assert extraction.extract("ad") is None


def test_extract_api_error_raises_llmerror(patch_extract):
    patch_extract(exc=RuntimeError("boom"))
    with pytest.raises(llm.LLMError):
        extraction.extract("ad")


@pytest.mark.parametrize("t", ["", "   ", None])
def test_extract_empty_input_no_call(patch_extract, t):
    create = patch_extract(text="{}")
    assert extraction.extract(t) is None
    assert create.calls == []


def test_extract_truncates_long_ads(patch_extract):
    create = patch_extract(text="{}")
    extraction.extract("x" * 10000)
    assert "x" * 4001 not in create.calls[0]["messages"][0]["content"]


def test_extract_missing_api_key_raises_clean_error(monkeypatch):
    with pytest.raises(llm.LLMError) as ei:
        extraction.extract("ad")
    assert ei.value.status == 503


def test_extract_non_dict_json_rejected(patch_extract):
    patch_extract(text='["a","b"]')
    assert extraction.extract("ad") is None


# ---- drafting ----
def test_draft_happy_path(patch_draft):
    create = patch_draft(text="  Hallo zusammen!  ")
    out = drafting.draft({"title": "t", "extracted": {"language": "de"}, "raw_text": "r"}, {"age": 18})
    assert out == "Hallo zusammen!"
    assert "German" in create.calls[0]["system"]


def test_draft_unknown_language_fallback(patch_draft):
    create = patch_draft(text="hi")
    drafting.draft({"title": "t"}, {})
    assert "same language the ad" in create.calls[0]["system"]


def test_draft_joins_text_blocks_ignores_others(patch_draft):
    blocks = [SimpleNamespace(type="thinking", text="secret"), SimpleNamespace(type="text", text="A"),
              SimpleNamespace(type="text", text="B")]
    patch_draft(blocks=blocks)
    assert drafting.draft({}, {}) == "AB"


def test_draft_refusal(patch_draft):
    patch_draft(text="", stop_reason="refusal")
    with pytest.raises(drafting.DraftError):
        drafting.draft({}, {})


def test_draft_empty(patch_draft):
    patch_draft(text="   ")
    with pytest.raises(drafting.DraftError):
        drafting.draft({}, {})


def test_draft_api_error_wrapped(patch_draft):
    req = SimpleNamespace(method="POST", url="http://x")
    patch_draft(exc=anthropic.APIError("bad", request=req, body=None))
    with pytest.raises(drafting.DraftError):
        drafting.draft({}, {})
