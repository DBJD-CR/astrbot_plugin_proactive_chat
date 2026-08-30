"""主动消息插件遥测管理模块。"""

from __future__ import annotations

import asyncio
import base64
import copy
import platform
import re
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.star import StarTools

from ..utils.version import get_astrbot_version_info


class TelemetryManager:
    """遥测管理器，负责匿名上报插件运行状态、配置快照与错误信息。"""

    # 统一的遥测接收端地址
    _ENDPOINT = "https://plugincenter.aloys23.link/api/ingest"
    # 当前项目专用的 App Key 以 Base64 形式存放，运行时再解码使用。
    _ENCODED_APP_KEY = "dGtfTUlMdzk5NTZ4aGNZTm54RzBzSHZzdFl3VjhnN0YtUmc="
    _APP_KEY = base64.b64decode(_ENCODED_APP_KEY).decode()
    # 插件名用于定位插件私有数据目录，并持久化实例级匿名 ID。
    _PLUGIN_NAME = "astrbot_plugin_proactive_chat"
    # 匹配统一消息来源 UMO，避免错误日志或异常文本把真实会话标识直接上传到遥测平台。
    _UMO_PATTERN = re.compile(
        r"(?P<umo>[A-Za-z0-9_.-]+:(?:FriendMessage|PrivateMessage|GroupMessage|GuildMessage):[^\s,'\"]+)"
    )
    # 匹配 password/token/key/secret 这类典型敏感键值，做统一掩码替换。
    _KEY_VALUE_PATTERN = re.compile(
        r"(?i)(password|token|secret|api[_-]?key|app[_-]?key)(\s*[=:]\s*)([^,\s]+)"
    )
    # 匹配 prompt / system_prompt / proactive_prompt 字段，确保提示词正文被整段过滤。
    _PROMPT_FIELD_PATTERN = re.compile(
        r'(?is)("?(?:proactive_prompt|system_prompt|prompt)"?\s*[:=]\s*)(.+?)(?=,\s*"?[A-Za-z0-9_]+"?\s*[:=]|\}|\]|$)'
    )

    # 收到 429 后的静默冷却时长（秒），期间新事件转入 backlog 而非直接丢弃。
    _RATE_LIMIT_COOLDOWN = 300
    # 恢复同步时每批最多发送的事件数。
    _DRAIN_BATCH_SIZE = 10
    # 恢复同步时每批之间的间隔（秒）。
    _DRAIN_INTERVAL = 5
    # backlog 最大容量，超出时丢弃最旧的事件。
    _BACKLOG_MAX_SIZE = 200
    # feature 事件弹性打包：攒满此数量立即 flush。
    _FLUSH_THRESHOLD = 5
    # feature 事件弹性打包：首条事件进入 buffer 后最多等待此秒数再 flush。
    _FLUSH_TIMEOUT = 15

    def __init__(self, config: dict[str, Any], plugin_version: str = "unknown") -> None:
        # 保留原始配置引用，方便后续在需要时提取配置快照或扩展更多上下文字段。
        self._config = config
        # 版本号由统一版本工具提供，避免不同模块各自解析 metadata 造成不一致。
        self._plugin_version = plugin_version
        # 遥测默认开启，只有用户明确在 telemetry_config 中关闭时才停用。
        telemetry_config = config.get("telemetry_config", {})
        self._enabled = telemetry_config.get("enabled", True)
        # 为当前安装实例生成稳定匿名 ID，用于跨重启聚合同一实例的遥测事件。
        self._instance_id = self._get_or_create_instance_id()
        # HTTP session 延迟初始化，只有真正发请求时才创建，减少空载资源占用。
        self._session: aiohttp.ClientSession | None = None
        # 当前先固定为 production，后续若接入测试环境可在这里扩展切换。
        self._env = "production"
        # 熔断器：记录上次收到 429 的单调时间戳，冷却期内跳过所有请求。
        self._rate_limited_until: float = 0.0
        # feature 事件弹性打包缓冲区：track_feature 只往这里追加，由阈值或超时触发 flush。
        self._feature_buffer: list[dict] = []
        # 超时 flush 任务：首条事件入 buffer 时启动，到期后强制 flush。
        self._flush_timer_task: asyncio.Task[None] | None = None
        # 熔断期间的事件积压队列，恢复后低频逐批同步。maxlen 防止内存无限增长。
        self._backlog: deque[dict] = deque(maxlen=self._BACKLOG_MAX_SIZE)
        # 恢复同步后台任务的引用，确保同一时刻只有一个 drain 在运行。
        self._drain_task: asyncio.Task[None] | None = None
        # AstrBot 版本在启动时一并上报，便于区分宿主版本差异带来的兼容性问题。
        self._astrbot_version_info = get_astrbot_version_info()
        self._astrbot_version = self._astrbot_version_info.version

        if self._enabled:
            logger.debug(
                f"[主动消息] 已启用匿名遥测喵 (Instance ID: {self._instance_id}, Version: {self._plugin_version})"
            )
        else:
            logger.debug("[主动消息] 遥测功能未启用喵。")

    @property
    def enabled(self) -> bool:
        """是否启用遥测。"""
        return self._enabled

    def _get_or_create_instance_id(self) -> str:
        """获取或创建实例 ID。"""
        try:
            # 使用 AstrBot 提供的插件数据目录，确保不同插件不会互相覆盖实例 ID 文件。
            data_dir = StarTools.get_data_dir(self._PLUGIN_NAME)
            id_file = data_dir / ".telemetry_id"
            if id_file.exists():
                # 若实例 ID 已存在则直接复用，保证同一安装实例的遥测身份长期稳定。
                instance_id = id_file.read_text(encoding="utf-8").strip()
                if instance_id:
                    return instance_id

            # 首次运行时生成新的 UUID，并持久化到插件数据目录。
            instance_id = str(uuid.uuid4())
            data_dir.mkdir(parents=True, exist_ok=True)
            id_file.write_text(instance_id, encoding="utf-8")
            logger.debug(f"[主动消息] 已生成新的遥测实例 ID 喵: {instance_id}")
            return instance_id
        except Exception as e:
            logger.warning(f"[主动消息] 无法持久化遥测实例 ID 喵: {e}")
            return str(uuid.uuid4())

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话。"""
        if self._session is None or self._session.closed:
            # 遥测必须是 best-effort，超时设短以免拖慢主业务事件循环。
            timeout = aiohttp.ClientTimeout(total=5)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def track(self, event_name: str, data: dict[str, Any] | None = None) -> bool:
        """发送遥测事件（startup/shutdown/heartbeat/config 等非 feature 类事件）。"""
        if not self._enabled:
            return False

        event = {
            "event": event_name,
            "data": data or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 熔断期间：非 feature 事件也存入 backlog，恢复后统一补发。
        now = time.monotonic()
        if now < self._rate_limited_until:
            self._backlog.append(event)
            return True

        return await self._send_batch([event])

    async def _send_batch(
        self, events: list[dict], *, requeue_on_failure: bool = True
    ) -> bool:
        """将一批事件打包发送到服务端。

        这是所有遥测发送的统一出口。支持单条或多条事件，服务端 ingest API
        原生支持 batch 数组结构，无需额外适配。

        返回 True 表示发送成功，False 表示失败（已触发熔断或网络异常）。
        当 requeue_on_failure=True 时，失败事件会被转入 backlog 等待恢复后补发；
        当 requeue_on_failure=False 时（drain 调用），由调用方自行处理失败事件。
        """
        if not events:
            return True

        now = time.monotonic()
        # 再次检查熔断状态（可能在 end_flow 调用时已经进入熔断）。
        if now < self._rate_limited_until:
            if requeue_on_failure:
                self._backlog.extend(events)
            return False

        payload = {
            "instance_id": self._instance_id,
            "version": self._plugin_version,
            "env": self._env,
            "batch": events,
        }

        try:
            session = await self._get_session()
            headers = {
                "Content-Type": "application/json",
                "X-App-Key": self._APP_KEY,
            }
            async with session.post(
                self._ENDPOINT,
                json=payload,
                headers=headers,
            ) as response:
                if response.status == 200:
                    logger.debug(
                        f"[主动消息] 遥测批次发送成功喵（{len(events)} 条事件）。"
                    )
                    # 发送成功后检查是否有积压需要启动低频同步。
                    self._maybe_start_drain()
                    return True
                if response.status == 401:
                    logger.warning("[主动消息] 遥测 App Key 无效或项目已禁用喵。")
                    return False
                if response.status == 429:
                    # 触发熔断，当前批次事件转入 backlog 等待恢复后补发。
                    self._rate_limited_until = now + self._RATE_LIMIT_COOLDOWN
                    if requeue_on_failure:
                        self._backlog.extend(events)
                    logger.warning(
                        f"[主动消息] 遥测请求频率超限喵，将静默 {self._RATE_LIMIT_COOLDOWN} 秒，"
                        f"{len(events)} 条事件已缓冲。"
                    )
                    return False
                logger.debug(f"[主动消息] 遥测事件发送失败喵: HTTP {response.status}")
                return False
        except asyncio.TimeoutError:
            logger.debug("[主动消息] 遥测请求超时喵。")
            return False
        except aiohttp.ClientConnectionError as e:
            # 连接失败触发短暂熔断（60秒），事件转入 backlog。
            self._rate_limited_until = now + 60
            if requeue_on_failure:
                self._backlog.extend(events)
            logger.debug(
                f"[主动消息] 遥测连接失败喵，静默 60 秒，{len(events)} 条事件已缓冲: {e}"
            )
            return False
        except aiohttp.ClientPayloadError as e:
            logger.debug(f"[主动消息] 遥测数据负载错误喵: {e}")
            return False
        except aiohttp.ClientError as e:
            logger.debug(f"[主动消息] 遥测网络错误喵: {e}")
            return False
        except Exception as e:
            logger.debug(f"[主动消息] 遥测未知错误喵: {e}")
            return False

    # ─── 弹性打包（feature 事件专用）────────────────────────────────────

    async def _flush_feature_buffer(self) -> None:
        """将 feature 缓冲区中的事件打包发送。

        由阈值触发（攒满 _FLUSH_THRESHOLD 条）或超时触发（_FLUSH_TIMEOUT 秒）调用。
        如果当前处于熔断状态，事件会整批转入 backlog 等待恢复后补发。

        注意：本方法只负责搬运并发送事件，不操作定时器。调用方需自行管理计时器生命周期。
        """
        if not self._feature_buffer:
            return
        events = self._feature_buffer
        self._feature_buffer = []
        await self._send_batch(events)

    async def _flush_timer_expired(self) -> None:
        """超时回调：_FLUSH_TIMEOUT 秒内未攒满阈值，强制 flush 当前 buffer。"""
        try:
            await asyncio.sleep(self._FLUSH_TIMEOUT)
            # 超时路径：先清除自身引用再 flush，避免 flush 内部尝试取消已完成的自身任务。
            self._flush_timer_task = None
            await self._flush_feature_buffer()
        except asyncio.CancelledError:
            pass

    def _cancel_flush_timer(self) -> None:
        """取消正在等待的超时 flush 任务。"""
        if self._flush_timer_task and not self._flush_timer_task.done():
            self._flush_timer_task.cancel()
            self._flush_timer_task = None

    def _ensure_flush_timer(self) -> None:
        """确保超时 flush 任务正在运行（首条事件入 buffer 时启动）。"""
        if self._flush_timer_task is None or self._flush_timer_task.done():
            self._flush_timer_task = asyncio.create_task(self._flush_timer_expired())

    # ─── 恢复同步（drain）──────────────────────────────────────────────

    def _maybe_start_drain(self) -> None:
        """检查是否有积压事件需要低频同步，若有则启动后台 drain 任务。

        仅在 _send_batch 成功后调用，确保网络已恢复可用。
        同一时刻只允许一个 drain 任务运行，避免并发发送再次触发限流。
        """
        if not self._backlog:
            return
        if self._drain_task is not None and not self._drain_task.done():
            return
        self._drain_task = asyncio.create_task(self._drain_backlog())

    async def _drain_backlog(self) -> None:
        """低频逐批发送积压事件。

        每次从 backlog 头部取出最多 _DRAIN_BATCH_SIZE 条事件打包发送，
        每批之间间隔 _DRAIN_INTERVAL 秒，避免恢复瞬间再次触发服务端限流。
        若发送失败（再次被限流），立即停止 drain 并把未发出的事件放回队列。
        """
        try:
            while self._backlog:
                batch: list[dict] = []
                for _ in range(min(self._DRAIN_BATCH_SIZE, len(self._backlog))):
                    batch.append(self._backlog.popleft())

                success = await self._send_batch(batch, requeue_on_failure=False)
                if not success:
                    # 发送失败，把事件放回 backlog 头部，停止本轮 drain。
                    for event in reversed(batch):
                        self._backlog.appendleft(event)
                    break

                # 低频间隔，给服务端喘息空间。
                await asyncio.sleep(self._DRAIN_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[主动消息] 遥测积压同步异常喵: {e}")
        finally:
            self._drain_task = None

    # ─── 公共上报接口 ─────────────────────────────────────────────────

    async def track_startup(self) -> bool:
        """上报启动事件。"""
        return await self.track(
            "startup",
            {
                "os": platform.system(),
                "os_version": platform.release(),
                "python_version": platform.python_version(),
                "arch": platform.machine(),
                "astrbot_version": self._astrbot_version,
                "astrbot_version_source": self._astrbot_version_info.source,
                "astrbot_version_error": self._astrbot_version_info.error,
            },
        )

    async def track_shutdown(
        self, exit_code: int = 0, runtime_seconds: float = 0
    ) -> bool:
        """上报停止事件。"""
        return await self.track(
            "shutdown",
            {
                "exit_code": exit_code,
                "runtime_seconds": runtime_seconds,
            },
        )

    async def track_heartbeat(self, uptime_seconds: float = 0) -> bool:
        """上报心跳事件。"""
        return await self.track(
            "heartbeat",
            {
                "uptime_seconds": uptime_seconds,
            },
        )

    async def track_feature(
        self, feature_name: str, extra: dict[str, Any] | None = None
    ) -> bool:
        """上报功能使用事件（弹性打包）。

        事件不会立即发送，而是追加到 _feature_buffer。满足以下任一条件时触发 flush：
        - buffer 中事件数达到 _FLUSH_THRESHOLD（5 条）→ 立即打包发送
        - 首条事件入 buffer 后超过 _FLUSH_TIMEOUT（15 秒）→ 超时强制发送

        熔断期间事件直接存入 backlog，不进入 buffer。
        """
        if not self._enabled:
            return False

        data = dict(extra or {})
        data["feature"] = feature_name
        event = {
            "event": "feature",
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 熔断期间：存入 backlog 等待恢复后补发，不进入 buffer。
        if time.monotonic() < self._rate_limited_until:
            self._backlog.append(event)
            return True

        # 追加到弹性缓冲区。
        self._feature_buffer.append(event)

        # 判断是否达到阈值：满了就立即 flush。
        if len(self._feature_buffer) >= self._FLUSH_THRESHOLD:
            self._cancel_flush_timer()
            await self._flush_feature_buffer()
        else:
            # 未满阈值：确保超时计时器在运行（首条事件时启动）。
            self._ensure_flush_timer()

        return True

    async def track_config(self, config: dict[str, Any]) -> bool:
        """上报配置快照，过滤敏感字段但保留统计型配置。"""
        if not self._enabled:
            return False

        try:
            # 深拷贝后再修改，避免遥测过滤逻辑意外污染插件运行中的真实配置对象。
            config_copy = copy.deepcopy(config)

            friend_settings = config_copy.get("friend_settings")
            if isinstance(friend_settings, dict):
                # 私聊会话列表不上传原文，只保留数量用于统计默认值设计是否合理。
                session_list = friend_settings.pop("session_list", [])
                friend_settings["session_list_count"] = (
                    len(session_list) if isinstance(session_list, list) else 0
                )
                # 私聊主动提示词正文属于高敏感内容，必须完全移除。
                if "proactive_prompt" in friend_settings:
                    del friend_settings["proactive_prompt"]

            group_settings = config_copy.get("group_settings")
            if isinstance(group_settings, dict):
                # 群聊会话列表同样只统计数量，不上传任何具体 UMO。
                session_list = group_settings.pop("session_list", [])
                group_settings["session_list_count"] = (
                    len(session_list) if isinstance(session_list, list) else 0
                )
                # 群聊提示词也做整段过滤，避免活跃策略被直接暴露。
                if "proactive_prompt" in group_settings:
                    del group_settings["proactive_prompt"]

            web_admin = config_copy.get("web_admin")
            if isinstance(web_admin, dict) and "password" in web_admin:
                # 管理端密码绝不进入遥测事件。
                del web_admin["password"]

            return await self.track("config", config_copy)
        except Exception as e:
            logger.debug(f"[主动消息] 配置快照提取失败喵: {e}")
            return False

    async def track_error(
        self, exception: Exception, module: str | None = None
    ) -> bool:
        """上报错误事件。"""
        # 错误消息与堆栈都必须先脱敏，再做截断，避免截断破坏正则匹配从而导致敏感内容漏网。
        raw_message = str(exception)
        sanitized_message = self._sanitize_message(raw_message)
        stack = "".join(
            traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )
        )
        sanitized_stack = self._sanitize_stack(stack)

        data = {
            "type": type(exception).__name__,
            "message": sanitized_message[:500],
            "module": module,
            "severity": "error",
            "stack": sanitized_stack[:4000],
        }
        return await self.track("error", data)

    def _sanitize_stack(self, stack: str) -> str:
        """脱敏堆栈与潜在敏感文本。"""
        # 先替换用户目录路径，避免把系统用户名或宿主机目录结构暴露出去。
        stack = re.sub(r"[A-Za-z]:\\Users\\[^\\]+\\", r"<USER_HOME>\\", stack)
        stack = re.sub(r"/(?:home|Users|root)/[^/]+/", r"<USER_HOME>/", stack)
        # 插件内部路径统一折叠成相对占位，保留定位意义但隐藏真实安装位置。
        stack = re.sub(r".*astrbot_plugin_proactive_chat[/\\]", r"<PLUGIN>/", stack)
        # site-packages 同样做路径折叠，避免把环境细节大量上传到遥测平台。
        stack = re.sub(r".*site-packages[/\\]", r"<SITE_PACKAGES>/", stack)
        # 对其他 Windows / Unix 绝对路径做保底折叠，减少非用户目录路径泄露。
        stack = re.sub(r"\b[A-Za-z]:\\[^\r\n'\"<>|]*", "<WINDOWS_PATH>", stack)
        stack = re.sub(r"(?<!<)/(?:[^\s/'\"]+/)+[^\s/'\"]+", "<UNIX_PATH>", stack)
        # 最后再复用消息脱敏规则，继续过滤 UMO、Prompt 和 key/value 等业务敏感信息。
        stack = self._sanitize_message(stack)
        return stack

    def _sanitize_message(self, message: str) -> str:
        """脱敏错误消息中的路径、UMO、Prompt 与密钥类字段。"""
        sanitized = message
        # 先过滤系统路径，防止异常字符串中直接包含宿主机用户名或完整目录。
        sanitized = re.sub(r"/(?:home|Users|root)/[^/\s]+/", r"<USER_HOME>/", sanitized)
        sanitized = re.sub(r"[A-Za-z]:\\Users\\[^\\\s]+\\", r"<USER_HOME>\\", sanitized)
        sanitized = re.sub(r"\b[A-Za-z]:\\[^\r\n'\"<>|]*", "<WINDOWS_PATH>", sanitized)
        sanitized = re.sub(
            r"(?<!<)/(?:[^\s/'\"]+/)+[^\s/'\"]+", "<UNIX_PATH>", sanitized
        )
        # 再过滤业务敏感内容：会话 UMO、密码/密钥类字段、提示词正文。
        sanitized = self._UMO_PATTERN.sub("<SESSION_UMO>", sanitized)
        sanitized = self._KEY_VALUE_PATTERN.sub(r"\1\2<FILTERED>", sanitized)
        sanitized = self._PROMPT_FIELD_PATTERN.sub(r"\1<FILTERED_PROMPT>", sanitized)
        return sanitized

    async def close(self) -> None:
        """关闭遥测会话，取消后台任务并尝试最后一次 flush 所有缓冲事件。"""
        # 取消弹性打包的超时计时器，避免 close 后仍有定时回调尝试发送。
        self._cancel_flush_timer()

        # 先取消正在运行的低频同步任务，避免 close 后仍有后台网络请求。
        if self._drain_task and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

        # 将 feature 弹性缓冲区中尚未发送的事件合并到 backlog，统一做最后一次 flush。
        if self._feature_buffer:
            self._backlog.extend(self._feature_buffer)
            self._feature_buffer = []

        # 尝试最后一次性发送 backlog 中的积压事件（best-effort，失败则丢弃）。
        if self._backlog and self._session and not self._session.closed:
            remaining = list(self._backlog)
            self._backlog.clear()
            try:
                await self._send_batch(remaining)
            except Exception:
                pass

        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            logger.debug("[主动消息] 遥测会话已关闭喵。")
