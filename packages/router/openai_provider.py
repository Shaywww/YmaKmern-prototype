"""OpenAI-compatible provider（DeepSeek / OpenAI / 通义千问 / Kimi 等）。

统一把 HTTP 失败归一化为 ModelError（稳定错误码）：
429 -> RATE_LIMITED（可重试）；timeout -> TIMEOUT（可重试）；
401/403 -> AUTH_FAILED（不可重试）；其他 -> PROVIDER_UNAVAILABLE（可重试）。
"""
from __future__ import annotations

import httpx
from typing import Optional

from .router import ModelProvider, ModelConfig, ModelError, ModelErrorKind


class OpenAIProvider(ModelProvider):
    """OpenAI API 兼容的模型提供者。

    支持 DeepSeek / OpenAI / 通义千问 / Kimi 等所有兼容 API。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        timeout: float = 30.0,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    async def complete(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        config: ModelConfig,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_id,
            "messages": messages,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
        }
        if config.structured_output:
            payload["response_format"] = config.structured_output
        try:
            resp = await self._client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            if resp.status_code == 429:
                raise ModelError(
                    ModelErrorKind.RATE_LIMITED,
                    f"rate limited by {self._base_url}",
                    retryable=True,
                    model_id=model_id,
                )
            if resp.status_code in (401, 403):
                raise ModelError(
                    ModelErrorKind.AUTH_FAILED,
                    "auth failed",
                    retryable=False,
                    model_id=model_id,
                )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except ModelError:
            raise
        except httpx.TimeoutException as e:
            raise ModelError(
                ModelErrorKind.TIMEOUT,
                f"timeout calling {model_id}",
                retryable=True,
                model_id=model_id,
            ) from e
        except Exception as e:
            raise ModelError(
                ModelErrorKind.PROVIDER_UNAVAILABLE,
                f"LLM call failed: {e}",
                retryable=True,
                model_id=model_id,
            ) from e

    def health(self, model_id: str) -> bool:
        return True

    async def close(self):
        await self._client.aclose()