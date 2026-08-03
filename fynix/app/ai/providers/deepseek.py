"""DeepSeek adapter (OpenAI-compatible chat completions endpoint)."""

from __future__ import annotations

import json
import time

import httpx

from app.ai.providers.base import CompletionRequest, CompletionResponse
from app.config import get_settings
from app.core.errors import ProviderError


class DeepSeekProvider:
    name = "deepseek"
    is_external = True

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key if api_key is not None else settings.deepseek_api_key
        self._base_url = (base_url or settings.deepseek_base_url).rstrip("/")
        self._timeout = settings.ai_request_timeout_s
        self._client = client

    def _http(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=self._timeout)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        if not self._api_key:
            raise ProviderError("DEEPSEEK_API_KEY is not configured", permanent=True)

        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        body: dict = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.stop:
            body["stop"] = request.stop
        if request.json_schema is not None:
            body["response_format"] = {"type": "json_object"}
            messages.append(
                {
                    "role": "system",
                    "content": "Return one JSON object matching this schema: "
                    + json.dumps(request.json_schema, ensure_ascii=False),
                }
            )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        started = time.perf_counter()
        client = self._http()
        try:
            response = client.post(
                f"{self._base_url}/chat/completions", json=body, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"deepseek timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"deepseek transport error: {exc}") from exc
        finally:
            if self._client is None:
                client.close()

        if response.status_code >= 400:
            permanent = 400 <= response.status_code < 500 and response.status_code != 429
            raise ProviderError(
                f"deepseek returned {response.status_code}: {response.text[:400]}",
                permanent=permanent,
                details={"status": response.status_code},
            )

        data = response.json()
        choices = data.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content", "")
        usage = data.get("usage", {})
        return CompletionResponse(
            text=text,
            model=data.get("model", request.model),
            provider=self.name,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=choices[0].get("finish_reason", "stop"),
        )
