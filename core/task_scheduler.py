"""调度器与计时器模块。"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger


class SchedulerMixin:
    """调度与计时相关的混入类。"""

    scheduler: Any
    timezone: Any
    data_lock: asyncio.Lock
    session_data: dict
    group_timers: dict[str, asyncio.TimerHandle]
    auto_trigger_timers: dict[str, asyncio.TimerHandle]
    last_message_times: dict[str, float]
    plugin_start_time: float
    session_temp_state: dict[str, dict]
    _cleanup_counter: int

    async def _setup_auto_trigger(self, session_id: str, silent: bool = False) -> None:
        """为指定会话设置自动主动消息触发器。"""
        session_config = self._get_session_config(session_id)
        if not session_config:
            return

        # 读取自动触发配置
        auto_trigger_settings = session_config.get("auto_trigger_settings", {})
        if not auto_trigger_settings.get("enable_auto_trigger", False):
            logger.debug(
                f"[主动消息] {self._get_session_log_str(session_id, session_config)} 未启用自动主动消息功能喵。"
            )
            return

        auto_trigger_minutes = auto_trigger_settings.get(
            "auto_trigger_after_minutes", 5
        )
        if auto_trigger_minutes <= 0:
            logger.debug(
                f"[主动消息] {self._get_session_log_str(session_id, session_config)} 的自动触发时间设置为0，禁用自动触发喵。"
            )
            return

        # 取消旧的自动触发计时器（避免重复）
        if session_id in self.auto_trigger_timers:
            try:
                self.auto_trigger_timers[session_id].cancel()
                logger.debug(
                    f"[主动消息] 已取消 {self._get_session_log_str(session_id, session_config)} 现有的自动触发计时器喵。"
                )
            except Exception as e:
                logger.warning(f"[主动消息] 取消自动触发计时器时出错喵: {e}")
            finally:
                del self.auto_trigger_timers[session_id]

        # 闭包回调仅负责把真正逻辑投递回事件循环中的受控协程，
        # 避免在 call_later 回调里直接读写共享状态。
        def _auto_trigger_callback(captured_session_id=session_id):
            self._track_task(
                asyncio.create_task(
                    self._handle_auto_trigger_callback(
                        captured_session_id, auto_trigger_minutes
                    )
                )
            )

        try:
            loop = asyncio.get_running_loop()
            delay_seconds = auto_trigger_minutes * 60
            self.auto_trigger_timers[session_id] = loop.call_later(
                delay_seconds, _auto_trigger_callback
            )
            if not silent:
                # silent=True 用于批量初始化时避免重复日志
                logger.info(
                    f"[主动消息] 已为 {self._get_session_log_str(session_id, session_config)} 设置自动主动消息触发器喵，"
                    f"将在 {auto_trigger_minutes} 分钟后检查是否需要自动触发喵。"
                )
        except Exception as e:
            logger.error(f"[主动消息] 设置自动触发计时器失败喵: {e}")

    async def _cancel_auto_trigger(self, session_id: str) -> bool:
        """取消指定会话的自动主动消息触发器。"""
        cancelled = False
        if session_id in self.auto_trigger_timers:
            try:
                self.auto_trigger_timers[session_id].cancel()
                cancelled = True
                logger.info(
                    f"[主动消息] 已取消 {self._get_session_log_str(session_id)} 的自动触发计时器喵。"
                )
            except Exception as e:
                logger.warning(f"[主动消息] 取消自动触发计时器时出错喵: {e}")
            finally:
                del self.auto_trigger_timers[session_id]
        return cancelled

    async def _cancel_all_related_auto_triggers(self, session_id: str) -> bool:
        """取消指定会话的自动触发器（UMO 直接匹配）。"""
        return await self._cancel_auto_trigger(session_id)

    def _is_friend_type(self, msg_type: str) -> bool:
        return "Friend" in msg_type or "Private" in msg_type

    def _is_persisted_task_still_valid(
        self,
        session_id: str,
        session_info: dict | None,
        current_time: float | None = None,
    ) -> bool:
        """判断持久化任务是否仍然有效且可恢复。"""
        if not isinstance(session_info, dict):
            return False

        session_config = self._get_session_config(session_id)
        if not session_config or not session_config.get("enable", False):
            return False

        next_trigger = session_info.get("next_trigger_time")
        if not isinstance(next_trigger, (int, float)):
            return False

        check_time = current_time if current_time is not None else time.time()
        # 与 APScheduler misfire_grace_time 保持一致，允许 60 秒轻微抖动
        return check_time < (next_trigger + 60)

    def _clear_session_schedule_state(
        self,
        session_id: str,
        *,
        keep_unanswered_count: bool = True,
        keep_last_message_time: bool = True,
        keep_self_id: bool = True,
    ) -> bool:
        """清理会话上的调度持久化字段，避免残留幽灵任务状态。"""
        session_info = self.session_data.get(session_id)
        if not isinstance(session_info, dict):
            return False

        protected_keys = set()
        if keep_unanswered_count:
            protected_keys.add("unanswered_count")
        if keep_last_message_time:
            protected_keys.add("last_message_time")
        if keep_self_id:
            protected_keys.add("self_id")

        schedule_keys = {
            "next_trigger_time",
            "last_scheduled_at",
            "last_schedule_min_interval_seconds",
            "last_schedule_max_interval_seconds",
            "last_schedule_random_interval_seconds",
        }

        changed = False
        for key in schedule_keys:
            if key in protected_keys:
                continue
            if key in session_info:
                del session_info[key]
                changed = True

        return changed

    def _purge_related_jobs(self, session_id: str) -> None:
        """清理同一目标但不同 UMO 的调度任务，防止幽灵任务。"""
        parsed = self._parse_session_id(session_id)
        if not parsed:
            return

        _, msg_type, target_id = parsed
        is_friend = self._is_friend_type(msg_type)

        for job in self.scheduler.get_jobs():
            job_id = str(job.id)
            job_parsed = self._parse_session_id(job_id)
            if not job_parsed:
                continue
            _, job_type, job_target = job_parsed
            if self._is_friend_type(job_type) == is_friend and job_target == target_id:
                try:
                    self.scheduler.remove_job(job.id)
                except Exception:
                    pass

    def _add_chat_job(self, session_id: str, run_date: datetime) -> None:
        """规范化 session_id，清理同目标历史任务，并注册一个新的 check_and_chat 定时 job。"""
        normalized = self._normalize_session_id(session_id)
        self._purge_related_jobs(normalized)
        self.scheduler.add_job(
            self.check_and_chat,
            "date",
            run_date=run_date,
            args=[normalized],
            id=normalized,
            replace_existing=True,
            misfire_grace_time=60,
        )

    def _has_related_persisted_task(self, session_id: str) -> bool:
        """判断同一目标是否存在仍可恢复的持久化任务（避免重复触发）。"""
        parsed = self._parse_session_id(session_id)
        if not parsed:
            return False

        _, msg_type, target_id = parsed
        is_friend = self._is_friend_type(msg_type)
        current_time = time.time()

        for existing_id, session_info in list(self.session_data.items()):
            existing_parsed = self._parse_session_id(existing_id)
            if not existing_parsed:
                continue
            _, existing_type, existing_target = existing_parsed
            if (
                self._is_friend_type(existing_type) == is_friend
                and existing_target == target_id
                and self._is_persisted_task_still_valid(
                    existing_id, session_info, current_time=current_time
                )
            ):
                return True

        return False

    def _resolve_session_id_for_config(
        self, session_id: str, session_config: dict
    ) -> str:
        """将配置中的会话标识解析为完整 UMO。"""
        parsed = self._parse_session_id(session_id)
        if parsed:
            return session_id

        session_type = session_config.get("_session_type", "friend")
        msg_type = "FriendMessage" if session_type == "friend" else "GroupMessage"
        return self._resolve_full_umo(str(session_id), msg_type)

    async def _setup_auto_triggers_for_enabled_sessions(self) -> None:
        """为所有启用了自动触发功能的会话设置自动主动消息触发器。"""
        logger.info("[主动消息] 开始检查并设置自动主动消息触发器喵...")

        # 统计：成功创建、已存在持久化任务、无效/未配置、未启用自动触发、已达未回复上限
        auto_trigger_count = 0
        skipped_existing = 0
        skipped_invalid = 0
        skipped_disabled = 0
        skipped_max_unanswered = 0

        # 私聊 session_list 批量注册
        friend_settings = self.config.get("friend_settings", {})
        if friend_settings.get("enable", False):
            for session_id in friend_settings.get("session_list", []):
                result = await self._setup_auto_trigger_for_session_config(
                    friend_settings, session_id
                )
                if result == "created":
                    auto_trigger_count += 1
                elif result == "existing":
                    skipped_existing += 1
                elif result == "invalid":
                    skipped_invalid += 1
                elif result == "disabled":
                    skipped_disabled += 1
                elif result == "max_unanswered":
                    skipped_max_unanswered += 1

        # 群聊 session_list 批量注册
        group_settings = self.config.get("group_settings", {})
        if group_settings.get("enable", False):
            for session_id in group_settings.get("session_list", []):
                result = await self._setup_auto_trigger_for_session_config(
                    group_settings, session_id
                )
                if result == "created":
                    auto_trigger_count += 1
                elif result == "existing":
                    skipped_existing += 1
                elif result == "invalid":
                    skipped_invalid += 1
                elif result == "disabled":
                    skipped_disabled += 1
                elif result == "max_unanswered":
                    skipped_max_unanswered += 1

        # 汇总日志
        has_auto_trigger_config = False
        if friend_settings.get("auto_trigger_settings", {}).get(
            "enable_auto_trigger", False
        ):
            has_auto_trigger_config = True
        if group_settings.get("auto_trigger_settings", {}).get(
            "enable_auto_trigger", False
        ):
            has_auto_trigger_config = True

        if auto_trigger_count == 0:
            if has_auto_trigger_config:
                # 仅用于提示“没创建”的具体原因，避免误导为“会话无效”
                reasons = []
                if skipped_existing:
                    reasons.append(f"{skipped_existing} 个会话已有持久化任务")
                if skipped_invalid:
                    reasons.append(f"{skipped_invalid} 个会话无效或未配置")
                if skipped_disabled:
                    reasons.append(f"{skipped_disabled} 个会话未启用自动触发")
                if skipped_max_unanswered:
                    reasons.append(
                        f"{skipped_max_unanswered} 个会话已达到未回复次数上限"
                    )
                reason_str = "，".join(reasons) if reasons else "未发现可设置的会话"
                logger.info(
                    f"[主动消息] 检测到自动主动消息配置，但没有需要设置的触发器喵（{reason_str}）。"
                )
            else:
                logger.info("[主动消息] 没有会话启用自动主动消息功能喵。")
        else:
            logger.info(
                f"[主动消息] 已为 {auto_trigger_count} 个会话设置自动主动消息触发器喵。"
                f"（跳过：已有任务 {skipped_existing}，无效 {skipped_invalid}，未启用 {skipped_disabled}，"
                f"已达未回复上限 {skipped_max_unanswered}）"
            )

    async def _setup_auto_trigger_for_session_config(
        self, settings: dict, session_id: str
    ) -> str:
        """为指定会话配置设置自动触发器。"""
        # 返回值：created/existing/invalid/disabled/max_unanswered，用于日志汇总与原因归类
        session_config = self._get_session_config(session_id)
        if not session_config or not session_config.get("enable", False):
            # 未命中 session_list 或被禁用，都视为无效会话
            return "invalid"

        # 将配置项中的会话标识补全为可发送的完整 UMO
        resolved_session_id = self._resolve_session_id_for_config(
            session_id, session_config
        )

        auto_trigger_settings = session_config.get("auto_trigger_settings", {})
        if not auto_trigger_settings.get("enable_auto_trigger", False):
            logger.debug(
                f"[主动消息] {self._get_session_log_str(resolved_session_id)} 未启用自动主动消息功能喵。"
            )
            return "disabled"

        # 检查是否已有有效的持久化任务（同一目标）
        if self._has_related_persisted_task(resolved_session_id):
            logger.info(
                f"[主动消息] {self._get_session_log_str(resolved_session_id)} 已存在持久化的主动消息任务喵，"
                f"跳过自动触发器设置以避免冲突喵。"
            )
            return "existing"

        schedule_conf = session_config.get("schedule_settings", {})
        max_unanswered = schedule_conf.get("max_unanswered_times", 3)
        unanswered_count = self.session_data.get(resolved_session_id, {}).get(
            "unanswered_count", 0
        )
        if max_unanswered > 0 and unanswered_count >= max_unanswered:
            logger.info(
                f"[主动消息] {self._get_session_log_str(resolved_session_id, session_config)} 的未回复次数 ({unanswered_count}) "
                f"已达到上限 ({max_unanswered})，跳过初始化自动触发器设置喵。"
            )
            return "max_unanswered"

        logger.debug(
            f"[主动消息] 正在为 {self._get_session_log_str(resolved_session_id)} 设置自动触发器喵。"
        )
        auto_trigger_minutes = auto_trigger_settings.get(
            "auto_trigger_after_minutes", 5
        )
        logger.info(
            f"[主动消息] 已为 {self._get_session_log_str(resolved_session_id)} 设置自动触发器喵，"
            f"将在 {auto_trigger_minutes} 分钟后检查是否需要自动触发喵。"
        )
        await self._setup_auto_trigger(resolved_session_id, silent=True)
        return "created"

    async def _init_jobs_from_data(self) -> None:
        """从已加载的 session_data 中恢复定时任务。"""
        restored_count = 0
        cleaned_runtime_state = 0
        current_time = time.time()

        logger.info(
            f"[主动消息] 开始从数据恢复定时任务喵，当前时间: {datetime.fromtimestamp(current_time)}"
        )

        # 清理旧格式数据（历史遗留的 session_id）
        cleaned_count = self._cleanup_invalid_session_data()
        if cleaned_count > 0:
            logger.info(f"[主动消息] 清理了 {cleaned_count} 个无效的会话数据条目喵。")
            async with self.data_lock:
                await self._save_data_internal()

        logger.debug(f"[主动消息] 会话数据条目数: {len(self.session_data)}")

        # 遍历持久化任务并恢复调度器
        for session_id, session_info in list(self.session_data.items()):
            session_config = self._get_session_config(session_id)
            if not session_config or not session_config.get("enable", False):
                if self._clear_session_schedule_state(session_id):
                    cleaned_runtime_state += 1
                    logger.info(
                        f"[主动消息] {self._get_session_log_str(session_id, session_config)} 的配置无效或已禁用，已清理残留调度状态喵。"
                    )
                continue

            # 仅恢复存在 next_trigger_time 的持久化任务
            next_trigger = session_info.get("next_trigger_time")
            if not next_trigger:
                logger.debug(
                    f"[主动消息] {self._get_session_log_str(session_id, session_config)} 没有next_trigger_time，跳过喵"
                )
                continue

            if not self._is_persisted_task_still_valid(
                session_id, session_info, current_time=current_time
            ):
                logger.info(
                    f"[主动消息] {self._get_session_log_str(session_id, session_config)} 的持久化任务已过期或无效，清理后跳过恢复喵。"
                )
                if self._clear_session_schedule_state(session_id):
                    cleaned_runtime_state += 1
                    logger.debug(
                        f"[主动消息] 已清理 {self._get_session_log_str(session_id, session_config)} 的过期持久化状态喵。"
                    )
                continue

            try:
                run_date = datetime.fromtimestamp(next_trigger, tz=self.timezone)
                existing_job = self.scheduler.get_job(session_id)
                if existing_job:
                    logger.debug(
                        f"[主动消息] {self._get_session_log_str(session_id, session_config)} 的任务已存在，跳过恢复喵。"
                    )
                    continue

                self._add_chat_job(session_id, run_date)
                logger.info(
                    f"[主动消息] 已成功从文件恢复任务喵: {self._get_session_log_str(session_id, session_config)}, 执行时间: {run_date} 喵"
                )
                restored_count += 1
            except Exception as e:
                logger.error(
                    f"[主动消息] 添加 {self._get_session_log_str(session_id, session_config)} 的恢复任务到调度器时失败喵: {e}"
                )
                if self._clear_session_schedule_state(session_id):
                    cleaned_runtime_state += 1
                    logger.warning(
                        f"[主动消息] {self._get_session_log_str(session_id, session_config)} 的恢复任务创建失败，已清理残留持久化状态喵。"
                    )

        if cleaned_runtime_state > 0:
            async with self.data_lock:
                await self._save_data_internal()

        logger.info(
            f"[主动消息] 任务恢复检查完成，共恢复 {restored_count} 个定时任务喵。"
        )
        if cleaned_runtime_state > 0:
            logger.info(
                f"[主动消息] 启动恢复阶段额外清理了 {cleaned_runtime_state} 个残留调度状态喵。"
            )
        if restored_count == 0:
            logger.info("[主动消息] 没有需要恢复的定时任务喵。")

    async def _schedule_next_chat_and_save(
        self, session_id: str, reset_counter: bool = False
    ) -> None:
        """安排下一次主动聊天并立即将状态持久化到文件。"""
        normalized_session_id = self._normalize_session_id(session_id)
        session_config = self._get_session_config(normalized_session_id)
        if not session_config:
            return

        schedule_conf = session_config.get("schedule_settings", {})

        async with self.data_lock:
            # 如果存在非规范化的旧键，迁移到规范化键
            if normalized_session_id != session_id and session_id in self.session_data:
                existing_payload = self.session_data.get(session_id, {})
                self.session_data.setdefault(normalized_session_id, {}).update(
                    existing_payload
                )
                del self.session_data[session_id]

            # 清理可能残留的旧任务（旧键）
            if normalized_session_id != session_id:
                try:
                    self.scheduler.remove_job(session_id)
                except Exception:
                    pass

            # 用户回复时重置计数器
            if reset_counter:
                self.session_data.setdefault(normalized_session_id, {})[
                    "unanswered_count"
                ] = 0

            # 计算随机触发时间
            min_interval = int(schedule_conf.get("min_interval_minutes", 30)) * 60
            max_interval = max(
                min_interval, int(schedule_conf.get("max_interval_minutes", 900)) * 60
            )
            random_interval = random.randint(min_interval, max_interval)
            scheduled_at = time.time()
            next_trigger_time = scheduled_at + random_interval
            run_date = datetime.fromtimestamp(next_trigger_time, tz=self.timezone)

            # 更新调度器与持久化数据
            # 先清理同目标历史任务，再写入新任务，确保同一目标仅一条生效
            self._add_chat_job(normalized_session_id, run_date)

            session_payload = self.session_data.setdefault(normalized_session_id, {})
            session_payload["next_trigger_time"] = next_trigger_time
            session_payload["last_scheduled_at"] = scheduled_at
            session_payload["last_schedule_min_interval_seconds"] = min_interval
            session_payload["last_schedule_max_interval_seconds"] = max_interval
            session_payload["last_schedule_random_interval_seconds"] = random_interval
            logger.info(
                f"[主动消息] 已为 {self._get_session_log_str(normalized_session_id, session_config)} 安排下一次主动消息喵，时间：{run_date.strftime('%Y-%m-%d %H:%M:%S')} 喵。"
            )

            await self._save_data_internal()

    async def _reset_group_silence_timer(self, session_id: str) -> None:
        """重置指定群聊的沉默倒计时。"""
        normalized_session_id = self._normalize_session_id(session_id)
        session_config = self._get_session_config(normalized_session_id)
        if not session_config or not session_config.get("enable", False):
            return

        # 取消旧计时器（包含旧键）
        for timer_key in [session_id, normalized_session_id]:
            if timer_key in self.group_timers:
                try:
                    self.group_timers[timer_key].cancel()
                except Exception as e:
                    logger.warning(
                        f"[主动消息] 取消 {self._get_session_log_str(timer_key, session_config)} 的旧计时器时出错喵: {e}"
                    )
                finally:
                    del self.group_timers[timer_key]

        idle_minutes = session_config.get("group_idle_trigger_minutes", 10)

        # 群聊沉默回调仅负责投递受控协程，
        # 真正的状态检查与调度写入放到异步上下文中统一处理。
        def _schedule_callback(captured_session_id=normalized_session_id):
            self._track_task(
                asyncio.create_task(
                    self._handle_group_silence_callback(
                        captured_session_id, idle_minutes
                    )
                )
            )

        try:
            loop = asyncio.get_running_loop()
            self.group_timers[normalized_session_id] = loop.call_later(
                idle_minutes * 60, _schedule_callback
            )
        except Exception as e:
            logger.error(f"[主动消息] 设置沉默倒计时失败喵: {e}")

    async def _handle_auto_trigger_callback(
        self, session_id: str, auto_trigger_minutes: int | float
    ) -> None:
        """在异步上下文中处理自动触发回调，避免直接在定时器回调里操作共享状态。"""
        try:
            async with self.data_lock:
                # 计时器已被取消则直接跳过
                if session_id not in self.auto_trigger_timers:
                    logger.debug(
                        f"[主动消息] {self._get_session_log_str(session_id)} 的自动触发已被取消，跳过喵。"
                    )
                    return

                # 确认配置仍然启用
                current_config = self._get_session_config(session_id)
                if not current_config or not current_config.get("enable", False):
                    logger.info(
                        f"[主动消息] {self._get_session_log_str(session_id, current_config)} 的配置已禁用，取消自动触发喵。"
                    )
                    return

                # 仅在插件启动后未收到任何消息时触发
                last_message_time = self.last_message_times.get(session_id, 0)
                current_time = time.time()
                time_since_plugin_start = current_time - self.plugin_start_time

                # 仅“启动后始终无人发言”的会话满足自动触发条件
                if last_message_time != 0 or time_since_plugin_start < (
                    auto_trigger_minutes * 60
                ):
                    return

                schedule_conf = current_config.get("schedule_settings", {})
                min_interval = int(schedule_conf.get("min_interval_minutes", 30)) * 60
                max_interval = max(
                    min_interval,
                    int(schedule_conf.get("max_interval_minutes", 900)) * 60,
                )
                random_interval = random.randint(min_interval, max_interval)
                scheduled_at = time.time()
                next_trigger_time = scheduled_at + random_interval
                run_date = datetime.fromtimestamp(next_trigger_time, tz=self.timezone)

                # 自动触发生成的任务虽然不持久化到磁盘，但仍需补齐运行时元信息，
                # 以便 Web 管理端能够正确计算倒计时进度，而不是误判为满进度。
                normalized_for_job = self._normalize_session_id(session_id)
                session_payload = self.session_data.setdefault(normalized_for_job, {})
                session_payload["last_scheduled_at"] = scheduled_at
                session_payload["last_schedule_min_interval_seconds"] = min_interval
                session_payload["last_schedule_max_interval_seconds"] = max_interval
                session_payload["last_schedule_random_interval_seconds"] = (
                    random_interval
                )

                self._add_chat_job(normalized_for_job, run_date)

                logger.info(
                    f"[主动消息] {self._get_session_log_str(session_id, current_config)} 满足条件，自动触发任务已创建喵！执行时间 (非持久化): {run_date.strftime('%Y-%m-%d %H:%M:%S')} 喵"
                )
        except Exception as e:
            logger.error(f"[主动消息] 自动触发任务创建失败喵: {e}")
        finally:
            # 触发一次后移除计时器
            if session_id in self.auto_trigger_timers:
                del self.auto_trigger_timers[session_id]

    async def _handle_group_silence_callback(
        self, session_id: str, idle_minutes: int | float
    ) -> None:
        """在异步上下文中处理群聊沉默回调，避免直接在定时器回调里操作共享状态。"""
        try:
            async with self.data_lock:
                # 若计时器已被重置则跳过
                if session_id not in self.group_timers:
                    return

                # 先移除当前已触发的句柄，避免状态页继续把它识别为“仍在运行的群沉默计时器”
                del self.group_timers[session_id]

                # 确保会话数据存在
                if session_id not in self.session_data:
                    logger.info(
                        f"[主动消息] {self._get_session_log_str(session_id)} 的会话数据不存在，创建初始会话数据喵。"
                    )
                    self.session_data[session_id] = {"unanswered_count": 0}

                current_config = self._get_session_config(session_id)
                if not current_config or not current_config.get("enable", False):
                    logger.info(
                        f"[主动消息] {self._get_session_log_str(session_id, current_config)} 的配置已禁用或不存在，跳过主动消息创建喵。"
                    )
                    return

                current_unanswered = self.session_data.get(session_id, {}).get(
                    "unanswered_count", 0
                )

            self._track_task(
                asyncio.create_task(
                    self._schedule_next_chat_and_save(session_id, reset_counter=False)
                )
            )
            logger.info(
                f"[主动消息] {self._get_session_log_str(session_id, current_config)} 已沉默 {idle_minutes} 分钟，开始计划主动消息喵。(当前未回复次数: {current_unanswered})"
            )
        except Exception as e:
            logger.error(f"[主动消息] 沉默倒计时回调函数执行失败喵: {e}")

    def _cleanup_expired_session_states(self, current_time: float) -> None:
        """清理过期的会话状态，防止内存泄漏。"""
        expired_sessions: list[str] = []
        timeout_seconds = 300

        # 标记超过阈值的会话状态
        for session_id, state in self.session_temp_state.items():
            last_user_time = state.get("last_user_time", 0)
            if current_time - last_user_time > timeout_seconds:
                expired_sessions.append(session_id)

        # 清理过期状态
        for session_id in expired_sessions:
            del self.session_temp_state[session_id]
