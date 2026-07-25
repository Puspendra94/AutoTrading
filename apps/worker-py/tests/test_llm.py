"""Unit tests for the ported LLM layer (Phase 3b-1) — deterministic parts only.

Covers chain parsing, pricing normalization, text extraction, and the synthetic fallback
shape/validity. Live model calls are not exercised here (that needs credentials + network);
the fallback path is verified by pointing the chain at a provider with no credentials.
"""
import os

import pytest

from worker.llm import chain_builder as cb
from worker.llm.schemas import StrategyParams
from worker.llm.service import LlmService, price_for


def test_parse_chain_default(monkeypatch):
    monkeypatch.delenv("LLM_MODELS", raising=False)
    chain = cb.parse_chain()
    assert chain == [cb.ModelSpec("direct_api", "claude-opus-4-8")]


def test_parse_chain_multi_and_colon_in_model_id(monkeypatch):
    # Bedrock model ids contain colons — split on the FIRST ':' only.
    monkeypatch.setenv(
        "LLM_MODELS",
        "bedrock:anthropic.claude-sonnet-4-5-20250929-v1:0, deepseek:deepseek-chat, direct_api:claude-opus-4-8",
    )
    chain = cb.parse_chain()
    assert chain[0] == cb.ModelSpec("bedrock", "anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert chain[1] == cb.ModelSpec("deepseek", "deepseek-chat")
    assert chain[2] == cb.ModelSpec("direct_api", "claude-opus-4-8")


def test_parse_chain_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv("LLM_MODELS", "openai:gpt-4o")
    with pytest.raises(ValueError):
        cb.parse_chain()


def test_parse_chain_rejects_missing_colon(monkeypatch):
    monkeypatch.setenv("LLM_MODELS", "direct_api-claude")
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
