"""LLM client — unified async interface via litellm.

Supports Ollama (local), OpenAI, Anthropic (Claude), and 100+ other
providers through a single ``chat()`` method.  Provider is selected
via config: ``provider`` + ``model`` + optional ``api_key``.

Provider string → litellm model ID mapping:
  ollama   → "ollama/<model>"        (e.g. "ollama/gemma3:4b")
  openai   → "<model>"               (e.g. "gpt-4o")
  anthropic→ "<model>"               (e.g. "claude-sonnet-4-20250514")
  custom   → "<model>" + base_url    (any OpenAI-compatible endpoint)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()

# Suppress litellm's noisy default logging
os.environ.setdefault("LITELLM_LOG", "ERROR")


@dataclass
class LLMResponse:
    text: str
    model: str
    thinking: str = ""  # extended thinking content (if model supports it)
    prompt_tokens: int = 0
    eval_tokens: int = 0
    total_duration_ms: float = 0.0
    raw: dict = field(default_factory=dict)  # full provider response for debugging


class LLMClient:
    """Async LLM client using litellm for multi-provider support.

    Config examples::

        # Ollama (default, local)
        LLMClient(provider="ollama", model="gemma3:4b")

        # OpenAI
        LLMClient(provider="openai", model="gpt-4o", api_key="sk-...")

        # Anthropic Claude
        LLMClient(provider="anthropic", model="claude-sonnet-4-20250514", api_key="sk-ant-...")

        # Custom OpenAI-compatible endpoint
        LLMClient(provider="custom", model="my-model",
                  base_url="http://my-server:8080/v1", api_key="token")
    """

    def __init__(
        self,
        provider: str = "ollama",
        model: str = "gemma3:4b",
        base_url: str = "http://localhost:11434",
        api_key: str = "",
        temperature: float = 0.7,
        timeout: float = 10.0,
    ) -> None:
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout

        # Set provider-specific env vars so litellm can find them
        if api_key:
            if provider == "openai":
                os.environ.setdefault("OPENAI_API_KEY", api_key)
            elif provider == "anthropic":
                os.environ.setdefault("ANTHROPIC_API_KEY", api_key)

    def _litellm_model(self, model: str | None = None) -> str:
        """Convert provider + model into a litellm model string."""
        m = model or self.model
        if self.provider == "ollama":
            return f"ollama/{m}" if not m.startswith("ollama/") else m
        if self.provider == "custom":
            return f"openai/{m}" if not m.startswith("openai/") else m
        # openai, anthropic, etc. — pass through as-is
        return m

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Send a chat completion request.

        Args:
            messages: List of ``{"role": "...", "content": "..."}``.
            model: Override default model for this request.
            temperature: Override default temperature for this request.

        Returns:
            LLMResponse with text, thinking (if available), and token counts.
        """
        import litellm

        litellm_model = self._litellm_model(model)
        temp = temperature if temperature is not None else self.temperature

        kwargs: dict = {
            "model": litellm_model,
            "messages": messages,
            "temperature": temp,
            "timeout": self.timeout,
        }

        # Provider-specific options
        if self.provider == "ollama":
            kwargs["api_base"] = self.base_url
        elif self.provider == "custom":
            kwargs["api_base"] = self.base_url
            if self.api_key:
                kwargs["api_key"] = self.api_key

        start = time.monotonic()

        try:
            response = await litellm.acompletion(**kwargs)
        except litellm.Timeout:
            logger.warning("llm_timeout", model=litellm_model, timeout=self.timeout)
            return LLMResponse(text="", model=litellm_model)
        except Exception as e:
            logger.error("llm_error", error=str(e), provider=self.provider)
            return LLMResponse(text="", model=litellm_model)

        elapsed_ms = (time.monotonic() - start) * 1000

        # Extract response
        choice = response.choices[0] if response.choices else None
        text = choice.message.content.strip() if choice and choice.message.content else ""

        # Extract thinking content (Anthropic extended thinking, etc.)
        thinking = ""
        if choice and hasattr(choice.message, "thinking"):
            thinking = choice.message.thinking or ""
        # Some providers put thinking in tool_calls or other fields
        if not thinking and choice and hasattr(choice.message, "reasoning_content"):
            thinking = choice.message.reasoning_content or ""

        # Token usage
        usage = response.usage if response.usage else None
        prompt_tokens = usage.prompt_tokens if usage else 0
        eval_tokens = usage.completion_tokens if usage else 0

        result = LLMResponse(
            text=text,
            model=response.model or litellm_model,
            thinking=thinking,
            prompt_tokens=prompt_tokens,
            eval_tokens=eval_tokens,
            total_duration_ms=elapsed_ms,
        )

        logger.debug(
            "llm_response",
            provider=self.provider,
            model=result.model,
            tokens=result.eval_tokens,
            duration_ms=f"{result.total_duration_ms:.0f}",
            has_thinking=bool(thinking),
            text=text[:80],
        )
        return result

    async def close(self) -> None:
        """Clean up resources (no-op for litellm, kept for API compat)."""
        pass
