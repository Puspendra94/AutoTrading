"""Provider-agnostic LangChain model chain — faithful port of llm-chain.builder.ts.

Configured entirely via LLM_MODELS (comma-separated "provider:modelId" pairs, tried in
order as a fallback chain), so adding a model within a supported provider needs no code
change. Adding a new *provider* is one branch in build_model; every caller stays untouched.

The heavy chat-model packages (langchain_anthropic / langchain_aws / langchain_deepseek)
are imported lazily inside build_model so the pure helpers (parse_chain, extract_text) and
the schema/pricing logic can be imported and unit-tested without them installed.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from ..config import config

log = logging.getLogger("worker.llm.chain")

KNOWN_PROVIDERS = ("direct_api", "bedrock", "deepseek", "groq")
ProviderKind = Literal["direct_api", "bedrock", "deepseek", "groq"]


@dataclass(frozen=True)
class ModelSpec:
    provider: ProviderKind
    model_id: str


def parse_chain() -> list[ModelSpec]:
    """Parse LLM_MODELS into an ordered fallback chain. Mirrors LlmChainBuilder.parseChain:
    default 'direct_api:claude-opus-4-8', validate provider, split on the FIRST ':' only
    (Bedrock model ids contain colons)."""
    raw = config.llm_models
    specs: list[ModelSpec] = []
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        idx = entry.find(":")
        if idx == -1:
            raise ValueError(f"Invalid LLM_MODELS entry '{entry}' — expected 'provider:modelId'.")
        provider = entry[:idx].strip()
        model_id = entry[idx + 1:].strip()
        if provider not in KNOWN_PROVIDERS:
            raise ValueError(
                f"Unknown provider '{provider}' in LLM_MODELS — must be one of: {', '.join(KNOWN_PROVIDERS)}."
            )
        specs.append(ModelSpec(provider=provider, model_id=model_id))  # type: ignore[arg-type]
    if not specs:
        raise ValueError("LLM_MODELS resolved to an empty chain.")
    return specs


def build_model(spec: ModelSpec, max_tokens: int) -> Any:
    """Instantiate the LangChain chat model for a spec, using the same env vars and
    credential precedence as the backend's buildModel."""
    if spec.provider == "direct_api":
        api_key = re.sub(r"^[\"']|[\"']$", "", config.anthropic_api_key).strip()
        if not api_key or len(api_key) < 10:
            raise ValueError("ANTHROPIC_API_KEY not configured for direct_api provider.")
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(api_key=api_key, model=spec.model_id, max_tokens=max_tokens)

    if spec.provider == "deepseek":
        api_key = config.deepseek_api_key.strip()
        if not api_key or len(api_key) < 10:
            raise ValueError("DEEPSEEK_API_KEY not configured for deepseek provider.")
        from langchain_deepseek import ChatDeepSeek

        return ChatDeepSeek(api_key=api_key, model=spec.model_id, max_tokens=max_tokens)

    if spec.provider == "groq":
        api_key = config.groq_api_key.strip()
        if not api_key or len(api_key) < 10:
            raise ValueError("GROQ_API_KEY not configured for groq provider.")
        from langchain_groq import ChatGroq

        return ChatGroq(api_key=api_key, model=spec.model_id, max_tokens=max_tokens)

    if spec.provider == "bedrock":
        region = config.aws_region
        if not region:
            raise ValueError("AWS_REGION not configured for bedrock provider.")
        from langchain_aws import ChatBedrockConverse

        bearer_token = config.aws_bedrock_api_key.strip()
        access_key = config.aws_access_key_id
        secret_key = config.aws_secret_access_key
        session_token = config.aws_session_token

        kwargs: dict[str, Any] = {"model": spec.model_id, "region_name": region, "max_tokens": max_tokens}
        if bearer_token:
            # langchain-aws reads a Bedrock bearer token from this env var (the Python SDK
            # has no constructor arg equivalent to JS's bedrockBearerToken).
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = bearer_token
        elif access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
            if session_token:
                kwargs["aws_session_token"] = session_token
        return ChatBedrockConverse(**kwargs)

    raise ValueError(f"Unhandled provider '{spec.provider}'.")


def extract_text(content: Any) -> str:
    """Plain text from LangChain's message content union (str, or a list of content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text", "") or "")
            else:
                parts.append(getattr(block, "text", "") or "")
        return "".join(parts)
    return ""
