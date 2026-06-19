# 文件名: main.py (位于 data/plugins/astrbot_plugin_proactive_chat/ 目录下)
# 版本: v1.2.0

"""插件入口与主类定义。"""

from __future__ import annotations

import asyncio
import re
import time

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig

# 导入各模块的 Mixins，用于组装插件能力
from .core.chat_flow import ProactiveCoreMixin
from .core.data_storage import StorageMixin
from .core.llm_adapter import LlmMixin
from .core.message_events import EventsMixin
from .core.message_sender import SenderMixin
from .core.notification_center import NotificationCenter
from .core.plugin_lifecycle import LifecycleMixin
from .core.session_config import ConfigMixin
from .core.session_override_manager import SessionOverrideManager
from .core.session_parser import SessionMixin
from .core.task_scheduler import SchedulerMixin
from .core.telemetry_manager import TelemetryManager
from .core.web_admin_server import WebAdminServer
from .utils.version import get_plugin_version


class ProactiveChatPlugin(
    SessionMixin,  # 会话 ID 解析、规范化与日志格式化
    StorageMixin,  # 会话数据加载/保存与迁移清理
    ConfigMixin,  # 配置读取与会话级配置路由
    SchedulerMixin,  # 定时任务、自动触发与沉默计时
    LlmMixin,  # 上下文准备与 LLM 调用封装
    SenderMixin,  # 主动消息发送与装饰钩子
    EventsMixin,  # 私聊/群聊事件监听处理
    LifecycleMixin,  # initialize/terminate 生命周期管理
    ProactiveCoreMixin,  # 主动消息主流程编排
    star.Star,
):
    """
    插件的主类，负责生命周期管理、事件监听和核心逻辑执行。
    """

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)

        # 注入的配置对象（由 AstrBot 框架提供）
        self.config: AstrBotConfig = config
        # 调度器与时区会在 initialize 中初始化
        self.scheduler = None  # AsyncIOScheduler 实例（initialize 中创建）
        self.timezone = None  # ZoneInfo 时区对象（initialize 中加载）

        # 使用 StarTools 获取插件专属数据目录（Path 对象）
        self.data_dir = star.StarTools.get_data_dir("astrbot_plugin_proactive_chat")
        self.session_data_file = self.data_dir / "session_data.json"

        # 共享锁与持久化数据容器
        self.data_lock = None
        self.session_data: dict = {}
        # 记录当前正在执行“立即触发”的会话，防止重复点击导致并发主动消息。
        self.manual_trigger_sessions: set[str] = set()

        # 会话差异配置管理器、通知中心与 Web 管理端
        self.session_override_manager = SessionOverrideManager(self.data_dir)
        self.notification_center = NotificationCenter(self)
        try:
            self.web_admin_server = WebAdminServer(self)
        except Exception as e:
            # Web 管理端属于增强能力，创建失败时仅禁用控制台，不影响插件主体继续加载。
            self.web_admin_server = None
            logger.error(f"[主动消息] Web 管理端组件创建失败喵，已自动禁用: {e}")
        # 插件版本统一通过版本工具读取，供遥测、通知系统、状态接口等多个模块复用。
        self.version = get_plugin_version()
        # 遥测管理器在插件实例创建阶段即初始化，但真正发请求仍由生命周期阶段控制。
        self.telemetry = TelemetryManager(
            config=dict(self.config),
            plugin_version=self.version,
        )
        # 保存所有已创建但尚未完成的遥测任务引用，避免被垃圾回收或在终止时遗漏清理。
        self._telemetry_tasks: set[asyncio.Task[None]] = set()
        # 独立的心跳后台任务句柄；插件关闭时需要显式 cancel。
        self._heartbeat_task: asyncio.Task[None] | None = None
        # 使用单调时钟记录遥测启动时间，用于计算 uptime，避免系统时间跳变造成误差。
        self._start_time: float = 0.0
        # 保存原 asyncio 全局异常处理器，以便插件卸载时恢复原状。
        self._original_exception_handler = None
        # 标记是否接管过全局异常处理器；必须在 __init__ 初始化，
        # 否则遥测关闭时 terminate 访问该属性会抛 AttributeError，导致后续清理（含调度器关闭）全部中断。
        self._exception_handler_installed = False

        # 群聊沉默倒计时与自动触发计时器
        self.group_timers: dict[str, asyncio.TimerHandle] = {}
        self.last_bot_message_time = 0  # 预留字段：记录 Bot 最近发言时间
        self.session_temp_state: dict[
            str, dict
        ] = {}  # 临时态（如群聊最后用户发言时间）
        self.last_message_times: dict[str, float] = {}  # 会话最近消息时间，用于触发判断
        self.auto_trigger_timers: dict[
            str, asyncio.TimerHandle
        ] = {}  # 自动触发计时器句柄
        # 插件启动时间与日志控制
        self.plugin_start_time = time.time()
        self.first_message_logged: set[str] = set()
        self._cleanup_counter = 0

        logger.info("[主动消息] 插件实例已创建喵。")

    def _track_task(self, task: asyncio.Task[None] | None) -> asyncio.Task[None] | None:
        """登记遥测任务引用，避免任务过早释放。"""
        if task is None:
            return None
        # 统一把遥测 task 收口到集合中，便于生命周期结束时批量取消与等待回收。
        self._telemetry_tasks.add(task)
        # 任务结束后自动把自己从集合移除，避免集合无限增长。
        task.add_done_callback(self._telemetry_tasks.discard)
        return task

    async def _cleanup_telemetry_tasks(self) -> None:
        """清理所有未完成的遥测任务。"""
        if not self._telemetry_tasks:
            return

        # 先做快照，避免遍历过程中因回调移除元素导致集合发生变化。
        pending_tasks = list(self._telemetry_tasks)
        for task in pending_tasks:
            if not task.done():
                # 未完成任务先统一取消，防止插件关闭时仍有上报在后台悬挂。
                task.cancel()

        if pending_tasks:
            # 吞掉所有异常，确保遥测清理失败不会影响插件主清理流程。
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        self._telemetry_tasks.clear()

    async def _deferred_startup_telemetry(self) -> None:
        """错开上报 startup 与 config 事件，避免并发请求触发服务端限流。"""
        try:
            await self.telemetry.track_startup()
            # 间隔 2 秒再发第二个事件，降低被服务端判定为突发流量的概率。
            await asyncio.sleep(2)
            await self.telemetry.track_config(dict(self.config))
        except Exception as e:
            logger.debug(f"[主动消息] 启动遥测上报失败喵: {e}")

    async def _heartbeat_loop(self) -> None:
        """遥测心跳循环。"""
        # 心跳间隔沿用参考插件的 12 小时策略，既能观测活跃安装量，又不会过于频繁。
        heartbeat_interval = 43200
        try:
            while True:
                if not self.telemetry or not self.telemetry.enabled:
                    # 若用户关闭了遥测，则心跳循环仅休眠，不主动退出，方便后续动态恢复。
                    await asyncio.sleep(heartbeat_interval)
                    continue

                # 运行时长基于 monotonic 计算，避免系统时间调整导致 uptime 倒退或突增。
                uptime = time.monotonic() - self._start_time
                try:
                    await self.telemetry.track_heartbeat(uptime_seconds=uptime)
                except Exception as e:
                    # 心跳上报失败只记 debug，绝不影响插件主业务逻辑。
                    logger.debug(f"[主动消息] 遥测心跳发送失败喵: {e}")

                await asyncio.sleep(heartbeat_interval)
        except asyncio.CancelledError:
            logger.debug("[主动消息] 遥测心跳任务已取消喵。")
            raise
        except Exception as e:
            logger.error(f"[主动消息] 遥测心跳循环异常喵: {e}")

    def _handle_asyncio_exception(self, loop, context) -> None:
        """全局 asyncio 异常处理器，仅处理当前插件相关异常。"""
        # asyncio 在 task 未被 await 且异常冒泡时，会把上下文传给全局异常处理器。
        exception = context.get("exception")
        message = context.get("message", "未知异常")

        is_plugin_exception = False
        if exception:
            # 逐帧检查 traceback 来源，只拦截当前插件内部抛出的未处理异步异常。
            tb = exception.__traceback__
            while tb is not None:
                filename = tb.tb_frame.f_code.co_filename
                if "astrbot_plugin_proactive_chat" in filename:
                    is_plugin_exception = True
                    break
                tb = tb.tb_next

        if not is_plugin_exception:
            # 不是本插件的异常时，必须把处理权交还给原处理器，避免污染全局行为。
            if self._original_exception_handler:
                self._original_exception_handler(loop, context)
            else:
                loop.default_exception_handler(context)
            return

        if exception:
            logger.error(f"[主动消息] 捕获未处理的异步异常喵: {exception}")
            logger.error(f"[主动消息] 异常上下文喵: {message}")
        else:
            logger.error(f"[主动消息] 捕获未处理的异步错误喵: {message}")

        if self.telemetry and self.telemetry.enabled:
            task_name = "unknown"
            future = context.get("future")
            if future:
                # 优先取 task 的显示名称，方便遥测平台按任务来源聚类问题。
                task_name = getattr(future, "get_name", lambda: str(future))()
                if not task_name or task_name == str(future):
                    future_repr = repr(future)
                    match = re.search(r"name='([^']+)'", future_repr)
                    if match:
                        task_name = match.group(1)

            # 若 asyncio context 未提供具体 exception，则退化包装成 RuntimeError 进行统一上报。
            error = exception or RuntimeError(message)
            self._track_task(
                asyncio.create_task(
                    self.telemetry.track_error(
                        error,
                        module=f"main.unhandled_async.{task_name}",
                    )
                )
            )

    async def terminate(self) -> None:
        """插件终止入口：委托 LifecycleMixin 清理。"""
        await LifecycleMixin.terminate(self)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=999)
    async def on_friend_message(self, event: AstrMessageEvent) -> None:
        """私聊消息入口：委托 EventsMixin 处理。"""
        # 主类仅做入口转发，具体逻辑由 EventsMixin 实现
        await EventsMixin.on_friend_message(self, event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=998)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """群聊消息入口：委托 EventsMixin 处理。"""
        # 主类仅做入口转发，具体逻辑由 EventsMixin 实现
        await EventsMixin.on_group_message(self, event)

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """消息发送后入口：委托 EventsMixin 处理。"""
        # 主类仅做入口转发，具体逻辑由 EventsMixin 实现
        await EventsMixin.on_after_message_sent(self, event)
