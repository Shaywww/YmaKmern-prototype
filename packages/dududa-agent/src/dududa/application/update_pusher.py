# -*- coding: utf-8 -*-
"""更新公告推送（Update Pusher）。

需求：给加过机器人的 QQ 好友推送每次更新内容；每条公告只推一次，
部分失败下次重试（已成功的人不会重复收到）。

机制：
- 公告文件 JSON：{"version", "content", "created_at", "pushed_at", "pushed_ids"}
- 推送目标：OneBot get_friend_list 的全部好友，逐人 send_private_msg
- 触发：插件启动时检查待推送公告（适配器未就绪时带重试），
  或管理员命令 dududa_announce 立即推送
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger("dududa20.update_pusher")


def build_notice_text(notice: dict) -> str:
    """公告文案：版本号 + 内容 + 落款（嘟嘟哒 2.0）。"""
    ver = str(notice.get("version") or "").strip()
    head = f"【嘟嘟哒更新公告】{ver}" if ver else "【嘟嘟哒更新公告】"
    content = str(notice.get("content") or "").strip()
    return f"{head}\n{content}\n\n—— 嘟嘟哒 2.0"


class UpdateNoticeStore:
    """公告文件读写：pending = 文件存在、有内容、且 pushed_at 为空。"""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> Optional[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if not str(data.get("content") or "").strip():
            return None
        return data

    def pending(self) -> Optional[dict]:
        data = self.load()
        if data is None or data.get("pushed_at"):
            return None
        return data

    def write(self, notice: dict) -> None:
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(notice, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def record_pushed_ids(self, notice: dict, pushed_ids) -> None:
        """部分成功：只更新 pushed_ids，pushed_at 留空（下次只重试失败者）。"""
        notice["pushed_ids"] = sorted(set(str(x) for x in (pushed_ids or ())))
        self.write(notice)

    def save_pushed(self, notice: dict, pushed_ids,
                    pushed_at: Optional[str] = None) -> None:
        """全部成功：写入 pushed_ids 与推送时间，公告转为已推送。"""
        notice["pushed_ids"] = sorted(set(str(x) for x in (pushed_ids or ())))
        notice["pushed_at"] = pushed_at or time.strftime("%Y-%m-%d %H:%M:%S")
        self.write(notice)


class UpdatePusher:
    """拉取好友列表并逐人推送待推送公告。"""

    ADAPTER_IDS = ("aiocqhttp", "lagrange")

    def __init__(self, store: UpdateNoticeStore, platform_manager: Any):
        self._store = store
        self._pm = platform_manager

    def _adapter_bot(self):
        insts = []
        try:
            insts = list(self._pm.get_insts())
        except Exception:
            try:
                insts = list(getattr(self._pm, "platform_insts", ()) or ())
            except Exception:
                insts = []
        for inst in insts:
            try:
                if inst.meta().id in self.ADAPTER_IDS:
                    return inst.bot
            except Exception:
                continue
        return None

    async def push_pending(self) -> dict:
        """推送待推送公告。报告字段：
        ok / skipped / error / friends / pushed_new / pushed_total / failed。
        friends=None 表示好友列表尚未拿到（适配器不可用或拉取失败）。
        """
        notice = self._store.pending()
        if notice is None:
            return {"ok": True, "skipped": "no_pending", "friends": 0,
                    "pushed_new": 0, "pushed_total": 0, "failed": []}
        bot = self._adapter_bot()
        if bot is None:
            return {"ok": False, "skipped": "adapter_unavailable",
                    "friends": None, "pushed_new": 0, "pushed_total": 0,
                    "failed": []}
        try:
            data = await bot.call_action("get_friend_list")
        except Exception as e:
            return {"ok": False, "error": f"get_friend_list: {e}",
                    "friends": None, "pushed_new": 0, "pushed_total": 0,
                    "failed": []}
        friends = [str(f.get("user_id", "")).strip()
                   for f in (data or ()) if str(f.get("user_id", "")).strip()]
        done = set(str(x) for x in (notice.get("pushed_ids") or ()))
        targets = [uid for uid in friends if uid not in done]
        text = build_notice_text(notice)
        ok_ids: list[str] = []
        failed: list[tuple[str, str]] = []
        for uid in targets:
            try:
                await bot.call_action(
                    "send_private_msg", user_id=int(uid), message=text)
                ok_ids.append(uid)
            except Exception as e:
                failed.append((uid, str(e)[:100]))
        done |= set(ok_ids)
        if not failed:
            self._store.save_pushed(notice, done)
        elif ok_ids:
            self._store.record_pushed_ids(notice, done)
        return {
            "ok": not failed,
            "version": str(notice.get("version") or ""),
            "friends": len(friends),
            "pushed_new": len(ok_ids),
            "pushed_total": len(done),
            "failed": failed,
        }


def build_update_push(context, data_dir: Optional[str] = None):
    """插件装配：返回 (store, pusher)；DUDUDA_UPDATE_PUSH=0 时关闭。"""
    if os.environ.get("DUDUDA_UPDATE_PUSH", "1") != "1":
        return None, None
    path = os.environ.get(
        "DUDUDA_NOTICE_FILE",
        os.path.join(data_dir or ".", "data", "update_notice.json"))
    store = UpdateNoticeStore(path)
    return store, UpdatePusher(store, context.platform_manager)


async def startup_push_loop(pusher, attempts: int = 10, delay: float = 30.0) -> None:
    """启动后后台推送：适配器未就绪时每 delay 秒重试，最多 attempts 次。"""
    for attempt in range(1, attempts + 1):
        try:
            report = await pusher.push_pending()
        except Exception as e:
            logger.warning("Update push attempt %d failed: %s", attempt, e)
        else:
            if report.get("skipped") == "no_pending":
                return
            if report.get("friends") is not None:
                logger.info("Update notice pushed: %s", report)
                return
            logger.warning("Update push attempt %d: %s", attempt,
                           report.get("error") or report.get("skipped"))
        await asyncio.sleep(delay)
