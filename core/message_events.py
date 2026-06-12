"""消息事件监听模块。"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class EventsMixin:
    """事件监听相关混入类。"""

    data_lock: Any
    session_data: dict
    last_message_times: dict[str, float]
    session_temp_state: dict[str, dict]
    first_message_logged: set[str]
    scheduler: Any

    def _extract_event_text(self, event: AstrMessageEvent) -> str:
        """提取一条事件中的纯文本，供语境调度临时判断使用。"""
        candidates: list[Any] = []
        for attr in ("message_str", "raw_message"):
            value = getattr(event, attr, None)
            if value:
                candidates.append(value)

        message_obj = getattr(event, "message_obj", None)
        if message_obj:
            for attr in ("message_str", "raw_message"):
                value = getattr(message_obj, attr, None)
                if value:
                    candidates.append(value)

        try:
            messages = event.get_messages()
        except Exception:
            messages = []
        if messages:
            extractor = getattr(self, "_extract_platform_message_text", None)
            if callable(extractor):
                for item in messages:
                    try:
                        extracted = extractor(item)
                        if extracted:
                            candidates.append(extracted)
                    except Exception:
                        pass
            parts: list[str] = []
            for item in messages:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
                elif isinstance(item, str):
                    parts.append(item)
            if parts:
                candidates.append("".join(parts))

        for item in candidates:
            text = " ".join(str(item or "").split())
            if text:
                return text[:500]
        return ""

    async def on_friend_message(self, event: AstrMessageEvent):
        """监听私聊消息，取消旧任务，并重置计时器和计数器。"""
        # 没有消息内容则无需处理
        if not event.get_messages():
            return

        session_id = event.unified_msg_origin
        # 统一会话键，避免跨平台前缀变化导致状态分裂
        normalized_session_id = self._normalize_session_id(session_id)

        # 缓存 self_id，便于装饰钩子构造事件
        if event.get_self_id():
            async with self.data_lock:
                self.session_data.setdefault(session_id, {})["self_id"] = (
                    event.get_self_id()
                )

        # 更新消息时间（仅插件启动后用于自动触发）
        current_time = time.time()
        self.last_message_times[normalized_session_id] = current_time
        last_user_text = self._extract_event_text(event)

        async with self.data_lock:
            temp_state = self.session_temp_state.setdefault(normalized_session_id, {})
            temp_state["last_user_text"] = last_user_text
            temp_state["last_user_time"] = current_time

            # 合并旧键数据
            # 将旧键数据迁移到规范化键，保持计数与 self_id 连续
            if normalized_session_id != session_id and session_id in self.session_data:
                existing_payload = self.session_data.get(session_id, {})
                self.session_data.setdefault(normalized_session_id, {}).update(
                    existing_payload
                )
                del self.session_data[session_id]

            if current_time >= self.plugin_start_time:
                self.session_data.setdefault(normalized_session_id, {})[
                    "last_message_time"
                ] = current_time

        # 取消自动触发：同时处理原键与规范化键，避免漏取消
        auto_trigger_cancelled = await self._cancel_all_related_auto_triggers(
            session_id
        )
        if normalized_session_id != session_id:
            normalized_cancelled = await self._cancel_all_related_auto_triggers(
                normalized_session_id
            )
            auto_trigger_cancelled = auto_trigger_cancelled or normalized_cancelled

        # 避免重复刷屏日志；仅在确实取消了自动触发计时器时打印
        session_config = self._get_session_config(normalized_session_id)
        if (
            auto_trigger_cancelled
            and session_config
            and session_config.get("enable", False)
            and normalized_session_id not in self.first_message_logged
        ):
            self.first_message_logged.add(normalized_session_id)
            logger.info(
                f"[主动消息] 已记录 {self._get_session_log_str(normalized_session_id, session_config)} 的消息时间并取消自动触发喵。"
            )

        # 未启用或配置无效则跳过
        session_config = self._get_session_config(normalized_session_id)
        if not session_config or not session_config.get("enable", False):
            logger.debug(
                f"[主动消息] {self._get_session_log_str(session_id, session_config)} 未启用或配置无效，跳过处理喵。"
            )
            return

        # 取消旧的调度任务并重新安排
        cancelled = False
        try:
            self.scheduler.remove_job(normalized_session_id)
            cancelled = True
        except Exception:
            pass

        if normalized_session_id != session_id:
            try:
                self.scheduler.remove_job(session_id)
                cancelled = True
            except Exception:
                pass

        # 兜底清理同目标任务（处理幽灵任务）
        self._purge_related_jobs(normalized_session_id)

        if cancelled:
            logger.info(
                f"[主动消息] 用户已回复喵，已取消 {self._get_session_log_str(normalized_session_id, session_config)} 的主动消息任务喵。"
            )

        logger.info(
            f"[主动消息] 重置 {self._get_session_log_str(normalized_session_id, session_config)} 的未回复计数器为0喵。"
        )
        await self._schedule_next_chat_and_save(
            normalized_session_id, reset_counter=True
        )

    async def on_group_message(self, event: AstrMessageEvent):
        """监听群聊消息流，重置沉默倒计时，并取消已计划的主动消息任务。"""
        if not event.get_messages():
            return

        session_id = event.unified_msg_origin
        normalized_session_id = self._normalize_session_id(session_id)

        # 缓存 self_id
        if event.get_self_id():
            async with self.data_lock:
                self.session_data.setdefault(session_id, {})["self_id"] = (
                    event.get_self_id()
                )

        # 过滤 Bot 自身消息，避免把机器人发言误判成“用户活跃”
        sender_id = None
        try:
            if hasattr(event, "message_obj") and event.message_obj:
                sender = getattr(event.message_obj, "sender", None)
                if sender:
                    sender_id = getattr(sender, "id", None) or getattr(
                        sender, "user_id", None
                    )
            if not sender_id:
                sender_id = getattr(event, "user_id", None) or getattr(
                    event, "sender_id", None
                )
        except Exception as e:
            logger.debug(f"[主动消息] 获取群聊发送者ID失败喵: {e}")

        self_id = event.get_self_id() or self.session_data.get(session_id, {}).get(
            "self_id"
        )
        if self_id and sender_id and str(sender_id) == str(self_id):
            logger.debug(
                f"[主动消息] 检测到 {self._get_session_log_str(session_id)} 的 Bot 自身消息，跳过用户逻辑喵。"
            )
            return

        # 记录群聊最近用户活跃时间（用于临时态超时清理）
        current_time = time.time()
        last_user_text = self._extract_event_text(event)
        self.session_temp_state[normalized_session_id] = {
            "last_user_time": current_time,
            "last_user_text": last_user_text,
        }
        logger.debug(
            f"[主动消息] 记录 {self._get_session_log_str(session_id)} 的消息时间戳喵: {current_time}"
        )

        # 更新消息时间（仅插件启动后用于自动触发）
        self.last_message_times[normalized_session_id] = current_time

        async with self.data_lock:
            if normalized_session_id != session_id and session_id in self.session_data:
                existing_payload = self.session_data.get(session_id, {})
                self.session_data.setdefault(normalized_session_id, {}).update(
                    existing_payload
                )
                del self.session_data[session_id]

            if current_time >= self.plugin_start_time:
                self.session_data.setdefault(normalized_session_id, {})[
                    "last_message_time"
                ] = current_time
                logger.debug(
                    f"[主动消息] 已记录插件启动后 {self._get_session_log_str(session_id)} 的消息时间喵 -> {current_time}"
                )
            else:
                logger.debug(
                    f"[主动消息] 忽略插件启动前 {self._get_session_log_str(session_id)} 的旧消息用于自动主动消息任务喵 -> {current_time}"
                )

        # 取消自动触发
        auto_trigger_cancelled = await self._cancel_all_related_auto_triggers(
            session_id
        )
        if normalized_session_id != session_id:
            normalized_cancelled = await self._cancel_all_related_auto_triggers(
                normalized_session_id
            )
            auto_trigger_cancelled = auto_trigger_cancelled or normalized_cancelled

        # 读取当前会话配置，供日志与启用状态判断复用，避免重复查询。
        session_config = self._get_session_config(normalized_session_id)

        # 避免重复刷屏日志；仅在确实取消了自动触发计时器时打印
        if (
            auto_trigger_cancelled
            and session_config
            and session_config.get("enable", False)
            and normalized_session_id not in self.first_message_logged
        ):
            self.first_message_logged.add(normalized_session_id)
            logger.info(
                f"[主动消息] 已记录 {self._get_session_log_str(normalized_session_id, session_config)} 的消息时间并取消自动触发喵。"
            )

        # 未启用或配置无效则跳过
        if not session_config or not session_config.get("enable", False):
            logger.debug(
                f"[主动消息] {self._get_session_log_str(session_id, session_config)} 未启用或配置无效，跳过处理喵。"
            )
            return

        # 取消群聊中的既有调度任务（含“已持久化但未入调度器”的兜底判断）
        had_scheduled_task = False
        if self.scheduler.get_job(normalized_session_id):
            had_scheduled_task = True
        if normalized_session_id != session_id and self.scheduler.get_job(session_id):
            had_scheduled_task = True
        if (
            not had_scheduled_task
            and normalized_session_id in self.session_data
            and self._is_persisted_task_still_valid(
                normalized_session_id,
                self.session_data.get(normalized_session_id),
                current_time=current_time,
            )
        ):
            had_scheduled_task = True

        cancelled = False
        try:
            self.scheduler.remove_job(normalized_session_id)
            cancelled = True
        except Exception:
            pass

        if normalized_session_id != session_id:
            try:
                self.scheduler.remove_job(session_id)
                cancelled = True
            except Exception:
                pass

        # 兜底清理同目标任务（处理幽灵任务）
        self._purge_related_jobs(normalized_session_id)

        if cancelled:
            logger.info(
                f"[主动消息] 群聊活跃喵，已取消 {self._get_session_log_str(normalized_session_id, session_config)} 的主动消息任务喵。"
            )
        elif had_scheduled_task:
            logger.info(
                f"[主动消息] 群聊活跃喵，{self._get_session_log_str(normalized_session_id, session_config)} 未找到可取消的主动消息任务（可能已被提前清理）喵。"
            )

        # 重置沉默倒计时
        await self._reset_group_silence_timer(normalized_session_id)

        # 清理计数与任务标记：用户发言后将未回复计数归零
        async with self.data_lock:
            changed = False
            if normalized_session_id in self.session_data:
                current_unanswered = self.session_data[normalized_session_id].get(
                    "unanswered_count", 0
                )
                self.session_data[normalized_session_id]["unanswered_count"] = 0
                changed = True
                if current_unanswered > 0:
                    logger.debug(
                        f"[主动消息] {self._get_session_log_str(normalized_session_id, session_config)} 的用户已回复， 未回复计数器已重置喵。"
                    )

                if "group" in normalized_session_id.lower():
                    changed = (
                        self._clear_session_schedule_state(normalized_session_id)
                        or changed
                    )

            if changed:
                await self._save_data_internal()

    async def on_after_message_sent(self, event: AstrMessageEvent):
        """监听 Bot 发送消息后事件，重置群聊沉默倒计时。"""
        session_id = event.unified_msg_origin
        normalized_session_id = self._normalize_session_id(session_id)

        # 只对群聊生效
        if "group" not in normalized_session_id.lower():
            return

        # Bot 自己发言后，清理已计划的调度任务，避免重复触发
        try:
            self.scheduler.remove_job(normalized_session_id)
            if normalized_session_id != session_id:
                self.scheduler.remove_job(session_id)
            logger.debug(
                f"[主动消息] Bot已发言，已取消 {self._get_session_log_str(normalized_session_id)} 的主动消息任务喵。"
            )
        except Exception as e:
            logger.debug(
                f"[主动消息] {self._get_session_log_str(normalized_session_id)} 没有待取消的调度任务喵: {e}"
            )

        # 兜底清理同目标任务
        self._purge_related_jobs(normalized_session_id)

        async with self.data_lock:
            changed = False
            if normalized_session_id != session_id and session_id in self.session_data:
                existing_payload = self.session_data.get(session_id, {})
                self.session_data.setdefault(normalized_session_id, {}).update(
                    existing_payload
                )
                del self.session_data[session_id]
                changed = True

            if self._clear_session_schedule_state(normalized_session_id):
                changed = True

            if changed:
                await self._save_data_internal()

        # 周期性清理临时状态，避免 session_temp_state 长期膨胀
        current_time = time.time()
        self._cleanup_counter += 1

        # 周期性清理过期会话状态
        if self._cleanup_counter % 10 == 0:
            self._cleanup_expired_session_states(current_time)

        try:
            await self._reset_group_silence_timer(normalized_session_id)
            if normalized_session_id in self.session_temp_state:
                del self.session_temp_state[normalized_session_id]
        except Exception as e:
            logger.error(
                f"[主动消息] {self._get_session_log_str(session_id)} 的 after_message_sent 处理异常喵: {e}"
            )
