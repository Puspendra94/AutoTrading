"""Unit tests for the ported LLM layer (Phase 3b-1) — deterministic parts only.

Covers chain parsing, pricing normalization, text extraction, and the synthetic fallback
shape/validity. Live model calls are not exercised here (that needs credentials + network);
the fallback path is verified by pointing the chain at a provider with no credentials.
"""
import contextlib
import importlib
import os

import pytest

from worker import config as cfg
from worker.llm import chain_builder as cb
from worker.llm.schemas import StrategyParams
from worker.llm.service import LlmService, price_for



@contextlib.contextmanager
def chain_env(**env):
    """Set LLM env vars so `parse_chain` actually sees them.

    `worker.config.config` is a frozen dataclass built from os.getenv at IMPORT time — deliberate,
    since the process reads its environment once at startup. That means monkeypatch.setenv alone
    is invisible to parse_chain: it reads config, not os.getenv, so the test would silently assert
    against whatever env existed when the module was first imported. Both modules have to be
    reloaded, and reloaded again on the way out so the patched values cannot leak into later tests.
    """
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: v for k, v in env.items() if v is not None})
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
    try:
        importlib.reload(cfg)
        importlib.reload(cb)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(cfg)
        importlib.reload(cb)


def test_parse_chain_default():
    with chain_env(LLM_MODELS=None):
        assert cb.parse_chain() == [cb.ModelSpec("direct_api", "claude-opus-4-8")]


def test_parse_chain_multi_and_colon_in_model_id():
    # Bedrock model ids contain colons — split on the FIRST ':' only.
    with chain_env(
        LLM_MODELS="bedrock:anthropic.claude-sonnet-4-5-20250929-v1:0, deepseek:deepseek-chat, direct_api:claude-opus-4-8",
    ):
        chain = cb.parse_chain()
        assert chain[0] == cb.ModelSpec("bedrock", "anthropic.claude-sonnet-4-5-20250929-v1:0")
        assert chain[1] == cb.ModelSpec("deepseek", "deepseek-chat")
        assert chain[2] == cb.ModelSpec("direct_api", "claude-opus-4-8")


def test_parse_chain_rejects_unknown_provider():
    with chain_env(LLM_MODELS="openai:gpt-4o"):
        with pytest.raises(ValueError):
            cb.parse_chain()


def test_parse_chain_rejects_missing_colon():
    with chain_env(LLM_MODELS="direct_api-claude"):
        with pytest.raises(ValueError):
            cb.parse_chain()


def test_price_for_normalizes_dated_and_bedrock_ids():
    assert price_for("claude-opus-4-8") == {"input": 5.0, "output": 25.0}
    # Bedrock-style prefix + dated suffix both normalize back to the base id.
    assert price_for("anthropic.claude-sonnet-5-20250101-v1:0")["input"] == 3.0
    # Unknown model -> $0 (never a fabricated estimate).
    assert price_for("deepseek-chat") == {"input": 0.0, "output": 0.0}


def test_extract_text_handles_str_and_blocks():
    assert cb.extract_text("hello") == "hello"
    assert cb.extract_text([{"text": "a"}, {"text": "b"}]) == "ab"
    assert cb.extract_text([]) == ""


def test_synthetic_structured_fallback_matches_schema(monkeypatch):
    monkeypatch.setenv("LLM_MODELS", "direct_api:claude-opus-4-8")
    svc = LlmService()
    out = svc._synthetic_structured_fallback(StrategyParams, "claude-opus-4-8")
    assert isinstance(out["data"], StrategyParams)
    assert out["data"].indicatorConfig.emaFastPeriod == 12
    assert out["model"].endswith("-fallback")
    assert out["inputTokens"] == 210 and out["outputTokens"] == 140


@pytest.mark.asyncio
async def test_structured_completion_falls_back_when_no_credentials(monkeypatch):
    # A direct_api chain with no ANTHROPIC_API_KEY -> build_model raises for every entry ->
    # the service returns the deterministic synthetic fallback, validated against the schema.
    monkeypatch.setenv("LLM_MODELS", "direct_api:claude-opus-4-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    svc = LlmService()
    out = await svc.generate_structured_completion("prompt", StrategyParams)
    assert isinstance(out["data"], StrategyParams)
    assert out["model"] == "claude-opus-4-8-fallback"


# --------------------------------------------------------------------------- groq provider
def test_parse_chain_accepts_groq():
    with chain_env(LLM_MODELS="groq:llama-3.3-70b-versatile"):
        [spec] = cb.parse_chain()
        assert spec.provider == "groq" and spec.model_id == "llama-3.3-70b-versatile"


def test_groq_is_a_known_provider():
    from worker.llm.chain_builder import KNOWN_PROVIDERS
    assert "groq" in KNOWN_PROVIDERS


def test_groq_without_a_key_fails_loudly_rather_than_silently():
    """A missing key must raise so the chain falls through to the next model, not hang or
    return a half-built client that fails later inside the call."""
    with chain_env(GROQ_API_KEY=""):
        with pytest.raises(ValueError, match="GROQ_API_KEY"):
            cb.build_model(cb.ModelSpec(provider="groq", model_id="x"), 512)


def test_groq_budget_is_clamped_to_its_free_tier_ceiling():
    """ENTRY_MAX_TOKENS is 12288 to fit DeepSeek's hidden reasoning. Groq's free tier rejects any
    single request over 8k tokens with a 413 before the model sees it, so the budget that keeps
    DeepSeek working made every Groq call fail."""
    from worker.llm.chain_builder import PROVIDER_MAX_TOKENS
    assert PROVIDER_MAX_TOKENS["groq"] < 8000

    captured = {}

    class FakeChatGroq:
        def __init__(self, **kw):
            captured.update(kw)

    with chain_env(GROQ_API_KEY="k" * 40):
        import sys, types
        mod = types.ModuleType("langchain_groq")
        mod.ChatGroq = FakeChatGroq
        sys.modules["langchain_groq"] = mod
        try:
            cb.build_model(cb.ModelSpec(provider="groq", model_id="openai/gpt-oss-120b"), 12288)
        finally:
            sys.modules.pop("langchain_groq", None)
    assert captured["max_tokens"] == PROVIDER_MAX_TOKENS["groq"]


def test_a_provider_without_a_ceiling_gets_the_budget_it_asked_for():
    """The clamp must not quietly shrink DeepSeek, which genuinely needs the full 12288."""
    from worker.llm.chain_builder import PROVIDER_MAX_TOKENS
    assert "deepseek" not in PROVIDER_MAX_TOKENS
