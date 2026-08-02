"""Anthropic Messages API adapter."""

from __future__ import annotations

import json
import time

import httpx

from app.ai.providers.base import CompletionRequest, CompletionResponse
from app.config import get_settings
from app.core.errors import ProviderError

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicProvider:
    name = "anthropic"
    is_external = True

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None) -> None:
        settings = get_settings()
        self._api_key = api_key if api_key is not None else settings.anthropic_api_key
        self._timeout = settings.ai_request_timeout_s
        self._client = client

    def _http(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=self._timeout)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not self._api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not configured", permanent=True)

        system_parts = [m.content for m in request.messages if m.role == "system"]
        turns = [
            {"role": m.role, "content": m.content}
            for m in request.messages
            if m.role in {"user", "assistant"}
        ]
        if request.json_schema is not None:
            system_parts.append(
                "Respond with a single JSON object and nothing else. It must validate "
                f"against this JSON Schema:\n{json.dumps(request.json_schema, ensure_ascii=False)}"
            )

        body: dict = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "messages": turns or [{"role": "user", "content": ""}],
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if request.stop:
            body["stop_sequences"] = request.stop

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        # A retried request must not be billed or executed twice (§5.3).
        if request.idempotency_key:
            headers["Idempotency-Key"] = request.idempotency_key

        started = time.perf_counter()
        client = self._http()
        try:
            response = client.post(API_URL, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"anthropic timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic transport error: {exc}") from exc
        finally:
            if self._client is None:
                client.close()

        if response.status_code >= 400:
            # 4xx other than 429 will not succeed on retry.
            permanent = 400 <= response.status_code < 500 and response.status_code != 429
            raise ProviderError(
                f"anthropic returned {response.status_code}: {response.text[:400]}",
                permanent=permanent,
                details={"status": response.status_code},
            )

        data = response.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )
        usage = data.get("usage", {})
        return CompletionResponse(
            text=text,
            model=data.get("model", request.model),
            provider=self.name,
            prompt_tokens=int(usage.get("input_tokens", 0)),
            completion_tokens=int(usage.get("output_tokens", 0)),
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=data.get("stop_reason", "stop"),
        )
