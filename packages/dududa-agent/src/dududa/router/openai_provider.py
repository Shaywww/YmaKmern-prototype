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
        base_urls: Optional[dict[str, str]] = None,
        api_keys: Optional[dict[str, str]] = None,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._base_urls = {k: v.rstrip("/") for k, v in (base_urls or {}).items()}
        self._api_keys = dict(api_keys or {})
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    def _base_for(self, model_id: str) -> str:
        """按模型选择 base（降级模型可指向不同网关）。

        OpenAI 兼容网关的 API 路径在 /v1 下；host 根路径通常是管理面板，
        自动补 /v1（如 https://gateway.example -> https://gateway.example/v1）。
        """
        base = self._base_urls.get(model_id, self._base_url)
        if base and base.count("/") == 2:
            return base + "/v1"
        return base

    def _key_for(self, model_id: str) -> str:
        """按模型选择密钥（降级模型可指向不同网关的密钥）。"""
        return self._api_keys.get(model_id, self._api_key)

    async def complete(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        config: ModelConfig,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {self._key_for(model_id)}",
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
                f"{self._base_for(model_id)}/chat/completions",
                json=payload,
                headers=headers,
            )
            if resp.status_code == 429:
                raise ModelError(
                    ModelErrorKind.RATE_LIMITED,
                    f"rate limited by {self._base_for(model_id)}",
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