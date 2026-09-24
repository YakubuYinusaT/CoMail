"""Model provider.

One place that knows which vendor is in use, so the rest of the code
asks for a structured response and does not care who answers.

Switching is an env var:

    LLM_PROVIDER=anthropic          # or openai
    LLM_MODEL=claude-sonnet-5       # optional, overrides the default

Both providers are reached through `instructor`, which enforces the
pydantic response model on either. That matters more than the vendor
choice: it means a malformed answer raises instead of arriving as a
plausible-looking dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, TypeVar

from pydantic import BaseModel

from app.config import settings

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    # OpenAI model names move quickly; override with LLM_MODEL when this
    # is not the one you want.
    "openai": "gpt-4o",
}


class ProviderNotConfigured(RuntimeError):
    """Raised instead of guessing. Says which key is missing and why."""


@dataclass
class Provider:
    name: str
    model: str


def resolve() -> Provider:
    cfg = settings()
    name = (cfg.llm_provider or "anthropic").strip().lower()
    if name not in DEFAULT_MODELS:
        raise ProviderNotConfigured(
            f"LLM_PROVIDER={name!r} is not one of: {', '.join(DEFAULT_MODELS)}"
        )

    key = cfg.anthropic_api_key if name == "anthropic" else cfg.openai_api_key
    if not key:
        want = "ANTHROPIC_API_KEY" if name == "anthropic" else "OPENAI_API_KEY"
        raise ProviderNotConfigured(
            f"{want} is not set, and LLM_PROVIDER is {name!r}. "
            f"Crawling, qualification, hook ranking and the sameness gate all "
            f"run without it; extraction and drafting do not."
        )
    return Provider(name=name, model=cfg.llm_model or DEFAULT_MODELS[name])


@lru_cache(maxsize=2)
def _client(provider_name: str):
    import instructor

    cfg = settings()
    if provider_name == "anthropic":
        from anthropic import Anthropic

        return instructor.from_anthropic(Anthropic(api_key=cfg.anthropic_api_key))

    from openai import OpenAI

    return instructor.from_openai(OpenAI(api_key=cfg.openai_api_key))


def complete(
    *,
    system: str,
    user: str,
    response_model: type[T],
    max_tokens: int = 2000,
    model: str | None = None,
    **kwargs: Any,
) -> T:
    """One structured completion, whichever provider is configured.

    The two SDKs disagree about where the system prompt goes: Anthropic
    takes it as its own argument, OpenAI as the first message. That is
    the whole difference, and it is handled here so nothing upstream has
    to branch on vendor.
    """
    provider = resolve()
    client = _client(provider.name)
    chosen = model or provider.model

    if provider.name == "anthropic":
        return client.messages.create(
            model=chosen,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            response_model=response_model,
            **kwargs,
        )

    return client.chat.completions.create(
        model=chosen,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_model=response_model,
        **kwargs,
    )
