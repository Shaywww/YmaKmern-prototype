# -*- coding: utf-8 -*-
"""Translate MCP service —— deepseek LLM 翻译（主），有道词典兜底（单词级）。"""
from __future__ import annotations

import os

import httpx

from .base import BaseMCPService, CachePolicy, MCPServiceConfig, ServiceResult

_DS_URL = "https://api.deepseek.com/v1/chat/completions"
_YOUDAO_URL = "https://dict.youdao.com/suggest"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"


class TranslateService(BaseMCPService):
    """中英互译：deepseek-chat 高质量翻译；无 key 或失败时降级有道词典。"""

    def __init__(self):
        super().__init__(MCPServiceConfig(
            service_name="translate",
            description="Translate text between Chinese and English (LLM powered, dictionary fallback)",
            cache_policy=CachePolicy.NONE,
            timeout_seconds=20.0,
            max_retries=1,
            mock_mode=False,
        ))

    @staticmethod
    def _detect_target(text: str) -> str:
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return "en" if cjk > len(text) * 0.3 else "zh"

    async def _fetch_live(self, **kwargs) -> dict:
        text = str(kwargs.get("text") or kwargs.get("q") or "").strip()
        target = str(kwargs.get("target") or "").strip().lower()
        if not text:
            raise ValueError("empty text")
        target = target or self._detect_target(text)
        lang_name = {"zh": "简体中文", "en": "English"}.get(target, target)
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if key:
            try:
                async with httpx.AsyncClient(
                        timeout=self.config.timeout_seconds) as client:
                    resp = await client.post(
                        _DS_URL,
                        headers={"Authorization": f"Bearer {key}"},
                        json={
                            "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                            "messages": [
                                {"role": "system",
                                 "content": "你是专业翻译。只输出译文本身，不要任何解释、引号或多余内容。"},
                                {"role": "user",
                                 "content": f"请把下面的内容翻译成{lang_name}：\n{text}"},
                            ],
                            "temperature": 0.3,
                            "max_tokens": 1024,
                        },
                    )
                    resp.raise_for_status()
                    out = (resp.json()["choices"][0]["message"]["content"] or "").strip()
                    if out:
                        return {"translation": out, "target": target, "source": "llm"}
            except Exception:
                pass
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    _YOUDAO_URL,
                    params={"q": text[:60], "num": "3", "doctype": "json"},
                    headers={"User-Agent": _USER_AGENT},
                )
                entries = (resp.json().get("data") or {}).get("entries") or []
            if entries:
                return {
                    "translation": entries[0].get("explain", ""),
                    "target": target,
                    "source": "dictionary",
                }
        except Exception:
            pass
        raise ValueError("translate failed: no LLM key and dictionary miss")

    def _get_mock(self, **kwargs) -> dict:
        return {"translation": "mock translation", "target": "zh", "source": "mock"}

    async def search(self, text: str = "", q: str = "", target: str = ""):
        text = (text or q or "").strip()
        if not text:
            return ServiceResult.fail("empty text")
        return await self.query(text=text, target=target)