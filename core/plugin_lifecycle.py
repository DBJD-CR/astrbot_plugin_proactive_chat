"""插件生命周期模块。"""

from __future__ import annotations

import asyncio
import time
import zoneinfo
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import astrbot.api.star as star
from astrbot.api import logger


class LifecycleMixin:
    """插件生命周期混入类。"""

    context: star.Context
    data_lock: asyncio.Lock
    plugin_start_time: float
    manual_trigger_sessions: set[str]
    scheduler: AsyncIOScheduler
    timezone: zoneinfo.ZoneInfo | None
    session_data: dict
    last_message_times: dict[str, float]
    group_timers: dict[str, asyncio.TimerHandle]
    auto_trigger_timers: dict[str, asyncio.TimerHandle]
    data_dir: Any
    session_data_file: Any
    web_admin_server: Any
    notification_center: Any
    telemetry: Any
    _heartbeat_task: asyncio.Task[None] | None
    _original_exception_handler: Any
    _exception_handler_installed: bool
    _start_time: float

    async def initialize(self) -> None:
        """插件的异步初始化函数。"""
        # 初始化共享锁
        self.data_lock = asyncio.Lock()

        # 配置校验（异常不阻断启动）
        try:
            await self._validate_config()
        except Exception as e:
            logger.warning(
                f"[主动消息] 配置验证发现问题喵: {e}，将继续使用默认设置喵。"
            )

        # 加载持久化数据
        async with self.data_lock:
            await self._load_data_internal()
            # 启动时先做会话键规范化，避免历史数据中的多键并存
            normalized = self._normalize_session_data()
            if normalized:
                # 仅在发生规范化变更时回写，减少无效 IO
                await self._save_data_internal()
        logger.info("[主动消息] 已成功从文件加载会话数据喵。")

        # 恢复插件启动后的消息时间（用于自动触发判定）
        restored_count = 0
        for session_id, session_info in self.session_data.items():
            if isinstance(session_info, dict) and "last_message_time" in session_info:
                last_time = session_info["last_message_time"]
                if isinstance(last_time, (int, float)) and last_time > 0:
                    # 仅恢复“本次启动后”的消息时间，避免历史消息误触发逻辑
                    if last_time >= self.plugin_start_time:
                        self.last_message_times[session_id] = last_time
                        restored_count += 1
                        logger.debug(
                            f"[主动消息] 已恢复 {self._get_session_log_str(session_id)} 在插件启动后的消息时间喵 -> {last_time}"
                        )
                    else:
                        logger.debug(
                            f"[主动消息] 忽略插件启动前的历史消息时间用于自动主动消息任务喵: {self._get_session_log_str(session_id)} -> {last_time}"
                        )

        if restored_count > 0:
            logger.info(
                f"[主动消息] 已从持久化数据恢复 {restored_count} 个会话在插件启动后的消息时间喵。"
            )

        # 读取时区设置（失败时回退系统时区）
        try:
            self.timezone = zoneinfo.ZoneInfo(self.context.get_config().get("timezone"))
        except (zoneinfo.ZoneInfoNotFoundError, TypeError, KeyError, ValueError) as e:
            logger.warning(
                f"[主动消息] 时区配置无效或未配置喵 ({e})，将使用服务器系统时区作为备用喵。"
            )
            self.timezone = None

        # 初始化遥测生命周期
        if self.telemetry and self.telemetry.enabled:
            loop = asyncio.get_running_loop()
            self._original_exception_handler = loop.get_exception_handler()
            loop.set_exception_handler(self._handle_asyncio_exception)
            self._exception_handler_installed = True
            self._start_time = time.monotonic()
            # 启动阶段上报 startup + config，通过延迟错开避免同时请求触发服务端限流。
            self._track_task(asyncio.create_task(self._deferred_startup_telemetry()))
            # 心跳任务用于长期运行实例的活跃度统计，与启动事件互补。
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            logger.debug("[主动消息] 已启动遥测心跳任务喵。")

        # 启动调度器
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        # 先恢复持久化任务，再初始化自动触发器，避免重复调度
        await self._init_jobs_from_data()
        logger.info("[主动消息] 调度器已初始化喵。")

        await self._setup_auto_triggers_for_enabled_sessions()
        logger.info("[主动消息] 自动主动消息触发器初始化完成喵。")

        # 启动通知系统
        try:
            if self.notification_center:
                await self.notification_center.start()
        except Exception as e:
            logger.error(f"[主动消息] 通知系统启动失败喵: {e}")
            if self.telemetry and self.telemetry.enabled:
                # 这里单独标记模块来源，便于区分“通知系统不可用”与主流程异常。
                self._track_task(
                    asyncio.create_task(
                        self.telemetry.track_error(
                            e,
                            module="core.plugin_lifecycle.initialize.notification_center",
                        )
                    )
                )

        # 启动 Web 管理端
        try:
            if self.web_admin_server:
                await self.web_admin_server.start()
        except Exception as e:
            logger.error(f"[主动消息] Web 管理端启动失败喵: {e}")
            if self.telemetry and self.telemetry.enabled:
                # Web 管理端属于附加能力，错误会上报但不会阻断插件主体运行。
                self._track_task(
                    asyncio.create_task(
                        self.telemetry.track_error(
                            e,
                            module="core.plugin_lifecycle.initialize.web_admin_server",
                        )
                    )
                )

    async def terminate(self) -> None:
        """插件被卸载或停用时调用的清理函数。"""
        logger.info("[主动消息] 收到插件终止指令，开始清理资源喵。")
        # 调度器关闭优先于其它清理：必须最先、且独立 try 执行，
        # 避免遥测/计时器等任意一步抛异常时跳过 shutdown，导致旧 scheduler 残留在后台继续触发任务。
        if getattr(self, "scheduler", None) and self.scheduler.running:
            try:
                jobs = self.scheduler.get_jobs()
                logger.info(f"[主动消息] 正在清理调度器任务喵，数量: {len(jobs)}")
                for job in jobs:
                    try:
                        self.scheduler.remove_job(job.id)
                        logger.debug(f"[主动消息] 已移除调度器任务喵: {job.id}")
                    except Exception as e:
                        logger.warning(f"[主动消息] 移除调度器任务时出错喵: {e}")
                self.scheduler.shutdown(wait=False)
                logger.info("[主动消息] 调度器已关闭喵。")
            except Exception as e:
                logger.error(f"[主动消息] 关闭调度器时出错喵: {e}")
        try:
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
                self._heartbeat_task = None

            if self.telemetry and self.telemetry.enabled and self._start_time > 0:
                runtime_seconds = time.monotonic() - self._start_time
                # 终止前直接等待一次 shutdown 上报，避免任务刚创建就被后续清理逻辑取消。
                try:
                    await self.telemetry.track_shutdown(
                        exit_code=0, runtime_seconds=runtime_seconds
                    )
                except Exception as e:
                    logger.debug(f"[主动消息] shutdown 遥测上报失败喵: {e}")
                # 再清理其余挂起的 telemetry tasks，避免遗留后台任务。
                await self._cleanup_telemetry_tasks()

            if self._exception_handler_installed:
                loop = asyncio.get_running_loop()
                # 恢复条件取决于“是否曾经接管过异常处理器”，
                # 而不是 terminate 时 telemetry 的当前启用状态。
                # 原处理器即使是 None（表示默认处理器），也应完整恢复。
                loop.set_exception_handler(self._original_exception_handler)
                self._original_exception_handler = None
                self._exception_handler_installed = False
            # 取消群聊沉默计时器
            timer_count = len(self.group_timers)
            for session_id, timer in self.group_timers.items():
                try:
                    timer.cancel()
                    logger.debug(
                        f"[主动消息] 已取消 {self._get_session_log_str(session_id)} 的沉默计时器喵。"
                    )
                except Exception as e:
                    logger.warning(f"[主动消息] 取消计时器时出错喵: {e}")

            self.group_timers.clear()
            logger.info(
                f"[主动消息] 已取消 {timer_count} 个正在运行的群聊沉默计时器喵。"
            )

            # 取消自动触发计时器
            auto_trigger_count = len(self.auto_trigger_timers)
            for session_id, timer in list(self.auto_trigger_timers.items()):
                try:
                    timer.cancel()
                    logger.debug(
                        f"[主动消息] 已取消 {self._get_session_log_str(session_id)} 的自动触发计时器喵。"
                    )
                except Exception as e:
                    logger.warning(f"[主动消息] 取消自动触发计时器时出错喵: {e}")

            self.auto_trigger_timers.clear()
            logger.info(f"[主动消息] 已取消 {auto_trigger_count} 个自动触发计时器喵。")

            # 终止前最后一次持久化，尽量保留当前会话状态
            if self.data_lock:
                try:
                    async with self.data_lock:
                        await self._save_data_internal()
                    logger.info("[主动消息] 会话数据已保存喵。")
                except Exception as e:
                    logger.error(f"[主动消息] 保存数据时出错喵: {e}")

            # 停止 Web 管理端
            if self.web_admin_server:
                try:
                    await self.web_admin_server.stop()
                except Exception as e:
                    logger.warning(f"[主动消息] 停止 Web 管理端时出错喵: {e}")

            # 停止通知系统
            if self.notification_center:
                try:
                    await self.notification_center.stop()
                except Exception as e:
                    logger.warning(f"[主动消息] 停止通知系统时出错喵: {e}")
        except Exception as e:
            logger.error(f"[主动消息] 生命周期终止阶段发生异常喵: {e}")
            if self.telemetry and self.telemetry.enabled:
                try:
                    # terminate 阶段仍做 best-effort 错误上报，但绝不因为遥测再抛出新异常。
                    await self.telemetry.track_error(
                        e, module="core.plugin_lifecycle.terminate"
                    )
                except Exception:
                    pass
        finally:
            if self.telemetry:
                try:
                    await self.telemetry.close()
                except Exception as e:
                    logger.debug(f"[主动消息] 遥测会话关闭失败喵: {e}")

            # 确保终止日志一定输出
            logger.info("[主动消息] 主动消息插件已终止喵。")
