"""LLM client — async interface to Ollama (and OpenAI-compatible APIs)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger()


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    eval_tokens: int = 0
    total_duration_ms: float = 0.0


class LLMClient:
    """Async LLM client for Ollama's /api/chat endpoint."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "gemma3:4b",
        temperature: float = 0.7,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Send a chat completion request. Returns the assistant's response.

        Args:
            messages: List of {"role": "system"|"user"|"assistant", "content": "..."}
            model: Override default model for this request.
            temperature: Override default temperature for this request.
        """
        client = await self._ensure_client()

        body = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature if temperature is not None else self.temperature,
            },
        }

        url = f"{self.base_url}/api/chat"
        try:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            logger.warning("llm_timeout", model=body["model"], timeout=self.timeout)
            return LLMResponse(text="", model=body["model"])
        except httpx.HTTPError as e:
            logger.error("llm_error", error=str(e))
            return LLMResponse(text="", model=body["model"])

        message = data.get("message", {})
        text = message.get("content", "").strip()
        total_ns = data.get("total_duration", 0)

        result = LLMResponse(
            text=text,
            model=data.get("model", body["model"]),
            prompt_tokens=data.get("prompt_eval_count", 0),
            eval_tokens=data.get("eval_count", 0),
            total_duration_ms=total_ns / 1_000_000,
        )

        logger.debug(
            "llm_response",
            model=result.model,
            tokens=result.eval_tokens,
            duration_ms=f"{result.total_duration_ms:.0f}",
            text=text[:80],
        )
        return result

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
