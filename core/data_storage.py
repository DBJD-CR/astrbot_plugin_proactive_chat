"""会话持久化与数据清理模块。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiofiles
import aiofiles.os as aio_os

from astrbot.api import logger


class StorageMixin:
    """会话持久化相关的混入类。"""

    data_dir: Any
    session_data_file: Any
    data_lock: asyncio.Lock
    session_data: dict

    async def _load_data_internal(self) -> None:
        """
        从文件中加载会话数据（异步无锁内部实现）。
        """
        # 检查持久化文件是否存在
        if await aio_os.path.exists(self.session_data_file):
            try:
                # 异步读取 JSON 内容
                async with aiofiles.open(self.session_data_file, encoding="utf-8") as f:
                    content = await f.read()
                    # JSON 解析放到线程池，避免阻塞主事件循环
                    loaded_data = await asyncio.to_thread(json.loads, content)
                    if isinstance(loaded_data, dict):
                        self.session_data = loaded_data
                    else:
                        logger.warning(
                            "[主动消息] 会话数据文件结构无效喵，根节点应为对象，将使用空数据启动喵。"
                        )
                        self.session_data = {}
            except (OSError, json.JSONDecodeError) as e:
                logger.error(
                    f"[主动消息] 加载会话数据失败喵: {e}，将使用空数据启动喵。"
                )
                self.session_data = {}
        else:
            # 文件不存在则启动空数据
            self.session_data = {}

    async def _save_data_internal(self) -> None:
        """
        将会话数据保存到文件（异步无锁内部实现）。
        """
        try:
            # 确保目录存在
            await aio_os.makedirs(self.data_dir, exist_ok=True)
            # 写入 JSON（避免阻塞事件循环）
            async with aiofiles.open(
                self.session_data_file, "w", encoding="utf-8"
            ) as f:
                content_to_write = await asyncio.to_thread(
                    json.dumps, self.session_data, indent=4, ensure_ascii=False
                )
                await f.write(content_to_write)
        except OSError as e:
            logger.error(f"[主动消息] 保存会话数据失败喵: {e}")

    def _merge_session_info(self, base: dict, incoming: dict) -> dict:
        """合并两份会话数据，避免重复任务与计数错乱。"""
        # 先以 base 为主，再按字段规则吸收 incoming
        merged = base.copy()
        for key in [
            "self_id",
            "last_message_time",
            "unanswered_count",
            "next_trigger_time",
            "last_scheduled_at",
            "last_schedule_min_interval_seconds",
            "last_schedule_max_interval_seconds",
            "last_schedule_random_interval_seconds",
            "last_schedule_strategy",
            "last_schedule_reason",
            "last_schedule_rule",
            "last_schedule_source",
        ]:
            if key not in incoming:
                continue
            if key not in merged:
                merged[key] = incoming[key]
                continue

            if key == "unanswered_count":
                if isinstance(merged[key], (int, float)) and isinstance(
                    incoming[key], (int, float)
                ):
                    merged[key] = max(merged[key], incoming[key])
                continue

            if key in {"last_message_time", "next_trigger_time", "last_scheduled_at"}:
                if isinstance(merged[key], (int, float)) and isinstance(
                    incoming[key], (int, float)
                ):
                    merged[key] = max(merged[key], incoming[key])
                continue

            if key in {
                "last_schedule_min_interval_seconds",
                "last_schedule_max_interval_seconds",
                "last_schedule_random_interval_seconds",
                "last_schedule_strategy",
                "last_schedule_reason",
                "last_schedule_rule",
                "last_schedule_source",
            }:
                base_scheduled_at = merged.get("last_scheduled_at")
                incoming_scheduled_at = incoming.get("last_scheduled_at")
                if isinstance(base_scheduled_at, (int, float)) and isinstance(
                    incoming_scheduled_at, (int, float)
                ):
                    if incoming_scheduled_at >= base_scheduled_at:
                        merged[key] = incoming[key]
                elif key not in merged or not merged.get(key):
                    merged[key] = incoming[key]
                continue

            # 非数值（如 self_id）优先保留已有值；仅在已有值缺失时才回退到 incoming。
            if merged[key] is None:
                merged[key] = incoming[key]
        return merged

    def _normalize_session_data(self) -> bool:
        """规范化并合并 session_data 中的重复会话键。"""
        if not self.session_data:
            return False

        normalized_data: dict[str, dict] = {}
        changed = False

        # 遍历副本，允许在后续阶段安全替换原字典
        for session_id, payload in list(self.session_data.items()):
            normalized_id = self._normalize_session_id(session_id)
            if normalized_id != session_id:
                changed = True
                logger.info(
                    f"[主动消息] 规范化会话键: {self._get_session_log_str(session_id)} -> {normalized_id}"
                )

            # 命中同一规范化键时执行合并，避免重复会话条目
            existing = normalized_data.get(normalized_id)
            if existing:
                normalized_data[normalized_id] = self._merge_session_info(
                    existing, payload
                )
                changed = True
            else:
                normalized_data[normalized_id] = payload

        if changed:
            # 仅在检测到变化时回写，避免无意义对象替换
            self.session_data = normalized_data

        return changed

    def _cleanup_invalid_session_data(self) -> int:
        """
        清理无效的会话数据（旧格式遗留或不可解析条目）。
        """
        cleaned_count = 0
        invalid_sessions: list[str] = []

        # 标记需要清理的旧格式 session_id
        for session_id in list(self.session_data.keys()):
            parsed = self._parse_session_id(session_id)
            if (
                session_id.startswith("friend_message:")
                or session_id.startswith("group_message:")
                or not parsed
            ):
                invalid_sessions.append(session_id)
                cleaned_count += 1

        # 执行删除并记录日志
        for session_id in invalid_sessions:
            del self.session_data[session_id]
            logger.info(
                f"[主动消息] 清理了无效的会话数据: {self._get_session_log_str(session_id)}"
            )

        return cleaned_count
