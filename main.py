# 文件名: main.py (位于 data/plugins/astrbot_plugin_proactive_chat/ 目录下)
# 版本: v1.1.5

# 导入标准库
import asyncio
import json
import math
import random
import re
import time
import traceback
import zoneinfo
from datetime import datetime

# 导入 aiofiles 及其异步os模块，用于非阻塞文件操作
import aiofiles
import aiofiles.os as aio_os

# 导入第三方库
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 导入 AstrBot 的核心 API 和组件
import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
# 导入官方定义的消息对象
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    UserMessageSegment,
)
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform import PlatformStatus
from astrbot.core.star.star_handler import EventType, star_handlers_registry

# 尝试兼容性导入 MessageSession，用于跨平台发送
try:
    from astrbot.core.platform.astr_message_event import MessageSession as MS
except ImportError:
    from astrbot.core.platform.message_session import MessageSession as MS

# --- 插件主类 ---


class ProactiveChatPlugin(star.Star):
    """
    插件的主类，继承自 astrbot.api.star.Star。
    负责插件的生命周期管理、事件监听和核心逻辑执行。
    """

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        """
        插件的构造函数。
        当 AstrBot 加载插件时被调用。
        config (AstrBotConfig): 插件专属的配置对象，由 AstrBot 框架通过依赖注入自动传入。
        """
        super().__init__(context)

        self.config: AstrBotConfig = config
        self.scheduler: AsyncIOScheduler | None = None
        self.timezone: zoneinfo.ZoneInfo | None = None

        # 使用 StarTools 获取插件专属数据目录，确保数据隔离
        self.data_dir = star.StarTools.get_data_dir("astrbot_plugin_proactive_chat")
        self.session_data_file = self.data_dir / "session_data.json"
        self.discovered_sessions_file = self.data_dir / "discovered_sessions.json"

        self.data_lock: asyncio.Lock | None = None
        self.session_data: dict[str, dict] = {}
        self.discovered_sessions: dict[str, dict] = {}

        # 用于群聊消息流监听的定时器字典
        # 键为会话ID，值为asyncio.TimerHandle对象，用于管理每个群聊的沉默倒计时
        # 当群聊中有人说话时，会重置对应的计时器；当计时器到期时，触发群聊的主动消息任务创建
        self.group_timers: dict[str, asyncio.TimerHandle] = {}

        # 记录最后一次检测到Bot消息的时间，用于时间窗口检测算法
        # 用于辅助在Bot发送消息时正确重置监听器的倒计时
        self.last_bot_message_time = 0

        # 使用会话隔离的状态管理，避免竞态条件
        # 将全局状态改为以session_id为键的字典，确保每个群聊会话状态独立
        self.session_temp_state: dict[str, dict] = {}  # 存储每个会话的临时状态

        # 自动主动消息功能的数据结构
        self.last_message_times: dict[str, float] = {}  # 记录每个会话的最后消息时间
        self.auto_trigger_timers: dict[str, asyncio.TimerHandle] = {}  # 自动触发计时器
        self.plugin_start_time = time.time()  # 插件启动时间

        # 用于控制相关日志只打印一次
        self.first_message_logged: set[str] = set()  # 记录已经打印过首次消息日志的会话

        # 用于清理过期会话状态的计数器
        self._cleanup_counter = 0

        logger.info("[主动消息] 插件实例已创建喵。")

    def _parse_session_id(self, session_id: str) -> tuple[str, str, str] | None:
        """
        解析会话ID，返回 (platform, message_type, target_id)。
        支持处理其他非标准字段等复杂情况（如 Satori 等平台的非标准格式）。
        """
        if not isinstance(session_id, str):
            return None

        # 1. 尝试基于标准消息类型进行分割
        # 常见的消息类型列表，可根据需要扩展
        known_types = [
            "FriendMessage",
            "GroupMessage",
            "PrivateMessage",
            "GuildMessage",
        ]

        for msg_type in known_types:
            # 查找类型标识符的位置，格式应该是 ...:MsgType:...
            search_pattern = f":{msg_type}:"
            idx = session_id.find(search_pattern)

            if idx != -1:
                # 找到了类型锚点
                platform = session_id[:idx]
                # 获取锚点之后的部分
                after_type = session_id[idx + len(search_pattern) :]

                # 处理 Satori 等多段式 ID: Platform:MsgType:SubType:TargetID
                # 必须保留 SubType 信息，MessageSession 才能正确路由喵！
                # 我们将 MsgType 之后的所有部分都视为 target_id
                return platform, msg_type, after_type

        # 2. 如果没找到标准类型，尝试简单的 3 段式解析
        parts = session_id.split(":")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]

        # 3. 尝试处理包含冒号的 platform 或复杂 ID
        # 这种情况下，我们假设最后一部分是 target_id，倒数第二部分是 type
        if len(parts) > 3:
            return ":".join(parts[:-2]), parts[-2], parts[-1]

        return None

    def _is_private_message_type(self, message_type: str) -> bool:
        return "Friend" in message_type or "Private" in message_type

    def _is_group_message_type(self, message_type: str) -> bool:
        return "Group" in message_type

    def _is_private_session(self, session_id: str) -> bool:
        parsed = self._parse_session_id(session_id)
        if not parsed:
            lower = session_id.lower()
            return "friendmessage" in lower or "private" in lower
        return self._is_private_message_type(parsed[1])

    def _is_group_session(self, session_id: str) -> bool:
        parsed = self._parse_session_id(session_id)
        if not parsed:
            return "group" in session_id.lower()
        return self._is_group_message_type(parsed[1])

    def _split_configured_session_target(
        self, configured_value: str, default_message_type: str
    ) -> tuple[str | None, str, str] | None:
        if not configured_value:
            return None

        configured_value = str(configured_value).strip()
        parsed = self._parse_session_id(configured_value)
        if parsed:
            platform, parsed_type, target_id = parsed
            return platform, parsed_type, target_id

        return None, default_message_type, configured_value

    def _is_configured_session_match(
        self,
        configured_value: str,
        runtime_session_id: str,
        runtime_target_id: str,
        expected_session_type: str,
    ) -> bool:
        if not configured_value:
            return False

        configured = self._split_configured_session_target(
            configured_value,
            "FriendMessage" if expected_session_type == "private" else "GroupMessage",
        )
        runtime = self._parse_session_id(runtime_session_id)

        if not configured:
            return False

        configured_platform, configured_type, configured_target = configured
        if runtime:
            runtime_platform, runtime_type, runtime_target = runtime
            if expected_session_type == "private" and not self._is_private_message_type(
                runtime_type
            ):
                return False
            if expected_session_type == "group" and not self._is_group_message_type(
                runtime_type
            ):
                return False

            if configured_platform and configured_platform != runtime_platform:
                return False

            if expected_session_type == "private" and not self._is_private_message_type(
                configured_type
            ):
                return False
            if expected_session_type == "group" and not self._is_group_message_type(
                configured_type
            ):
                return False

            return runtime_target == configured_target or runtime_target.endswith(
                f":{configured_target}"
            )

        return runtime_target_id == configured_target or runtime_target_id.endswith(
            f":{configured_target}"
        )

    def _resolve_configured_session_id(
        self, configured_value: str, default_message_type: str
    ) -> str | None:
        configured = self._split_configured_session_target(
            configured_value, default_message_type
        )
        if not configured:
            return None

        preferred_platform, message_type, target_id = configured
        if preferred_platform:
            return self._resolve_full_umo(target_id, message_type, preferred_platform)
        return self._resolve_full_umo(target_id, message_type)

    def _get_session_processing_key(
        self, configured_value: str, default_message_type: str
    ) -> str:
        resolved_session_id = self._resolve_configured_session_id(
            configured_value, default_message_type
        )
        if resolved_session_id:
            return resolved_session_id

        return f"{default_message_type}:{configured_value}"

    def _is_platform_allowed_for_discovery(
        self, platform_id: str, session_type: str
    ) -> bool:
        settings = self.config.get(f"{session_type}_settings", {})
        allowed_platform_ids = settings.get("allowed_platform_ids", []) or []
        if not allowed_platform_ids:
            return True
        return platform_id in {str(item).strip() for item in allowed_platform_ids if item}

    def _get_discovered_session_config(self, session_id: str) -> dict | None:
        discovered_info = self.discovered_sessions.get(session_id)
        if not isinstance(discovered_info, dict):
            return None

        session_type = discovered_info.get("session_type")
        if session_type not in {"private", "group"}:
            return None

        settings = self.config.get(f"{session_type}_settings", {})
        if not settings.get("enable", False):
            return None
        if not settings.get("allow_discovered_sessions", False):
            return None

        platform_id = discovered_info.get("platform_id", "")
        if platform_id and not self._is_platform_allowed_for_discovery(
            platform_id, session_type
        ):
            return None

        config_copy = settings.copy()
        config_copy["_session_type"] = session_type
        config_copy["_from_discovery"] = True
        config_copy["_session_name"] = discovered_info.get("target_name", "")
        return config_copy

    async def _record_discovered_session(self, event: AstrMessageEvent, session_id: str):
        lock = self.data_lock
        if lock is None:
            return

        parsed = self._parse_session_id(session_id)
        if not parsed:
            return

        platform_id, message_type, target_id = parsed
        if self._is_private_message_type(message_type):
            session_type = "private"
        elif self._is_group_message_type(message_type):
            session_type = "group"
        else:
            return

        if not self._is_platform_allowed_for_discovery(platform_id, session_type):
            return

        current_time = time.time()
        discovered_info = {
            "platform_id": platform_id,
            "message_type": message_type,
            "target_id": target_id,
            "session_type": session_type,
            "target_name": event.get_sender_name() or "",
            "self_id": event.get_self_id() or "",
            "last_seen_at": current_time,
        }

        should_save = False
        async with lock:
            existing = self.discovered_sessions.get(session_id)
            if not isinstance(existing, dict):
                self.discovered_sessions[session_id] = {
                    **discovered_info,
                    "discovered_at": current_time,
                }
                should_save = True
            else:
                existing.update({k: v for k, v in discovered_info.items() if v != ""})
                if "discovered_at" not in existing:
                    existing["discovered_at"] = current_time

        if should_save:
            async with lock:
                await self._save_discovered_sessions_internal()
            # 确保 Dashboard 会话管理列表中可见：若该 UMO 尚无对话记录，则创建一条
            try:
                conv_mgr = self.context.conversation_manager
                existing_conv_id = await conv_mgr.get_curr_conversation_id(session_id)
                if not existing_conv_id:
                    await conv_mgr.new_conversation(session_id)
                    logger.info(
                        f"[主动消息] 已为新发现会话 {session_id} 创建对话记录，Dashboard 可见喵。"
                    )
            except Exception as e:
                logger.debug(
                    f"[主动消息] 为发现会话创建对话记录时出错（不影响功能）: {e}"
                )

        # 无论是否首次发现，都同步更新 schema options，确保配置页 session_list 下拉可选
        self._refresh_schema_session_options()

    def _refresh_schema_session_options(self) -> None:
        """将已发现会话注入 config schema 的 session_list options，使配置页出现下拉选项。

        self.config.schema 是 AstrBot 内存中的 dict，直接修改即可对前端立即生效，
        无需修改磁盘文件或重启插件。
        """
        try:
            schema = self.config.schema
            if not isinstance(schema, dict):
                return

            private_ids = sorted(
                sid for sid, info in self.discovered_sessions.items()
                if isinstance(info, dict) and info.get("session_type") == "private"
            )
            group_ids = sorted(
                sid for sid, info in self.discovered_sessions.items()
                if isinstance(info, dict) and info.get("session_type") == "group"
            )

            for section_key, ids in (
                ("private_settings", private_ids),
                ("group_settings", group_ids),
            ):
                section = schema.get(section_key, {})
                items = section.get("items", {})
                sl = items.get("session_list")
                if isinstance(sl, dict):
                    sl["options"] = ids
        except Exception as e:
            logger.debug(f"[主动消息] 更新 schema options 失败（不影响功能）: {e}")

    def _get_discovered_sessions_sorted(
        self, session_type: str | None = None
    ) -> list[tuple[str, dict]]:
        entries: list[tuple[str, dict]] = []
        for session_id, info in self.discovered_sessions.items():
            if not isinstance(info, dict):
                continue
            if session_type and info.get("session_type") != session_type:
                continue
            entries.append((session_id, info))

        entries.sort(
            key=lambda item: item[1].get("last_seen_at", 0),
            reverse=True,
        )
        return entries

    def _is_session_in_global_list(self, session_id: str, session_type: str) -> bool:
        settings = self.config.get(f"{session_type}_settings", {})
        session_list = settings.get("session_list", []) or []
        return session_id in {str(item).strip() for item in session_list if item}

    def _get_discovered_session_summary(self, session_id: str, info: dict) -> str:
        session_type = info.get("session_type", "unknown")
        type_label = "私聊" if session_type == "private" else "群聊"
        target_name = str(info.get("target_name", "")).strip()
        target_id = str(info.get("target_id", "")).strip()
        platform_id = str(info.get("platform_id", "")).strip()

        name_part = target_name if target_name and target_name != "Unknown" else target_id
        if not name_part:
            name_part = session_id

        platform_part = f"{platform_id} 平台" if platform_id else "未知平台"
        return f"[{type_label}] {name_part} ({platform_part})"

    def _resolve_discovered_session_selector(self, selector: str) -> tuple[str, dict] | None:
        selector = selector.strip()
        if not selector:
            return None

        if selector in self.discovered_sessions and isinstance(
            self.discovered_sessions[selector], dict
        ):
            return selector, self.discovered_sessions[selector]

        if selector.isdigit():
            index = int(selector)
            discovered_entries = self._get_discovered_sessions_sorted()
            if 1 <= index <= len(discovered_entries):
                return discovered_entries[index - 1]

        return None


    async def _add_discovered_session_to_config(self, session_id: str) -> str:
        info = self.discovered_sessions.get(session_id)
        if not isinstance(info, dict):
            return f"未找到会话 `{session_id}` 喵。"

        session_type = info.get("session_type")
        if session_type not in {"private", "group"}:
            return f"会话 `{session_id}` 的类型无效，无法导入喵。"

        settings_key = f"{session_type}_settings"
        settings = self.config.get(settings_key, {})
        session_list = settings.get("session_list", []) or []
        normalized_list = [str(item).strip() for item in session_list if item]

        if session_id in normalized_list:
            return f"{self._get_discovered_session_summary(session_id, info)} 已经在会话列表里喵。"

        normalized_list.append(session_id)
        settings["session_list"] = normalized_list
        self.config[settings_key] = settings
        self.config.save_config()

        if settings.get("enable", False):
            await self._setup_auto_trigger_for_session_config(
                settings,
                str(info.get("message_type", "FriendMessage")),
                session_id,
                str(info.get("target_name", "")),
            )

        logger.info(f"[主动消息] 已通过命令将会话导入配置喵: {session_id}")
        return f"已将 {self._get_discovered_session_summary(session_id, info)} 导入到全局会话列表喵。"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("proactive_sessions")
    async def proactive_sessions(self, event: AstrMessageEvent) -> None:
        """查看自动发现的会话列表"""
        discovered_entries = self._get_discovered_sessions_sorted()
        if not discovered_entries:
            event.set_result(
                MessageEventResult()
                .message("当前还没有自动发现到任何会话喵。")
                .use_t2i(False)
            )
            return

        lines = ["已发现会话列表:\n"]
        for index, (session_id, info) in enumerate(discovered_entries, start=1):
            session_type = str(info.get("session_type", "unknown"))
            imported = self._is_session_in_global_list(session_id, session_type)
            last_seen_at = info.get("last_seen_at", 0)
            if isinstance(last_seen_at, (int, float)) and last_seen_at > 0:
                last_seen_text = datetime.fromtimestamp(last_seen_at).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            else:
                last_seen_text = "未知"

            status_text = "已导入" if imported else "未导入"
            lines.append(
                f"{index}. {self._get_discovered_session_summary(session_id, info)}\n"
                f"   会话ID: {session_id}\n"
                f"   状态: {status_text} | 最后活跃: {last_seen_text}\n"
            )

        lines.append(
            "\n使用 /proactive_add <序号或会话ID> 导入指定会话，"
            "或在目标会话中直接使用 /proactive_add_current 一键导入当前会话。"
        )
        event.set_result(
            MessageEventResult().message("\n".join(lines)).use_t2i(False)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("proactive_add")
    async def proactive_add(
        self, event: AstrMessageEvent, selector: str = ""
    ) -> None:
        """导入自动发现的会话到全局会话列表"""
        resolved = self._resolve_discovered_session_selector(selector)
        if not resolved:
            event.set_result(
                MessageEventResult()
                .message("用法: /proactive_add <序号或会话ID>")
                .use_t2i(False)
            )
            return

        session_id, _ = resolved
        result_text = await self._add_discovered_session_to_config(session_id)
        event.set_result(MessageEventResult().message(result_text).use_t2i(False))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("proactive_add_current")
    async def proactive_add_current(self, event: AstrMessageEvent) -> None:
        """导入当前会话到全局会话列表"""
        session_id = event.unified_msg_origin
        await self._record_discovered_session(event, session_id)

        parsed = self._parse_session_id(session_id)
        if not parsed:
            event.set_result(
                MessageEventResult()
                .message("当前会话ID无法解析，暂时不能导入喵。")
                .use_t2i(False)
            )
            return

        result_text = await self._add_discovered_session_to_config(session_id)
        event.set_result(MessageEventResult().message(result_text).use_t2i(False))

    def _get_session_log_str(
        self, session_id: str, session_config: dict | None = None
    ) -> str:
        """
        获取统一格式的会话日志字符串。
        格式：私聊/群聊 ID 备注名
        """
        parsed = self._parse_session_id(session_id)
        if parsed:
            _, msg_type, target_id = parsed

            type_str = "未知类型"
            if "Friend" in msg_type or "Private" in msg_type:
                type_str = "私聊"
            elif "Group" in msg_type:
                type_str = "群聊"

            # 获取备注名
            session_name = ""
            if session_config:
                session_name = session_config.get("_session_name", "")
            else:
                # 尝试获取配置
                config = self._get_session_config(session_id)
                if config:
                    session_name = config.get("_session_name", "")

            log_str = f"{type_str} {target_id}"
            if session_name:
                log_str += f" ({session_name})"

            return log_str

        # 如果解析失败，返回原始 session_id
        return session_id

    async def _cancel_all_related_auto_triggers(self, session_id: str):
        """
        取消与指定会话ID相关的所有可能的自动触发器。
        包括：
        1. 原始 session_id
        2. default 平台前缀的 ID (处理首次启动无数据的情况)
        3. 兼容 FriendMessage/GroupMessage 的 ID
        """
        # 1. 取消原始 ID
        await self._cancel_auto_trigger(session_id)

        parsed = self._parse_session_id(session_id)
        if not parsed:
            return

        platform, msg_type, target_id = parsed

        # 确定目标消息类型列表
        target_types = []
        if "Friend" in msg_type or "Private" in msg_type:
            target_types.append("FriendMessage")
        elif "Group" in msg_type:
            target_types.append("GroupMessage")

        for t in target_types:
            # 2. 尝试 {platform}:{t}:{target_id}
            id1 = f"{platform}:{t}:{target_id}"
            if id1 != session_id:
                await self._cancel_auto_trigger(id1)

            # 3. 尝试 default:{t}:{target_id}
            # 这是为了兼容 _setup_auto_trigger_for_session_config 中可能使用的 default 前缀
            if platform != "default":
                id2 = f"default:{t}:{target_id}"
                if id2 != session_id:
                    await self._cancel_auto_trigger(id2)

    async def _backup_configurations(self):
        """
        备份用户配置和提取Prompt，未雨绸缪喵。
        将所有重要数据持久化到插件数据目录。
        """
        try:
            # 确保数据目录存在
            await aio_os.makedirs(self.data_dir, exist_ok=True)

            # 1. 备份当前生效的完整配置 (self.config)
            # 这是用户通过 GUI 或配置文件实际设置的值，是最重要的资产
            config_backup_file = self.data_dir / "user_config_snapshot.json"
            async with aiofiles.open(config_backup_file, "w", encoding="utf-8") as f:
                content = json.dumps(self.config, indent=4, ensure_ascii=False)
                await f.write(content)

            # 2. 提取并汇总所有 Prompt 到 Markdown 文件，极大提升可读性
            prompts_content = ["# 🧠 主动消息 Prompt 汇总备份\n"]
            prompts_content.append(
                f"> 备份时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )

            # 提取辅助函数
            def _extract_prompt(title, settings):
                prompt = settings.get("proactive_prompt", "")
                if prompt:
                    prompts_content.append(f"## {title}\n")
                    prompts_content.append("```text")
                    prompts_content.append(prompt)
                    prompts_content.append("```\n")

            # 私聊全局
            private_settings = self.config.get("private_settings", {})
            _extract_prompt("私聊全局 Prompt", private_settings)

            # 群聊全局
            group_settings = self.config.get("group_settings", {})
            _extract_prompt("群聊全局 Prompt", group_settings)

            # 私聊个性化
            private_sessions = self.config.get("private_sessions", {})
            for key, session in private_sessions.items():
                if session.get("session_id") and session.get("enable"):
                    name = session.get("session_name", "未命名")
                    sid = session.get("session_id")
                    _extract_prompt(f"私聊会话 {key} ({sid} - {name})", session)

            # 群聊个性化
            group_sessions = self.config.get("group_sessions", {})
            for key, session in group_sessions.items():
                if session.get("session_id") and session.get("enable"):
                    name = session.get("session_name", "未命名")
                    sid = session.get("session_id")
                    _extract_prompt(f"群聊会话 {key} ({sid} - {name})", session)

            prompts_backup_file = self.data_dir / "prompts_collection.md"
            async with aiofiles.open(prompts_backup_file, "w", encoding="utf-8") as f:
                await f.write("\n".join(prompts_content))

            logger.info(
                f"[主动消息] 已将配置快照和 Prompt 汇总备份到数据目录喵: {self.data_dir}"
            )

        except Exception as e:
            logger.warning(f"[主动消息] 配置文件备份流程出错喵: {e}")

    # --- 数据持久化核心函数 ---

    async def _load_data_internal(self):
        """
        从文件中加载会话数据（异步无锁内部实现）。

        这是数据持久化的核心函数之一，负责：
        1. 检查会话数据文件是否存在
        2. 异步读取文件内容
        3. 解析JSON格式的会话数据
        4. 处理可能的异常情况（文件不存在、JSON解析错误等）

        注意：此函数必须在持有data_lock的情况下被调用，确保线程安全。
        使用asyncio.to_thread将同步的json.loads操作放入独立线程，避免阻塞事件循环。
        """
        if await aio_os.path.exists(self.session_data_file):
            try:
                async with aiofiles.open(self.session_data_file, encoding="utf-8") as f:
                    content = await f.read()
                    self.session_data = await asyncio.to_thread(json.loads, content)
            except (OSError, json.JSONDecodeError) as e:
                logger.error(
                    f"[主动消息] 加载会话数据失败喵: {e}，将使用空数据启动喵。"
                )
                self.session_data = {}
        else:
            self.session_data = {}

    async def _load_discovered_sessions_internal(self):
        if await aio_os.path.exists(self.discovered_sessions_file):
            try:
                async with aiofiles.open(
                    self.discovered_sessions_file, encoding="utf-8"
                ) as f:
                    content = await f.read()
                    loaded = await asyncio.to_thread(json.loads, content)
                    if isinstance(loaded, dict):
                        self.discovered_sessions = loaded
                    else:
                        self.discovered_sessions = {}
            except (OSError, json.JSONDecodeError) as e:
                logger.error(
                    f"[主动消息] 加载自动发现会话数据失败喵: {e}，将使用空数据启动喵。"
                )
                self.discovered_sessions = {}
        else:
            self.discovered_sessions = {}

    async def _save_data_internal(self):
        """
        将会话数据保存到文件（异步无锁内部实现）。

        这是数据持久化的核心函数之一，负责：
        1. 确保数据目录存在（如果不存在则创建）
        2. 异步打开会话数据文件
        3. 将session_data字典转换为JSON格式
        4. 异步写入文件内容
        5. 处理可能的IO异常

        使用ensure_ascii=False保证中文字符正常保存，indent=4提高可读性。
        此函数必须在持有data_lock的情况下被调用，确保数据一致性。
        """
        try:
            await aio_os.makedirs(self.data_dir, exist_ok=True)
            async with aiofiles.open(
                self.session_data_file, "w", encoding="utf-8"
            ) as f:
                content_to_write = await asyncio.to_thread(
                    json.dumps, self.session_data, indent=4, ensure_ascii=False
                )
                await f.write(content_to_write)
            await self._save_discovered_sessions_internal()
        except OSError as e:
            logger.error(f"[主动消息] 保存会话数据失败喵: {e}")

    async def _save_discovered_sessions_internal(self):
        try:
            await aio_os.makedirs(self.data_dir, exist_ok=True)
            async with aiofiles.open(
                self.discovered_sessions_file, "w", encoding="utf-8"
            ) as f:
                content_to_write = await asyncio.to_thread(
                    json.dumps,
                    self.discovered_sessions,
                    indent=4,
                    ensure_ascii=False,
                )
                await f.write(content_to_write)
        except OSError as e:
            logger.error(f"[主动消息] 保存自动发现会话数据失败喵: {e}")

    # --- 插件生命周期函数 ---

    async def initialize(self):
        """插件的异步初始化函数。"""
        self.data_lock = asyncio.Lock()

        # 备份用户配置和 Prompt，未雨绸缪喵
        await self._backup_configurations()

        # 添加配置验证
        try:
            await self._validate_config()
        except Exception as e:
            logger.warning(
                f"[主动消息] 配置验证发现问题喵: {e}，将继续使用默认设置喵。"
            )

        async with self.data_lock:
            await self._load_data_internal()
            await self._load_discovered_sessions_internal()
        self._refresh_schema_session_options()
        logger.info("[主动消息] 已成功从文件加载会话数据喵。")
        logger.info(
            f"[主动消息] 已加载 {len(self.discovered_sessions)} 个自动发现会话喵。"
        )
        # 从持久化数据中恢复最后消息时间
        # 注意：只恢复插件启动后的消息时间，插件启动前的历史消息不影响自动触发功能
        restored_count = 0
        for session_id, session_info in self.session_data.items():
            if isinstance(session_info, dict) and "last_message_time" in session_info:
                last_time = session_info["last_message_time"]
                if isinstance(last_time, (int, float)) and last_time > 0:
                    # 只恢复插件启动后的消息时间
                    if last_time >= self.plugin_start_time:
                        self.last_message_times[session_id] = last_time
                        restored_count += 1
                        logger.debug(
                            f"[主动消息] 已恢复 {self._get_session_log_str(session_id)} 在插件启动后的消息时间喵 -> {datetime.fromtimestamp(last_time)}"
                        )
                    else:
                        logger.debug(
                            f"[主动消息] 忽略插件启动前的历史消息时间用于自动主动消息任务喵: {self._get_session_log_str(session_id)} -> {datetime.fromtimestamp(last_time)}"
                        )

        if restored_count > 0:
            logger.info(
                f"[主动消息] 已从持久化数据恢复 {restored_count} 个会话在插件启动后的消息时间喵。"
            )

        try:
            self.timezone = zoneinfo.ZoneInfo(self.context.get_config().get("timezone"))
        except (zoneinfo.ZoneInfoNotFoundError, TypeError, KeyError, ValueError) as e:
            logger.warning(
                f"[主动消息] 时区配置无效或未配置喵 ({e})，将使用服务器系统时区作为备用喵。"
            )
            self.timezone = None

        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        assert self.scheduler is not None
        self.scheduler.start()

        await self._init_jobs_from_data()
        logger.info("[主动消息] 调度器已初始化喵。")

        # 为启用的会话设置自动主动消息触发器
        await self._setup_auto_triggers_for_enabled_sessions()
        logger.info("[主动消息] 自动主动消息触发器初始化完成喵。")

    async def terminate(self):
        """插件被卸载或停用时调用的清理函数。"""
        # 终止所有正在运行的 asyncio 计时器，防止插件停用后依然有逻辑在运转
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
        logger.info(f"[主动消息] 已取消 {timer_count} 个正在运行的群聊沉默计时器喵。")

        # 清理自动触发计时器，防止插件停用后仍有自动触发任务运行
        auto_trigger_count = len(self.auto_trigger_timers)
        for session_id, timer in list(
            self.auto_trigger_timers.items()
        ):  # 使用list避免字典在迭代时修改
            try:
                timer.cancel()
                logger.debug(
                    f"[主动消息] 已取消 {self._get_session_log_str(session_id)} 的自动触发计时器喵。"
                )
            except Exception as e:
                logger.warning(f"[主动消息] 取消自动触发计时器时出错喵: {e}")

        self.auto_trigger_timers.clear()
        logger.info(f"[主动消息] 已取消 {auto_trigger_count} 个自动触发计时器喵。")

        # 清理调度器中的所有任务
        if self.scheduler and self.scheduler.running:
            try:
                # 获取所有任务并移除
                jobs = self.scheduler.get_jobs()
                for job in jobs:
                    try:
                        self.scheduler.remove_job(job.id)
                        logger.debug(f"[主动消息] 已移除调度器任务喵: {job.id}")
                    except Exception as e:
                        logger.warning(f"[主动消息] 移除调度器任务时出错喵: {e}")

                self.scheduler.shutdown()
                logger.info("[主动消息] 调度器已关闭喵。")
            except Exception as e:
                logger.error(f"[主动消息] 关闭调度器时出错喵: {e}")

        # 保存数据
        lock = self.data_lock
        if lock:
            try:
                async with lock:
                    await self._save_data_internal()
                logger.info("[主动消息] 会话数据已保存喵。")
            except Exception as e:
                logger.error(f"[主动消息] 保存数据时出错喵: {e}")

        logger.info("[主动消息] 主动消息插件已终止喵。")

    async def _validate_config(self):
        """验证插件配置的完整性和有效性"""
        try:
            private_settings = self.config.get("private_settings", {})
            group_settings = self.config.get("group_settings", {})

            # 验证私聊配置 - 检查新的多会话架构
            if private_settings.get("enable", False):
                # 检查个性化配置槽位
                private_sessions = self.config.get("private_sessions", {})
                has_personal_config = False
                for session_key in [
                    "session_1",
                    "session_2",
                    "session_3",
                    "session_4",
                    "session_5",
                ]:
                    session_config = private_sessions.get(session_key, {})
                    if session_config.get("enable", False) and session_config.get(
                        "session_id"
                    ):
                        has_personal_config = True
                        break

                # 检查全局session_list
                session_list = private_settings.get("session_list", [])
                allow_discovered_sessions = private_settings.get(
                    "allow_discovered_sessions", False
                )

                if (
                    not has_personal_config
                    and not session_list
                    and not allow_discovered_sessions
                ):
                    logger.warning(
                        "[主动消息] 私聊主动消息已启用但未配置任何会话喵（既无个性化配置也无session_list）。"
                    )

                schedule_settings = private_settings.get("schedule_settings", {})
                min_interval = schedule_settings.get("min_interval_minutes", 30)
                max_interval = schedule_settings.get("max_interval_minutes", 900)

                if min_interval > max_interval:
                    logger.warning(
                        "[主动消息] 私聊配置中最小间隔大于最大间隔喵，将自动调整喵。"
                    )

            # 验证群聊配置 - 检查新的多会话架构
            if group_settings.get("enable", False):
                # 检查个性化配置槽位
                group_sessions = self.config.get("group_sessions", {})
                has_personal_config = False
                for session_key in [
                    "session_1",
                    "session_2",
                    "session_3",
                    "session_4",
                    "session_5",
                ]:
                    session_config = group_sessions.get(session_key, {})
                    if session_config.get("enable", False) and session_config.get(
                        "session_id"
                    ):
                        has_personal_config = True
                        break

                # 检查全局session_list
                session_list = group_settings.get("session_list", [])
                allow_discovered_sessions = group_settings.get(
                    "allow_discovered_sessions", False
                )

                if (
                    not has_personal_config
                    and not session_list
                    and not allow_discovered_sessions
                ):
                    logger.warning(
                        "[主动消息] 群聊主动消息已启用但未配置任何会话喵（既无个性化配置也无session_list）。"
                    )

            logger.info("[主动消息] 配置验证完成喵。")

        except Exception as e:
            logger.error(f"[主动消息] 配置验证过程出错喵: {e}")
            raise

    async def _setup_auto_trigger(self, session_id: str, silent: bool = False):
        """
        为指定会话设置自动主动消息触发器（核心实现）。

        这是自动主动消息功能的核心方法，负责：
        1. 检查会话是否启用了自动触发功能
        2. 设置自动触发计时器
        3. 当计时器到期时，创建主动消息任务（不是直接发送消息）

        参数：
        - session_id: 目标会话ID
        - silent: 是否静默执行（不打印日志）

        注意：这个功能只在插件启动后的一段时间内有效，一旦收到消息就会取消自动触发。
        """
        session_config = self._get_session_config(session_id)
        if not session_config:
            return

        auto_trigger_settings = session_config.get("auto_trigger_settings", {})
        scheduler = self.scheduler
        if scheduler is None:
            logger.warning("[主动消息] 调度器尚未初始化，无法设置自动主动消息触发器喵。")
            return

        # 检查是否启用了自动触发功能
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

        # 取消现有的自动触发计时器
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

        # 定义自动触发回调函数 - 修复闭包变量捕获问题
        def _auto_trigger_callback(captured_session_id=session_id):
            try:
                # 检查是否仍然需要自动触发（避免重复触发）
                if captured_session_id not in self.auto_trigger_timers:
                    logger.debug(
                        f"[主动消息] {self._get_session_log_str(captured_session_id)} 的自动触发已被取消，跳过喵。"
                    )
                    return

                # 检查配置是否仍然有效
                current_config = self._get_session_config(captured_session_id)
                if not current_config or not current_config.get("enable", False):
                    logger.info(
                        f"[主动消息] {self._get_session_log_str(captured_session_id, current_config)} 的配置已禁用，取消自动触发喵。"
                    )
                    return

                # 检查是否已经有活动（收到过消息）
                last_message_time = self.last_message_times.get(captured_session_id, 0)
                current_time = time.time()
                time_since_plugin_start = current_time - self.plugin_start_time

                # 只检查插件启动后是否收到消息
                # 插件启动前的历史消息不影响自动触发功能
                if last_message_time == 0 and time_since_plugin_start >= (
                    auto_trigger_minutes * 60
                ):
                    # 重要：创建任务而不是直接发送消息，但避免持久化
                    # 自动触发的任务不应该被持久化，避免与正常任务冲突
                    try:
                        current_session_config = self._get_session_config(
                            captured_session_id
                        )
                        if not current_session_config:
                            logger.warning(
                                f"[主动消息] 无法获取会话 {self._get_session_log_str(captured_session_id)} 的配置，取消自动触发喵"
                            )
                            return

                        schedule_conf = current_session_config.get(
                            "schedule_settings", {}
                        )
                        min_interval = (
                            int(schedule_conf.get("min_interval_minutes", 30)) * 60
                        )
                        max_interval = max(
                            min_interval,
                            int(schedule_conf.get("max_interval_minutes", 900)) * 60,
                        )
                        random_interval = random.randint(min_interval, max_interval)
                        next_trigger_time = time.time() + random_interval
                        run_date = datetime.fromtimestamp(
                            next_trigger_time, tz=self.timezone
                        )

                        # 直接添加到调度器，但不保存到session_data
                        scheduler.add_job(
                            self.check_and_chat,
                            "date",
                            run_date=run_date,
                            args=[captured_session_id],
                            id=captured_session_id,
                            replace_existing=True,
                            misfire_grace_time=60,
                        )

                        # 合并日志：一行包含所有关键信息
                        logger.info(
                            f"[主动消息] {self._get_session_log_str(captured_session_id, current_session_config)} 满足条件，自动触发任务已创建喵！执行时间 (非持久化): {run_date.strftime('%Y-%m-%d %H:%M:%S')} 喵"
                        )

                    except Exception as e:
                        logger.error(f"[主动消息] 自动触发任务创建失败喵: {e}")

                    # 清理自动触发计时器（只触发一次）
                    if captured_session_id in self.auto_trigger_timers:
                        del self.auto_trigger_timers[captured_session_id]

            except Exception as e:
                logger.error(f"[主动消息] 自动触发回调函数执行失败喵: {e}")

        # 设置自动触发计时器
        try:
            loop = asyncio.get_running_loop()
            # 转换分钟为秒，设置延迟调用
            delay_seconds = auto_trigger_minutes * 60

            self.auto_trigger_timers[session_id] = loop.call_later(
                delay_seconds, _auto_trigger_callback
            )

            # 根据silent参数决定是否打印日志，避免重复
            if not silent:
                logger.info(
                    f"[主动消息] 已为 {self._get_session_log_str(session_id, session_config)} 设置自动主动消息触发器喵，"
                    f"将在 {auto_trigger_minutes} 分钟后检查是否需要自动触发喵。"
                )

        except Exception as e:
            logger.error(f"[主动消息] 设置自动触发计时器失败喵: {e}")

    async def _cancel_auto_trigger(self, session_id: str):
        """
        取消指定会话的自动主动消息触发器。
        当收到消息时调用，确保不会重复触发。
        """
        if session_id in self.auto_trigger_timers:
            try:
                self.auto_trigger_timers[session_id].cancel()
                logger.info(
                    f"[主动消息] 已取消 {self._get_session_log_str(session_id)} 的自动触发计时器喵。"
                )
            except Exception as e:
                logger.warning(f"[主动消息] 取消自动触发计时器时出错喵: {e}")
            finally:
                del self.auto_trigger_timers[session_id]

    def _cleanup_invalid_session_data(self):
        """
        清理无效的会话数据，包括：
        1. 删除通用格式的会话ID（如 private_message:xxx, group_message:xxx）
        2. 这些是由早期版本的自动触发功能错误创建的

        返回清理的条目数量
        """
        cleaned_count = 0
        invalid_sessions = []

        for session_id in list(self.session_data.keys()):
            # 检查是否是通用格式的错误会话ID
            if session_id.startswith("private_message:") or session_id.startswith(
                "group_message:"
            ):
                invalid_sessions.append(session_id)
                cleaned_count += 1

        # 删除无效的会话数据
        for session_id in invalid_sessions:
            del self.session_data[session_id]
            logger.info(
                f"[主动消息] 清理了无效的会话数据: {self._get_session_log_str(session_id)}"
            )

        return cleaned_count

    async def _setup_auto_triggers_for_enabled_sessions(self):
        """
        为所有启用了自动触发功能的会话设置自动主动消息触发器。
        在插件初始化时调用。
        """
        logger.info("[主动消息] 开始检查并设置自动主动消息触发器喵...")

        auto_trigger_count = 0
        processed_sessions = set()  # 记录已处理的会话，避免重复

        # 1. 检查私聊个性化配置槽位（优先级最高）
        private_sessions = self.config.get("private_sessions", {})
        for session_key in [
            "session_1",
            "session_2",
            "session_3",
            "session_4",
            "session_5",
        ]:
            session_config = private_sessions.get(session_key, {})
            if session_config.get("session_id"):
                target_id = session_config["session_id"]
                processing_key = self._get_session_processing_key(
                    target_id, "FriendMessage"
                )
                # 无论是否启用，只要在个性化配置中存在，就标记为已处理
                # 防止回退到全局配置
                if processing_key not in processed_sessions:
                    processed_sessions.add(processing_key)
                    if session_config.get("enable", False):
                        session_name = session_config.get("session_name", "")
                        auto_trigger_count += (
                            await self._setup_auto_trigger_for_session_config(
                                session_config, "FriendMessage", target_id, session_name
                            )
                        )

        # 2. 检查群聊个性化配置槽位（优先级最高）
        group_sessions = self.config.get("group_sessions", {})
        for session_key in [
            "session_1",
            "session_2",
            "session_3",
            "session_4",
            "session_5",
        ]:
            session_config = group_sessions.get(session_key, {})
            if session_config.get("session_id"):
                target_id = session_config["session_id"]
                processing_key = self._get_session_processing_key(
                    target_id, "GroupMessage"
                )
                # 无论是否启用，只要在个性化配置中存在，就标记为已处理
                if processing_key not in processed_sessions:
                    processed_sessions.add(processing_key)
                    if session_config.get("enable", False):
                        session_name = session_config.get("session_name", "")
                        auto_trigger_count += (
                            await self._setup_auto_trigger_for_session_config(
                                session_config, "GroupMessage", target_id, session_name
                            )
                        )

        # 3. 检查全局配置的session_list（只处理未在个性化配置中的会话）
        private_settings = self.config.get("private_settings", {})
        session_list = private_settings.get("session_list", [])
        if private_settings.get("enable", False) and session_list:
            for target_id in session_list:
                processing_key = self._get_session_processing_key(
                    target_id, "FriendMessage"
                )
                if processing_key not in processed_sessions:
                    # 尝试从个性化配置中获取备注名，如果没有就为空
                    session_name = ""
                    for session_key in [
                        "session_1",
                        "session_2",
                        "session_3",
                        "session_4",
                        "session_5",
                    ]:
                        session_config = private_sessions.get(session_key, {})
                        if session_config.get("session_id") == target_id:
                            session_name = session_config.get("session_name", "")
                            break

                    auto_trigger_count += (
                        await self._setup_auto_trigger_for_session_config(
                            private_settings, "FriendMessage", target_id, session_name
                        )
                    )
                    processed_sessions.add(processing_key)

        group_settings = self.config.get("group_settings", {})
        session_list = group_settings.get("session_list", [])
        if group_settings.get("enable", False) and session_list:
            for target_id in session_list:
                processing_key = self._get_session_processing_key(
                    target_id, "GroupMessage"
                )
                if processing_key not in processed_sessions:
                    # 尝试从个性化配置中获取备注名，如果没有就为空
                    session_name = ""
                    for session_key in [
                        "session_1",
                        "session_2",
                        "session_3",
                        "session_4",
                        "session_5",
                    ]:
                        session_config = group_sessions.get(session_key, {})
                        if session_config.get("session_id") == target_id:
                            session_name = session_config.get("session_name", "")
                            break

                    auto_trigger_count += (
                        await self._setup_auto_trigger_for_session_config(
                            group_settings, "GroupMessage", target_id, session_name
                        )
                    )
                    processed_sessions.add(processing_key)

        for discovered_session_id, discovered_info in self.discovered_sessions.items():
            if discovered_session_id in processed_sessions:
                continue

            discovered_config = self._get_discovered_session_config(discovered_session_id)
            if not discovered_config:
                continue

            if discovered_info.get("session_type") == "private":
                auto_trigger_count += (
                    await self._setup_auto_trigger_for_session_config(
                        discovered_config,
                        "FriendMessage",
                        discovered_session_id,
                        discovered_info.get("target_name", ""),
                    )
                )
                processed_sessions.add(discovered_session_id)
            elif discovered_info.get("session_type") == "group":
                auto_trigger_count += (
                    await self._setup_auto_trigger_for_session_config(
                        discovered_config,
                        "GroupMessage",
                        discovered_session_id,
                        discovered_info.get("target_name", ""),
                    )
                )
                processed_sessions.add(discovered_session_id)

        # 4. 检查全局配置的默认设置（没有session_list的情况）
        if private_settings.get("enable", False) and not private_settings.get(
            "session_list", []
        ):
            logger.warning(
                "[主动消息] 私聊全局配置已启用但未配置session_list，无法设置自动触发器喵。"
            )

        if group_settings.get("enable", False) and not group_settings.get(
            "session_list", []
        ):
            logger.debug("[主动消息] 群聊全局配置已启用但未配置session_list喵。")

        if auto_trigger_count == 0:
            # 更精确地分析为什么没有设置触发器
            has_auto_trigger_config = False

            # 检查私聊个性化配置
            private_sessions = self.config.get("private_sessions", {})
            for session_key in [
                "session_1",
                "session_2",
                "session_3",
                "session_4",
                "session_5",
            ]:
                session_config = private_sessions.get(session_key, {})
                if session_config.get("auto_trigger_settings", {}).get(
                    "enable_auto_trigger", False
                ):
                    has_auto_trigger_config = True
                    break

            # 检查群聊个性化配置
            if not has_auto_trigger_config:
                group_sessions = self.config.get("group_sessions", {})
                for session_key in [
                    "session_1",
                    "session_2",
                    "session_3",
                    "session_4",
                    "session_5",
                ]:
                    session_config = group_sessions.get(session_key, {})
                    if session_config.get("auto_trigger_settings", {}).get(
                        "enable_auto_trigger", False
                    ):
                        has_auto_trigger_config = True
                        break

            # 检查全局配置
            if not has_auto_trigger_config:
                private_settings = self.config.get("private_settings", {})
                group_settings = self.config.get("group_settings", {})
                if private_settings.get("auto_trigger_settings", {}).get(
                    "enable_auto_trigger", False
                ) or group_settings.get("auto_trigger_settings", {}).get(
                    "enable_auto_trigger", False
                ):
                    has_auto_trigger_config = True

            if has_auto_trigger_config:
                logger.info(
                    "[主动消息] 检测到自动主动消息配置，但会话ID无效或未配置，无法设置触发器喵。"
                )
            else:
                logger.info("[主动消息] 没有会话启用自动主动消息功能喵。")
        else:
            logger.info(
                f"[主动消息] 已为 {auto_trigger_count} 个会话设置自动主动消息触发器喵。"
            )

    async def _setup_auto_trigger_for_session_config(
        self, settings: dict, message_type: str, target_id: str, session_name: str = ""
    ) -> int:
        """
        为指定会话配置设置自动触发器（支持多会话架构）。

        参数：
        - settings: 会话配置设置
        - message_type: 消息类型（FriendMessage 或 GroupMessage）
        - target_id: 目标ID（用户QQ号或群号）
        - session_name: 会话备注名，用于日志显示

        返回：设置的触发器数量
        """
        type_description = "私聊" if message_type == "FriendMessage" else "群聊"

        # 统一日志格式
        log_str = f"{type_description} {target_id}"
        if session_name and session_name.strip():
            log_str += f" ({session_name})"

        auto_trigger_settings = settings.get("auto_trigger_settings", {})
        if not auto_trigger_settings.get("enable_auto_trigger", False):
            logger.debug(f"[主动消息] {log_str} 未启用自动主动消息功能喵。")
            return 0

        resolved_session_id = self._resolve_configured_session_id(target_id, message_type)
        if not resolved_session_id:
            logger.warning(f"[主动消息] {log_str} 无法解析为有效会话，跳过自动触发器设置喵。")
            return 0

        # 检查是否已经有持久化的主动消息任务
        has_existing_task = False
        current_time = time.time()
        # 使用 list() 创建副本以避免并发迭代时字典大小改变的风险
        for session_id, session_info in list(self.session_data.items()):
            next_trigger = session_info.get("next_trigger_time")
            if isinstance(next_trigger, (int, float)) and session_id == resolved_session_id:
                trigger_time_with_grace = next_trigger + 60
                is_not_expired = current_time < trigger_time_with_grace

                if is_not_expired:
                    logger.debug(f"[主动消息] {log_str} 找到有效的持久化任务喵。")
                    has_existing_task = True
                    break

        if has_existing_task:
            logger.info(
                f"[主动消息] {log_str} 已存在持久化的主动消息任务喵，"
                f"跳过自动触发器设置以避免冲突喵。"
            )
            return 0

        session_id = resolved_session_id
        logger.debug(f"[主动消息] 正在为 {log_str} 设置自动触发器喵。")
        # 在外层函数统一打印完整的日志信息，内层函数静默执行，避免重复
        auto_trigger_minutes = auto_trigger_settings.get(
            "auto_trigger_after_minutes", 5
        )
        logger.info(
            f"[主动消息] 已为 {log_str} 设置自动触发器喵，"
            f"将在 {auto_trigger_minutes} 分钟后检查是否需要自动触发喵。"
        )
        await self._setup_auto_trigger(session_id, silent=True)
        return 1

    def _resolve_full_umo(
        self, target_id: str, msg_type: str, preferred_platform: str | None = None
    ) -> str:
        """
        动态解析并验证存活的 UMO 喵。
        灵灵会优先确保返回的平台 ID 对应的实例当前处于运行状态。
        """
        type_keyword = (
            "Friend" if "Friend" in msg_type or "Private" in msg_type else "Group"
        )
        # 获取所有活跃且不是 WebChat 的平台实例映射喵
        active_insts = {
            p.meta().id: p
            for p in self.context.platform_manager.get_insts()
            if p.meta().id and "webchat" not in p.meta().id.lower()
        }

        # 0. 优先使用首选平台（如果指定且运行中）
        if (
            preferred_platform
            and preferred_platform in active_insts
            and active_insts[preferred_platform].status == PlatformStatus.RUNNING
        ):
            return f"{preferred_platform}:{msg_type}:{target_id}"

        # 1. 优先从历史记录匹配，但必须验证平台依然“运行中”喵
        for existing_id in self.session_data.keys():
            if type_keyword in existing_id and existing_id.endswith(f":{target_id}"):
                p_id = existing_id.split(":")[0]
                if (
                    p_id in active_insts
                    and active_insts[p_id].status == PlatformStatus.RUNNING
                ):
                    return existing_id

        for existing_id in self.discovered_sessions.keys():
            if type_keyword in existing_id and existing_id.endswith(f":{target_id}"):
                p_id = existing_id.split(":")[0]
                if (
                    p_id in active_insts
                    and active_insts[p_id].status == PlatformStatus.RUNNING
                ):
                    return existing_id

        # 2. 如果历史记录失效，从当前正在运行且健康的物理平台中推选喵
        running_platforms = [
            p for p in active_insts.values() if p.status == PlatformStatus.RUNNING
        ]
        if running_platforms:
            return f"{running_platforms[0].meta().id}:{msg_type}:{target_id}"

        # 3. 最后的保底措施喵
        fallback_p_id = list(active_insts.keys())[0] if active_insts else "default"
        return f"{fallback_p_id}:{msg_type}:{target_id}"

    def _get_session_config(self, session_id: str) -> dict | None:
        """
        根据会话ID(session_id)获取对应的会话配置。

        支持多会话配置，先检查个性化配置，再检查全局配置。

        配置优先级：
        1. 个性化会话配置（私聊/群聊槽位）
        2. 全局配置中的session_list匹配
        3. 全局配置的默认设置

        返回值：配置字典（如果找到且启用）或None（如果未找到或禁用）
        """
        parsed = self._parse_session_id(session_id)
        if not parsed:
            return None

        # 使用 _ 忽略 platform，因为此处只需要 message_type 和 target_id
        _, message_type, target_id = parsed

        # 根据消息类型分别处理，使用模糊匹配以支持不同平台的命名喵
        if "Friend" in message_type or "Private" in message_type:
            config = self._get_private_session_config(session_id, target_id)
            if config:
                return config
            return self._get_discovered_session_config(session_id)
        elif "Group" in message_type:
            config = self._get_group_session_config(session_id, target_id)
            if config:
                return config
            return self._get_discovered_session_config(session_id)

        return None

    def _get_private_session_config(
        self, session_id: str, target_id: str
    ) -> dict | None:
        """获取私聊会话配置"""
        # 1. 检查个性化配置槽位
        private_sessions = self.config.get("private_sessions", {})
        for session_key in [
            "session_1",
            "session_2",
            "session_3",
            "session_4",
            "session_5",
        ]:
            session_config = private_sessions.get(session_key, {})
            # 兼容多段式 ID：只要配置的 ID 是目标 ID 的一部分（通常是末尾）喵
            config_id = str(session_config.get("session_id", ""))
            if config_id and self._is_configured_session_match(
                config_id, session_id, target_id, "private"
            ):
                if session_config.get("enable", False):
                    # 添加会话名称到配置中，用于日志显示
                    config_copy = session_config.copy()
                    config_copy["_session_name"] = session_config.get(
                        "session_name", ""
                    )
                    config_copy["_session_type"] = "private"
                    return config_copy
                else:
                    # 如果个性化配置存在但未启用，则视为明确禁用，不回退到全局配置
                    return None

        # 2. 检查全局配置的session_list（严格模式：必须明确在list中）
        private_settings = self.config.get("private_settings", {})
        if not private_settings.get("enable", False):
            return None

        session_list = private_settings.get("session_list", [])
        for configured_value in session_list:
            if self._is_configured_session_match(
                str(configured_value), session_id, target_id, "private"
            ):
                config_copy = private_settings.copy()
                config_copy["_session_type"] = "private"
                config_copy["_from_session_list"] = True
                return config_copy

        return None

    def _get_group_session_config(self, session_id: str, target_id: str) -> dict | None:
        """获取群聊会话配置"""
        # 1. 检查个性化配置槽位
        group_sessions = self.config.get("group_sessions", {})
        for session_key in [
            "session_1",
            "session_2",
            "session_3",
            "session_4",
            "session_5",
        ]:
            session_config = group_sessions.get(session_key, {})
            # 兼容多段式 ID：只要配置的 ID 是目标 ID 的一部分（通常是末尾）喵
            config_id = str(session_config.get("session_id", ""))
            if config_id and self._is_configured_session_match(
                config_id, session_id, target_id, "group"
            ):
                if session_config.get("enable", False):
                    # 添加会话名称到配置中，用于日志显示
                    config_copy = session_config.copy()
                    config_copy["_session_name"] = session_config.get(
                        "session_name", ""
                    )
                    config_copy["_session_type"] = "group"
                    return config_copy
                else:
                    # 如果个性化配置存在但未启用，则视为明确禁用，不回退到全局配置
                    return None

        # 2. 检查全局配置的session_list（严格模式：必须明确在list中）
        group_settings = self.config.get("group_settings", {})
        if not group_settings.get("enable", False):
            return None

        session_list = group_settings.get("session_list", [])
        for configured_value in session_list:
            if self._is_configured_session_match(
                str(configured_value), session_id, target_id, "group"
            ):
                config_copy = group_settings.copy()
                config_copy["_session_type"] = "group"
                config_copy["_from_session_list"] = True
                return config_copy

        return None

    # --- 核心调度逻辑 ---

    async def _init_jobs_from_data(self):
        """
        从已加载的 session_data 中恢复定时任务。

        这是插件重启恢复机制的核心函数，负责：
        1. 遍历所有保存的会话数据
        2. 检查每个会话的配置是否有效且启用
        3. 验证定时任务是否过期（给予1分钟宽限期）
        4. 恢复未过期的定时任务到调度器
        5. 记录恢复统计信息

        特别处理：区分私聊和群聊的不同恢复逻辑
        - 私聊：使用APScheduler定时任务
        - 群聊：使用沉默倒计时机制（不在这里恢复）

        这个函数确保了插件重启后能够无缝继续之前的工作状态。
        """
        restored_count = 0
        current_time = time.time()
        scheduler = self.scheduler
        lock = self.data_lock
        if scheduler is None or lock is None:
            return

        # 增强调试信息
        logger.info(
            f"[主动消息] 开始从数据恢复定时任务喵，当前时间: {datetime.fromtimestamp(current_time)}"
        )
        # 首先清理无效的会话数据
        cleaned_count = self._cleanup_invalid_session_data()
        if cleaned_count > 0:
            logger.info(f"[主动消息] 清理了 {cleaned_count} 个无效的会话数据条目喵。")
            # 立即保存清理后的数据
            async with lock:
                await self._save_data_internal()

        logger.info(f"[主动消息] 会话数据条目数: {len(self.session_data)}")

        for session_id, session_info in self.session_data.items():
            # 检查此会话是否有对应的配置
            session_config = self._get_session_config(session_id)

            if not session_config or not session_config.get("enable", False):
                continue

            next_trigger = session_info.get("next_trigger_time")

            # 修正任务恢复逻辑，避免过早清理数据导致无法恢复
            if isinstance(next_trigger, (int, float)):
                # 检查任务是否过期（给1分钟的宽限期）
                trigger_time_with_grace = next_trigger + 60
                is_not_expired = current_time < trigger_time_with_grace

                if is_not_expired:
                    try:
                        run_date = datetime.fromtimestamp(
                            next_trigger, tz=self.timezone
                        )

                        # 检查是否已存在相同任务，避免重复
                        existing_job = scheduler.get_job(session_id)
                        if existing_job:
                            logger.debug(
                                f"[主动消息] {self._get_session_log_str(session_id, session_config)} 的任务已存在，跳过恢复喵。"
                            )
                            continue

                        scheduler.add_job(
                            self.check_and_chat,
                            "date",
                            run_date=run_date,
                            args=[session_id],
                            id=session_id,
                            replace_existing=True,
                            misfire_grace_time=60,
                        )
                        logger.info(
                            f"[主动消息] 已成功从文件恢复任务喵: {self._get_session_log_str(session_id, session_config)}, 执行时间: {run_date} 喵"
                        )
                        restored_count += 1
                    except Exception as e:
                        logger.error(
                            f"[主动消息] 添加 {self._get_session_log_str(session_id, session_config)} 的恢复任务到调度器时失败喵: {e}"
                        )
                else:
                    # 任务已过期，记录日志但不清理数据
                    logger.info(
                        f"[主动消息] {self._get_session_log_str(session_id, session_config)} 的任务已过期，跳过恢复喵。"
                    )
                    logger.debug(
                        f"[主动消息] 触发时间: {datetime.fromtimestamp(next_trigger)} 喵"
                    )
                    logger.debug(
                        f"[主动消息] 当前时间: {datetime.fromtimestamp(current_time)} 喵"
                    )
                    logger.debug("[主动消息] 宽限期: 60秒喵")
                    # 不要清理数据，让正常流程处理过期的任务
            else:
                logger.debug(
                    f"[主动消息] {self._get_session_log_str(session_id, session_config)} 没有next_trigger_time，跳过喵"
                )

        logger.info(
            f"[主动消息] 任务恢复检查完成，共恢复 {restored_count} 个定时任务喵。"
        )
        if restored_count == 0:
            logger.info("[主动消息] 没有需要恢复的定时任务喵。")

    async def _schedule_next_chat_and_save(
        self, session_id: str, reset_counter: bool = False
    ):
        """
        安排下一次主动聊天并立即将状态持久化到文件。

        这是调度逻辑的核心函数，负责：
        1. 根据配置计算下一次触发时间（在最小和最大间隔之间随机选择）
        2. 创建新的定时任务并添加到调度器
        3. 更新会话数据中的触发时间
        4. 立即保存数据到文件（确保重启后任务不丢失）
        5. 处理计数器重置（当用户回复时）

        参数：
        - session_id: 目标会话ID
        - reset_counter: 是否重置未回复计数器（用户回复时设为True）

        这个函数是"原子操作"，确保调度决策能够持久化保存。
        """
        session_config = self._get_session_config(session_id)
        if not session_config:
            return
        scheduler = self.scheduler
        lock = self.data_lock
        if scheduler is None or lock is None:
            return

        schedule_conf = session_config.get("schedule_settings", {})

        async with lock:
            if reset_counter:
                self.session_data.setdefault(session_id, {})["unanswered_count"] = 0

            min_interval = int(schedule_conf.get("min_interval_minutes", 30)) * 60
            max_interval = max(
                min_interval, int(schedule_conf.get("max_interval_minutes", 900)) * 60
            )
            random_interval = random.randint(min_interval, max_interval)
            next_trigger_time = time.time() + random_interval
            run_date = datetime.fromtimestamp(next_trigger_time, tz=self.timezone)

            scheduler.add_job(
                self.check_and_chat,
                "date",
                run_date=run_date,
                args=[session_id],
                id=session_id,
                replace_existing=True,
                misfire_grace_time=60,
            )

            self.session_data.setdefault(session_id, {})["next_trigger_time"] = (
                next_trigger_time
            )
            logger.info(
                f"[主动消息] 已为 {self._get_session_log_str(session_id, session_config)} 安排下一次主动消息喵，时间：{run_date.strftime('%Y-%m-%d %H:%M:%S')} 喵。"
            )

            await self._save_data_internal()

    # --- 事件监听 ---

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=999)
    async def on_private_message(self, event: AstrMessageEvent):
        """监听私聊消息，取消旧任务，并重置计时器和计数器。"""
        # 检查整个消息链，以正确响应图片等富媒体消息
        if not event.get_messages():
            return

        session_id = event.unified_msg_origin
        await self._record_discovered_session(event, session_id)
        lock = self.data_lock
        scheduler = self.scheduler
        if lock is None:
            return

        # 缓存 self_id
        if event.get_self_id():
            async with lock:
                self.session_data.setdefault(session_id, {})["self_id"] = (
                    event.get_self_id()
                )

        # 记录消息时间并取消自动触发
        # 只记录插件启动后的消息时间，用于自动触发功能
        current_time = time.time()
        self.last_message_times[session_id] = current_time

        # 持久化插件启动后的消息时间，确保插件重载后能恢复状态
        async with lock:
            # 只保存插件启动后的消息时间
            if current_time >= self.plugin_start_time:
                self.session_data.setdefault(session_id, {})["last_message_time"] = (
                    current_time
                )

        # 尝试取消自动触发 - 支持多种会话ID格式
        await self._cancel_all_related_auto_triggers(session_id)

        # 只打印一次日志，避免刷屏，且只针对配置的会话
        session_config = self._get_session_config(session_id)
        if session_config and session_config.get("enable", False):
            if session_id not in self.first_message_logged:
                self.first_message_logged.add(session_id)
                logger.info(
                    f"[主动消息] 已记录 {self._get_session_log_str(session_id, session_config)} 的消息时间并取消自动触发喵。"
                )

        session_config = self._get_session_config(session_id)
        if not session_config or not session_config.get("enable", False):
            logger.debug(
                f"[主动消息] {self._get_session_log_str(session_id, session_config)} 未启用或配置无效，跳过处理喵。"
            )
            return

        # 在重新调度前，先尝试取消任何已存在的、由 APScheduler 设置的定时任务
        if scheduler is not None:
            try:
                scheduler.remove_job(session_id)
                logger.info(
                    f"[主动消息] 用户已回复喵，已取消 {self._get_session_log_str(session_id, session_config)} 的主动消息任务喵。"
                )
            except Exception:  # JobLookupError
                pass  # 如果任务不存在，说明是正常情况，无需处理

        # 重要：只重置当前会话的计数器，不影响其他会话
        logger.info(
            f"[主动消息] 重置 {self._get_session_log_str(session_id, session_config)} 的未回复计数器为0喵。"
        )
        await self._schedule_next_chat_and_save(session_id, reset_counter=True)

    # 群聊消息监听与智能触发
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=998)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群聊消息流，重置沉默倒计时，并取消已计划的主动消息任务。"""
        # 响应所有类型的消息，而不仅仅是文本消息
        if not event.get_messages():
            return

        session_id = event.unified_msg_origin
        await self._record_discovered_session(event, session_id)
        lock = self.data_lock
        scheduler = self.scheduler
        if lock is None:
            return

        # 缓存 self_id
        if event.get_self_id():
            async with lock:
                self.session_data.setdefault(session_id, {})["self_id"] = (
                    event.get_self_id()
                )

        # 使用会话隔离的状态管理，避免竞态条件
        current_time = time.time()
        self.session_temp_state[session_id] = {"last_user_time": current_time}
        logger.debug(
            f"[主动消息] 记录 {self._get_session_log_str(session_id)} 的消息时间戳喵: {current_time}"
        )

        # 记录消息时间并取消自动触发
        self.last_message_times[session_id] = current_time

        # 只持久化插件启动后的消息时间，用于自动触发功能
        async with lock:
            if current_time >= self.plugin_start_time:
                self.session_data.setdefault(session_id, {})["last_message_time"] = (
                    current_time
                )
                logger.debug(
                    f"[主动消息] 已记录插件启动后 {self._get_session_log_str(session_id)} 的消息时间喵 -> {current_time}"
                )
            else:
                logger.debug(
                    f"[主动消息] 忽略插件启动前 {self._get_session_log_str(session_id)} 的旧消息用于自动主动消息任务喵 -> {current_time}"
                )

        await self._cancel_all_related_auto_triggers(session_id)

        # 只打印一次日志，避免刷屏，且只针对配置的会话
        session_config = self._get_session_config(session_id)
        if session_config and session_config.get("enable", False):
            if session_id not in self.first_message_logged:
                self.first_message_logged.add(session_id)
                logger.info(
                    f"[主动消息] 已记录 {self._get_session_log_str(session_id, session_config)} 的消息时间并取消自动触发喵。"
                )

        # Bot消息检测已迁移到after_message_sent事件
        # 旧的on_group_message中的Bot检测逻辑已被移除，避免重复检测
        # 现在通过on_after_message_sent专门处理Bot发送的消息
        sender_id = None
        try:
            # 只获取发送者ID用于日志记录，不再进行Bot检测
            if hasattr(event, "message_obj") and event.message_obj:
                if hasattr(event.message_obj, "sender") and event.message_obj.sender:
                    sender_id = getattr(
                        event.message_obj.sender, "id", None
                    ) or getattr(event.message_obj.sender, "user_id", None)

            if not sender_id:
                sender_id = getattr(event, "user_id", None) or getattr(
                    event, "sender_id", None
                )

        except Exception as e:
            logger.debug(f"[主动消息] 获取发送者ID失败喵: {e}")

        session_config = self._get_session_config(session_id)
        if not session_config or not session_config.get("enable", False):
            logger.debug(
                f"[主动消息] {self._get_session_log_str(session_id, session_config)} 未启用或配置无效，跳过处理喵。"
            )
            return

        # 群聊活跃时取消已预定的 APScheduler 任务
        if scheduler is not None:
            try:
                scheduler.remove_job(session_id)
                logger.info(
                    f"[主动消息] 群聊活跃喵，已取消 {self._get_session_log_str(session_id, session_config)} 的主动消息任务喵。"
                )
            except Exception as e:  # JobLookupError
                logger.debug(
                    f"[主动消息] {self._get_session_log_str(session_id, session_config)} 没有待取消的调度任务喵: {e}"
                )

        # 无论是用户消息还是Bot消息，都应该重置沉默倒计时和未回复计数器
        await self._reset_group_silence_timer(session_id)

        # 每个会话(私聊/群聊)有独立的session_id和数据，不会相互影响
        async with lock:
            if session_id in self.session_data:
                current_unanswered = self.session_data[session_id].get(
                    "unanswered_count", 0
                )
                self.session_data[session_id]["unanswered_count"] = 0
                if current_unanswered > 0:
                    logger.debug(
                        f"[主动消息] {self._get_session_log_str(session_id, session_config)} 的用户已回复， 未回复计数器已重置喵。"
                    )

                # 清理已作废的定时任务数据，避免重复恢复
                # 重要：只清理群聊的定时任务数据，因为群聊使用沉默倒计时机制
                # 私聊使用APScheduler，不应该在这里清理
                if self._is_group_session(session_id) and "next_trigger_time" in self.session_data[session_id]:
                    del self.session_data[session_id]["next_trigger_time"]

    # --- Bot消息检测 ---

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """
        监听消息发送后事件，识别Bot自己发送的消息，用于重置群聊沉默倒计时。
        """
        session_id = event.unified_msg_origin

        # 只关注群聊消息
        if not self._is_group_session(session_id):
            return

        current_time = time.time()

        # 定期清理过期的会话状态，防止内存泄漏
        # 每10次调用清理一次
        self._cleanup_counter += 1

        if self._cleanup_counter % 10 == 0:
            self._cleanup_expired_session_states(current_time)

        try:
            # 重置沉默倒计时
            await self._reset_group_silence_timer(session_id)
            # 清理会话状态
            if session_id in self.session_temp_state:
                del self.session_temp_state[session_id]

        except Exception as e:
            logger.error(
                f"[主动消息] {self._get_session_log_str(session_id)} 的 after_message_sent 处理异常喵: {e}"
            )

    async def _reset_group_silence_timer(self, session_id: str):
        """
        重置指定群聊的"沉默倒计时"。
        这是群聊主动消息机制的核心函数，工作原理：
        1. 取消该群聊现有的沉默计时器（如果有）
        2. 根据配置创建新的计时器，设置指定的沉默时间
        3. 当计时器到期时，会触发主动消息任务创建

        无论是用户发言还是Bot自己发言，都会调用此函数重置倒计时，
        确保只有在群聊真正沉默时才发送主动消息。
        """
        session_config = self._get_session_config(session_id)
        if not session_config or not session_config.get("enable", False):
            return

        # 取消上一个为该群聊设置的"沉默倒计时"
        if session_id in self.group_timers:
            try:
                self.group_timers[session_id].cancel()
            except Exception as e:
                logger.warning(
                    f"[主动消息] 取消 {self._get_session_log_str(session_id, session_config)} 的旧计时器时出错喵: {e}"
                )
            finally:
                del self.group_timers[session_id]

        # 获取沉默触发时间
        idle_minutes = session_config.get("group_idle_trigger_minutes", 10)

        # 定义倒计时结束后的回调函数 - 修复闭包变量捕获问题
        def _schedule_callback(captured_session_id=session_id):
            try:
                # 在创建任务前，验证群聊是否仍然处于沉默状态，并检查群聊是否仍然值得发送主动消息

                # 检查1: 验证当前是否还有活跃的计时器（如果群聊活跃，计时器应该被重置）
                if captured_session_id not in self.group_timers:
                    return

                # 检查2: 验证会话数据是否存在，如果不存在则创建初始数据
                if captured_session_id not in self.session_data:
                    logger.info(
                        f"[主动消息] {self._get_session_log_str(captured_session_id)} 的会话数据不存在，创建初始会话数据喵。"
                    )
                    # 为新会话创建初始数据
                    self.session_data[captured_session_id] = {"unanswered_count": 0}

                # 检查3: 验证配置是否仍然启用
                current_config = self._get_session_config(captured_session_id)
                if not current_config or not current_config.get("enable", False):
                    logger.info(
                        f"[主动消息] {self._get_session_log_str(captured_session_id, current_config)} 的配置已禁用或不存在，跳过主动消息创建喵。"
                    )
                    return

                # 当群聊沉默时，不应该重置计数器。reset_counter 必须为 False。
                # 这个回调是在主事件循环中被调用的，所以我们可以安全地创建异步任务
                # 获取当前的未回复次数，用于显示更准确的日志
                current_unanswered = self.session_data.get(captured_session_id, {}).get(
                    "unanswered_count", 0
                )
                asyncio.create_task(
                    self._schedule_next_chat_and_save(
                        captured_session_id, reset_counter=False
                    )
                )
                logger.info(
                    f"[主动消息] {self._get_session_log_str(captured_session_id, current_config)} 已沉默 {idle_minutes} 分钟，开始计划主动消息喵。(当前未回复次数: {current_unanswered})"
                )
            except Exception as e:
                logger.error(f"[主动消息] 沉默倒计时回调函数执行失败喵: {e}")

        # 设置一个新的"沉默倒计时"
        try:
            loop = asyncio.get_running_loop()
            self.group_timers[session_id] = loop.call_later(
                idle_minutes * 60, _schedule_callback
            )
        except Exception as e:
            logger.error(f"[主动消息] 设置沉默倒计时失败喵: {e}")

    async def _is_chat_allowed(self, session_id: str) -> bool:
        """
        检查是否允许进行主动聊天（条件检查）。

        这是主动消息发送前的必要检查，包括：
        1. 检查插件是否在该会话中启用
        2. 检查当前时间是否在免打扰时段内

        返回值：
        - True: 允许进行主动聊天
        - False: 不允许进行主动聊天（插件禁用或免打扰时段）

        这个函数是纯查询函数，不包含任何副作用。
        重新调度的逻辑由调用方根据返回值决定。
        """
        session_config = self._get_session_config(session_id)
        if not session_config or not session_config.get("enable", False):
            return False

        schedule_conf = session_config.get("schedule_settings", {})
        if is_quiet_time(schedule_conf.get("quiet_hours", "1-7"), self.timezone):
            logger.info("[主动消息] 当前为免打扰时段喵。")
            return False

        return True

    def _sanitize_history_content(self, history: list) -> list:
        """
        清洗历史消息内容，确保所有内容均为纯文本字符串喵。
        这可以防止某些 LLM 适配器在处理复杂对象（如 MessageSegment）时出现 JS 风格的 [object Object] 错误喵。
        """
        sanitized_history = []
        for msg in history:
            # 如果消息不是字典，尝试转换为字典或跳过
            if hasattr(msg, "to_dict"):
                msg_dict = msg.to_dict()
            elif isinstance(msg, dict):
                msg_dict = msg.copy()
            else:
                # 无法处理的格式，记录警告并跳过
                logger.debug(
                    f"[主动消息] 历史记录中发现无法识别的消息格式: {type(msg)}，已跳过喵。"
                )
                continue

            content = msg_dict.get("content")

            # 处理列表形式的内容（AstrBot 的多媒体消息格式）
            if isinstance(content, list):
                text_content = ""
                for segment in content:
                    if isinstance(segment, dict):
                        if segment.get("type") == "text":
                            text_content += segment.get("text", "")
                        # 目前只保留文本片段，忽略图片等其他片段以确保兼容性喵
                    elif hasattr(segment, "text"):  # 处理 TextPart 等对象
                        text_content += getattr(segment, "text", "")
                    elif hasattr(segment, "get_text"):
                        text_content += segment.get_text()
                    elif isinstance(segment, str):
                        text_content += segment
                msg_dict["content"] = text_content
            elif not isinstance(content, str):
                # 强制转换为字符串，防止 [object Object] 泄露
                msg_dict["content"] = str(content) if content is not None else ""

            sanitized_history.append(msg_dict)
        return sanitized_history

    async def _prepare_llm_request(self, session_id: str) -> dict | None:
        """
        准备 LLM 请求所需的上下文、人格和最终 Prompt。

        基于最新文档优化，简化人格和上下文获取逻辑。

        返回值：
        - dict: 包含conv_id、history、system_prompt的请求包
        - None: 如果准备失败
        """
        try:
            # 获取当前会话的对话ID
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(
                session_id
            )

            # 如果是新会话，主动创建对话
            if not conv_id:
                logger.info(
                    f"[主动消息] {self._get_session_log_str(session_id)} 是新会话，尝试创建新对话喵。"
                )
                try:
                    conv_id = await self.context.conversation_manager.new_conversation(
                        session_id
                    )
                    logger.info(f"[主动消息] 新对话创建成功喵，ID: {conv_id}")
                except ValueError:
                    # 让 ValueError 冒泡，以便外层 check_and_chat 可以捕获并进行非标准 ID 的兼容性处理
                    raise
                except Exception as e:
                    logger.error(f"[主动消息] 创建新对话失败喵: {e}", exc_info=True)
                    return None

            if not conv_id:
                logger.warning(
                    f"[主动消息] 无法获取或创建 {self._get_session_log_str(session_id)} 的对话ID，跳过本次任务喵。"
                )
                return None

            # 获取对话对象
            conversation = await self.context.conversation_manager.get_conversation(
                session_id, conv_id
            )

            # 获取对话历史（简化处理，直接使用字符串历史）
            pure_history_messages = []
            if conversation and conversation.history:
                try:
                    # 尝试解析JSON格式的历史记录
                    if isinstance(conversation.history, str):
                        pure_history_messages = await asyncio.to_thread(
                            json.loads, conversation.history
                        )
                    else:
                        pure_history_messages = conversation.history
                except (json.JSONDecodeError, TypeError):
                    logger.warning("[主动消息] 解析历史记录失败，使用空历史喵。")

            # 获取人格设定（使用新的人格管理器API）
            original_system_prompt = ""

            # 优先使用会话绑定的persona
            if conversation and conversation.persona_id:
                persona = await self.context.persona_manager.get_persona(
                    conversation.persona_id
                )
                if persona:
                    original_system_prompt = persona.system_prompt
                    logger.info(
                        f"[主动消息] 使用会话人格: '{conversation.persona_id}' 喵"
                    )

            # 如果没有会话persona，使用默认persona
            if not original_system_prompt:
                default_persona = (
                    await self.context.persona_manager.get_default_persona_v3(
                        umo=session_id
                    )
                )
                if default_persona:
                    original_system_prompt = default_persona["prompt"]
                    logger.info("[主动消息] 使用默认人格设定喵")

            if not original_system_prompt:
                logger.error(
                    "[主动消息] 呜喵？！关键错误喵：无法加载任何人格设定，放弃喵。"
                )
                return None

            logger.info(
                f"[主动消息] 成功加载上下文喵: 共 {len(pure_history_messages)} 条历史消息喵。"
            )

            return {
                "conv_id": conv_id,
                "history": pure_history_messages,
                "system_prompt": original_system_prompt,
            }

        except Exception as e:
            logger.warning(f"[主动消息] 获取上下文或人格失败喵: {e}")
            return None

    def _split_text(self, text: str, settings: dict) -> list[str]:
        """
        根据配置对文本进行分段。
        参考 AstrBot 核心 pipeline/result_decorate/stage.py 实现。
        """
        split_mode = settings.get("split_mode", "regex")

        if split_mode == "words":
            split_words = settings.get("split_words", ["。", "？", "！", "~", "…"])
            if not split_words:
                return [text]

            escaped_words = sorted(
                [re.escape(word) for word in split_words], key=len, reverse=True
            )
            pattern = re.compile(f"(.*?({'|'.join(escaped_words)})|.+$)", re.DOTALL)

            segments = pattern.findall(text)
            result = []
            for seg in segments:
                if isinstance(seg, tuple):
                    content = seg[0]
                    if not isinstance(content, str):
                        continue
                    # 移除末尾的分隔符（如果需要的话，这里保持原样，模仿AstrBot行为可能是保留标点）
                    # AstrBot逻辑似乎保留标点
                    if content.strip():
                        result.append(content)
                elif seg and seg.strip():
                    result.append(seg)
            return result if result else [text]

        else:  # regex 模式
            # 默认正则添加 \n 支持，以支持纯换行分隔的文本
            regex_pattern = settings.get("regex", r".*?[。？！~…\n]+|.+$")
            try:
                split_response = re.findall(
                    regex_pattern,
                    text,
                    re.DOTALL | re.MULTILINE,
                )
            except re.error:
                logger.error(
                    f"[主动消息] 分段回复正则表达式错误，使用默认分段方式: {traceback.format_exc()}"
                )
                split_response = re.findall(
                    r".*?[。？！~…\n]+|.+$",
                    text,
                    re.DOTALL | re.MULTILINE,
                )

            # 过滤空字符串
            return [seg for seg in split_response if seg.strip()]

    async def _calc_interval(self, text: str, settings: dict) -> float:
        """
        计算分段回复的间隔时间。
        参考 AstrBot 核心 pipeline/respond/stage.py 实现。
        """
        interval_method = settings.get("interval_method", "random")

        if interval_method == "log":
            log_base = float(settings.get("log_base", 1.8))
            # 简单的字数统计
            if all(ord(c) < 128 for c in text):
                word_count = len(text.split())
            else:
                word_count = len([c for c in text if c.isalnum()])

            i = math.log(word_count + 1, log_base)
            return random.uniform(i, i + 0.5)

        # random
        interval_str = settings.get("interval", "1.5, 3.5")
        try:
            interval_ls = [float(t) for t in interval_str.replace(" ", "").split(",")]
            if len(interval_ls) != 2:
                interval = [1.5, 3.5]
            else:
                interval = interval_ls
        except Exception:
            interval = [1.5, 3.5]

        return random.uniform(interval[0], interval[1])

    async def _trigger_decorating_hooks(self, session_id: str, chain: list) -> list:
        """
        触发 OnDecoratingResultEvent 钩子，让其他插件（如文本转语音、尾巴等）有机会处理消息。
        构造一个伪造的 AstrMessageEvent 来模拟消息处理流程。
        """
        # 1. 解析 session_id
        parsed = self._parse_session_id(session_id)
        if not parsed:
            return chain

        platform_name, msg_type_str, target_id = parsed

        # 2. 获取 Platform 实例
        platform_inst = None
        for p in self.context.platform_manager.platform_insts:
            if p.meta().id == platform_name:
                platform_inst = p
                break

        if not platform_inst:
            for p in self.context.platform_manager.platform_insts:
                if p.meta().name == platform_name:
                    platform_inst = p
                    break

        if not platform_inst:
            return chain

        # 3. 构造 AstrBotMessage
        message_obj = AstrBotMessage()
        if "Friend" in msg_type_str:
            message_obj.type = MessageType.FRIEND_MESSAGE
        elif "Group" in msg_type_str:
            message_obj.type = MessageType.GROUP_MESSAGE
            message_obj.group = Group(group_id=target_id)
        else:
            message_obj.type = MessageType.FRIEND_MESSAGE

        message_obj.session_id = target_id
        message_obj.message = chain
        # 尝试获取已知的 self_id，如果获取不到则使用占位符
        # 这里使用 session_data 中缓存的 self_id (如果在 on_message 中保存了)
        # 或者使用默认值
        message_obj.self_id = self.session_data.get(session_id, {}).get(
            "self_id", "bot"
        )

        # 将发送者设为目标用户，模拟是用户发来的消息触发了回复（虽然这是主动消息）
        # 这样有助于一些依赖 sender 信息的装饰器正常工作
        message_obj.sender = MessageMember(user_id=target_id)

        # 补全其他可能被访问的属性，防止 AttributeError
        message_obj.message_str = ""
        message_obj.raw_message = None
        message_obj.message_id = ""

        # 4. 构造 AstrMessageEvent
        event = AstrMessageEvent(
            message_str="",
            message_obj=message_obj,
            platform_meta=platform_inst.meta(),
            session_id=target_id,
        )

        # 5. 设置 Result
        res = MessageEventResult()
        res.chain = chain
        event.set_result(res)

        # 6. 触发 Hooks
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.OnDecoratingResultEvent
        )
        for handler in handlers:
            try:
                logger.debug(
                    f"[主动消息] 正在执行装饰钩子: {handler.handler_full_name} ({handler.handler_module_path}) 喵"
                )
                await handler.handler(event)
            except Exception as e:
                # 记录详细的错误来源和类型，精准定位 ApiNotAvailable 喵！
                error_type = type(e).__name__
                logger.error(
                    f"[主动消息] 执行装饰钩子失败喵！来源: {handler.handler_full_name}, "
                    f"错误类型: {error_type}, 错误详情: {e}"
                )
                # 这是一个极其显眼的标记喵
                if "Available" in error_type:
                    logger.error(
                        f"[主动消息] 抓到可能导致 ApiNotAvailable 的嫌疑人喵！模块: {handler.handler_module_path}"
                    )

        # 7. 返回结果
        # 必须尊重 event.get_result()，如果装饰器清空了 chain（意图拦截），我们应该返回空列表
        res = event.get_result()
        if res is not None:
            return res.chain if res.chain is not None else []
        return chain

    async def _send_chain_with_hooks(self, session_id: str, components: list):
        """
        发送消息链喵。
        灵灵会严格通过指定的平台发送消息，不再擅自寻找替补喵。
        """
        processed_chain_list = await self._trigger_decorating_hooks(
            session_id, components
        )
        if not processed_chain_list:
            return

        chain = MessageChain(processed_chain_list)
        parsed = self._parse_session_id(session_id)
        if not parsed:
            # 这种情况下没有平台信息，只能通过核心 API 盲发喵
            await self.context.send_message(session_id, chain)
            return

        p_id, m_type_str, t_id = parsed
        m_type = (
            MessageType.GROUP_MESSAGE
            if "Group" in m_type_str
            else MessageType.FRIEND_MESSAGE
        )

        # 灵灵现在会精准定位指定的平台喵！
        platforms = self.context.platform_manager.get_insts()
        target_platform = next((p for p in platforms if p.meta().id == p_id), None)

        if not target_platform:
            logger.warning(
                f"[主动消息] 找不到指定的平台 {p_id} 喵，尝试使用核心 API 兜底喵。"
            )
            await self.context.send_message(session_id, chain)
            return

        if target_platform.status != PlatformStatus.RUNNING:
            logger.warning(f"[主动消息] 平台 {p_id} 未运行喵，跳过主动消息喵。")
            return

        try:
            session_obj = MS(platform_name=p_id, message_type=m_type, session_id=t_id)
            await target_platform.send_by_session(session_obj, chain)
            logger.debug(f"[主动消息] 消息将通过平台 {p_id} 送达喵")
        except Exception as e:
            logger.error(f"[主动消息] 通过平台 {p_id} 发送失败喵: {e}")
            # 记录详细堆栈
            logger.debug(traceback.format_exc())
            # 如果发送失败，不再寻找其他平台，防止误发喵

    async def _send_proactive_message(self, session_id: str, text: str):
        """
        负责处理主动消息的发送逻辑，包括TTS语音和文本消息。
        支持分段回复功能。

        发送流程：
        1. 检查TTS配置，如果启用则尝试生成语音
        2. 如果TTS成功，发送语音消息
        3. 根据配置决定是否同时发送文本原文
        4. 如果TTS失败或禁用，检查分段回复配置
        5. 如果启用分段回复且文本长度未超过阈值，则分段发送
        6. 否则直接发送文本消息

        特别处理：如果是群聊消息，发送后会立即重置沉默倒计时，
        因为Bot发送消息也意味着群聊有活动。
        """
        session_config = self._get_session_config(session_id)
        if not session_config:
            logger.info(
                f"[主动消息] 无法获取会话配置，跳过 {self._get_session_log_str(session_id)} 的消息发送喵。"
            )
            return

        logger.info(
            f"[主动消息] 开始发送 {self._get_session_log_str(session_id, session_config)} 的主动消息喵。"
        )

        tts_conf = session_config.get("tts_settings", {})
        seg_conf = session_config.get("segmented_reply_settings", {})

        is_tts_sent = False
        if tts_conf.get("enable_tts", True):
            try:
                logger.info("[主动消息] 尝试进行手动TTS喵。")

                # 兼容性处理：尝试获取 TTS Provider
                tts_provider = None
                try:
                    tts_provider = self.context.get_using_tts_provider(umo=session_id)
                except ValueError as e:
                    if "too many values" in str(e) or "expected 3" in str(e):
                        logger.warning(
                            "[主动消息] TTS检测到非标准 session_id，尝试使用兼容模式重试喵。"
                        )
                        parsed = self._parse_session_id(session_id)
                        if parsed:
                            standard_session_id = f"{parsed[0]}:{parsed[1]}:{parsed[2]}"
                            tts_provider = self.context.get_using_tts_provider(
                                umo=standard_session_id
                            )
                    else:
                        raise e

                if tts_provider:
                    audio_path = await tts_provider.get_audio(text)
                    if audio_path:
                        # TTS 消息通常不需要装饰（或者是音频文件，装饰器可能处理不了），直接发送
                        await self.context.send_message(
                            session_id, MessageChain([Record(file=audio_path)])
                        )
                        is_tts_sent = True
                        # 发送语音后稍微等待一下
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"[主动消息] 手动TTS流程发生异常喵: {e}")

        # 决定是否发送文本
        should_send_text = not is_tts_sent or tts_conf.get("always_send_text", True)

        if should_send_text:
            # 检查是否启用分段回复
            enable_seg = seg_conf.get("enable", False)
            threshold = seg_conf.get("words_count_threshold", 150)

            # 如果启用了分段回复，且字数未超过阈值（为了防止长文被打断影响阅读体验，长文不分段）
            if enable_seg and len(text) <= threshold:
                segments = self._split_text(text, seg_conf)
                if not segments:  # 分段结果为空，直接发送原文本
                    segments = [text]

                logger.info(
                    f"[主动消息] 分段回复已启用，将发送 {len(segments)} 条消息喵。"
                )

                for idx, seg in enumerate(segments):
                    # 使用带 Hook 的发送方法
                    await self._send_chain_with_hooks(session_id, [Plain(text=seg)])

                    # 如果不是最后一条消息，则等待一段时间
                    if idx < len(segments) - 1:
                        interval = await self._calc_interval(seg, seg_conf)
                        logger.debug(f"[主动消息] 分段回复等待 {interval:.2f} 秒喵。")
                        await asyncio.sleep(interval)
            else:
                # 直接发送
                # 使用带 Hook 的发送方法
                await self._send_chain_with_hooks(session_id, [Plain(text=text)])

        # Bot 自己发送的消息，也应该被视为一次"活动"，重置群聊的沉默倒计时
        if self._is_group_session(session_id):
            # 立即重置，不要等待，确保时序正确
            await self._reset_group_silence_timer(session_id)
            logger.info(
                f"[主动消息] Bot主动消息已发送，已重置 {self._get_session_log_str(session_id, session_config)} 的沉默倒计时喵。"
            )

            # 记录Bot发送消息的时间，用于辅助检测Bot消息
            # 有些平台下self_id可能获取不到，我们可以通过发送时间来辅助判断
            self.last_bot_message_time = time.time()
            # 注意：群聊的next_trigger_time在任务成功完成后会被清理，这是正确的行为
            # 因为群聊使用监控沉默倒计时与APScheduler结合的机制，而不是固定的APScheduler任务

    async def _finalize_and_reschedule(
        self,
        session_id: str,
        conv_id: str,
        user_prompt: str,
        assistant_response: str,
        unanswered_count: int,
    ):
        """
        负责主动消息任务完成后的收尾工作，包括：
        1. 存档对话历史（使用add_message_pair）
        2. 更新未回复计数器
        3. 重新调度下一个任务（仅私聊）
        4. 保存所有状态到持久化存储

        此函数区分私聊和群聊的处理逻辑：
        - 私聊：立即重新调度下一个主动消息任务
        - 群聊：清理定时任务数据，使用沉默倒计时机制
        """
        # 1. 使用新的对话管理API存档对话历史
        try:
            # 使用预导入的TextPart，避免重复导入
            user_msg_obj = UserMessageSegment(content=[TextPart(text=user_prompt)])
            assistant_msg_obj = AssistantMessageSegment(
                content=[TextPart(text=assistant_response)]
            )

            await self.context.conversation_manager.add_message_pair(
                cid=conv_id,
                user_message=user_msg_obj,
                assistant_message=assistant_msg_obj,
            )
            logger.info("[主动消息] 已成功将本次主动消息存档至对话历史喵。")
        except Exception as e:
            logger.error(f"[主动消息] 存档对话历史失败喵: {e}")
            # 存档失败时不中断主流程，只记录错误
            logger.warning("[主动消息] 对话存档失败喵，但会继续执行后续步骤喵。")

        # 2. 然后再获取锁，执行关键区代码
        lock = self.data_lock
        scheduler = self.scheduler
        if lock is None:
            return

        async with lock:
            # 更新计数器 (对私聊和群聊都适用)
            # 只有在Bot成功发送消息给用户后，才增加未回复计数器
            # 每个会话(私聊/群聊)都有独立的计数器，不会相互影响
            new_unanswered_count = unanswered_count + 1
            self.session_data.setdefault(session_id, {})["unanswered_count"] = (
                new_unanswered_count
            )
            logger.info(
                f"[主动消息] {self._get_session_log_str(session_id)} 的第 {new_unanswered_count} 次主动消息已发送完成，当前未回复次数: {new_unanswered_count} 次喵。"
            )

            # 重新调度 (只对私聊进行立即的、连续的重新调度)
            if self._is_private_session(session_id):
                session_config = self._get_session_config(session_id)
                if not session_config:
                    return
                schedule_conf = session_config.get("schedule_settings", {})

                min_interval = int(schedule_conf.get("min_interval_minutes", 30)) * 60
                max_interval = max(
                    min_interval,
                    int(schedule_conf.get("max_interval_minutes", 900)) * 60,
                )
                random_interval = random.randint(min_interval, max_interval)
                next_trigger_time = time.time() + random_interval
                run_date = datetime.fromtimestamp(next_trigger_time, tz=self.timezone)

                if scheduler is None:
                    return

                scheduler.add_job(
                    self.check_and_chat,
                    "date",
                    run_date=run_date,
                    args=[session_id],
                    id=session_id,
                    replace_existing=True,
                    misfire_grace_time=60,
                )

                self.session_data.setdefault(session_id, {})["next_trigger_time"] = (
                    next_trigger_time
                )
                logger.info(
                    f"[主动消息] 已为 {self._get_session_log_str(session_id, session_config)} 安排下一次主动消息喵，时间：{run_date.strftime('%Y-%m-%d %H:%M:%S')} 喵。"
                )

            # 保存所有状态
            await self._save_data_internal()

    async def check_and_chat(self, session_id: str):
        """
        由定时任务触发的核心函数（车间主任），负责完成一次完整的主动消息流程。

        这是整个插件的核心调度函数，工作流程：
        1. 检查是否允许聊天（插件启用、非免打扰时段）
        2. 检查未回复次数是否达到上限
        3. 准备LLM请求（获取上下文、人格、构造Prompt）
        4. 调用LLM生成回复内容
        5. 发送生成的消息（支持TTS）
        6. 收尾工作（存档、更新计数器、重新调度）

        支持AstrBot 4.5.7+新API，同时保持向后兼容性。
        具备完善的错误处理机制，确保任务链不会中断。
        """
        lock = self.data_lock
        session_config: dict | None = None
        try:
            if lock is None:
                return

            if not await self._is_chat_allowed(session_id):
                logger.info("[主动消息] 当前为免打扰时段，跳过并重新调度喵。")
                await self._schedule_next_chat_and_save(session_id)
                return

            session_config = self._get_session_config(session_id)
            if not session_config:
                return

            schedule_conf = session_config.get("schedule_settings", {})

            async with lock:
                unanswered_count = self.session_data.get(session_id, {}).get(
                    "unanswered_count", 0
                )
                max_unanswered = schedule_conf.get("max_unanswered_times", 3)
                if max_unanswered > 0 and unanswered_count >= max_unanswered:
                    logger.info(
                        f"[主动消息] {self._get_session_log_str(session_id, session_config)} 的未回复次数 ({unanswered_count}) 已达到上限 ({max_unanswered})，暂停主动消息喵。"
                    )
                    return

            # 计数器逻辑说明
            # unanswered_count 未回复计数器
            # 0 = 还没有发送过主动消息，或用户已经回复
            # 1 = 已经发送了第1次主动消息，未回复次数1
            # 2 = 已经发送了第2次主动消息，未回复次数2，以此类推。
            logger.info(
                f"[主动消息] 开始生成第 {unanswered_count + 1} 次主动消息喵，当前未回复次数: {unanswered_count} 次喵。"
            )

            # 在任务执行前，通过活跃平台实例和历史记录动态修正 ID，确保 UMO 的时效性和准确性喵
            parsed = self._parse_session_id(session_id)
            if parsed:
                original_platform, msg_type, target_id = parsed
                # 传入 original_platform 作为首选平台
                new_session_id = self._resolve_full_umo(
                    target_id, msg_type, original_platform
                )

                # 严格检查：如果修正后的平台并未运行，灵灵不应该强行发送喵！
                new_parsed = self._parse_session_id(new_session_id)
                if new_parsed:
                    new_platform = new_parsed[0]
                    # 获取所有物理实例的状态喵
                    insts = {
                        p.meta().id: p
                        for p in self.context.platform_manager.get_insts()
                        if p.meta().id
                    }

                    # 检查平台是否存在且运行
                    # 只有当平台 ID 在当前实例列表中完全找不到（说明是无效的占位符或已被移除），或者实例未运行时，才拦截
                    platform_inst = insts.get(new_platform)
                    if (
                        not platform_inst
                        or platform_inst.status != PlatformStatus.RUNNING
                    ):
                        logger.warning(
                            f"[主动消息] 平台 {new_platform} 不存在或未运行（可能已全部停用），跳过主动消息发送喵。"
                        )
                        # 重新调度，等待机器人上线喵
                        await self._schedule_next_chat_and_save(session_id)
                        return

                if new_session_id != session_id:
                    logger.debug(
                        f"[主动消息] UMO 动态修正: {session_id} -> {new_session_id} 喵"
                    )
                    session_id = new_session_id

            try:
                request_package = await self._prepare_llm_request(session_id)
            except ValueError as e:
                # 捕获 "too many values to unpack" 错误
                if "too many values" in str(e) or "expected 3" in str(e):
                    logger.warning(
                        f"[主动消息] 检测到非标准 session_id ({session_id}) 导致核心解析错误，尝试使用兼容模式重试喵。"
                    )
                    parsed = self._parse_session_id(session_id)
                    if parsed:
                        # 重新构建标准的 3 段式 ID 进行重试
                        standard_session_id = f"{parsed[0]}:{parsed[1]}:{parsed[2]}"
                        logger.info(
                            f"[主动消息] 兼容模式标准化 ID: {standard_session_id}"
                        )
                        request_package = await self._prepare_llm_request(
                            standard_session_id
                        )
                    else:
                        raise e
                else:
                    raise e

            if not request_package:
                await self._schedule_next_chat_and_save(session_id)
                return

            conv_id = request_package["conv_id"]
            pure_history_messages = request_package["history"]
            original_system_prompt = request_package["system_prompt"]

            # 记录任务开始时的状态快照
            # 用于检测LLM调用期间是否有新消息到达
            task_start_state = {
                "last_message_time": self.last_message_times.get(session_id, 0),
                "unanswered_count": unanswered_count,
                "timestamp": time.time(),
            }

            # 使用新的统一LLM调用接口，基于最新文档的推荐方式，简化API调用逻辑
            # 准备prompt模板和时间字符串
            motivation_template = session_config.get("proactive_prompt", "")
            now_str = datetime.now(self.timezone).strftime("%Y年%m月%d日 %H:%M")
            final_user_simulation_prompt = motivation_template.replace(
                "{{unanswered_count}}", str(unanswered_count)
            ).replace("{{current_time}}", now_str)

            logger.info("[主动消息] 已生成包含动机和时间的 Prompt 喵。")

            # 使用新的统一LLM调用接口
            llm_response_obj = None
            try:
                # 获取当前会话使用的LLM提供商ID（v4.5.7+新API）
                provider_id = await self.context.get_current_chat_provider_id(
                    session_id
                )

                # 优化：在调用 LLM 前始终清洗历史记录，确保兼容性喵！
                # 这能有效防止某些适配器将对象误转为 [object Object] 字符串喵。
                pure_history_messages = self._sanitize_history_content(
                    pure_history_messages
                )

                # 使用统一的llm_generate接口调用LLM
                llm_response_obj = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=final_user_simulation_prompt,
                    contexts=pure_history_messages,
                    system_prompt=original_system_prompt,
                )

                logger.info("[主动消息] 使用新API调用LLM成功喵。")

            except Exception as llm_error:
                # 改进错误处理，提供更详细的错误信息
                logger.error(f"[主动消息] 使用新API调用LLM失败喵: {llm_error}")
                logger.info(f"[主动消息] 错误类型喵: {type(llm_error).__name__}")
                logger.info(f"[主动消息] 错误详情喵: {str(llm_error)}")

                # 尝试使用传统方式作为回退
                try:
                    provider = self.context.get_using_provider(umo=session_id)
                    if provider:
                        llm_response_obj = await provider.text_chat(
                            prompt=final_user_simulation_prompt,
                            contexts=pure_history_messages,
                            system_prompt=original_system_prompt,
                        )
                        logger.info("[主动消息] 使用传统API回退成功喵。")
                    else:
                        logger.warning(
                            "[主动消息] 未找到 LLM Provider，放弃并重新调度喵。"
                        )
                        await self._schedule_next_chat_and_save(session_id)
                        return
                except Exception as fallback_error:
                    # 记录更详细的回退失败信息
                    logger.error(f"[主动消息] 传统API回退也失败喵: {fallback_error}")
                    logger.info(
                        f"[主动消息] 回退错误类型喵: {type(fallback_error).__name__}"
                    )
                    logger.error(
                        "[主动消息] 呜喵？！LLM调用完全失败，将重新调度任务喵。"
                    )
                    await self._schedule_next_chat_and_save(session_id)
                    return

            if llm_response_obj and llm_response_obj.completion_text:
                response_text = llm_response_obj.completion_text.strip()

                # 灵灵的“对象”拦截器喵！
                # 如果 LLM 真的返回了这串 JS 风格的错误文本，灵灵会拦截它喵
                if response_text == "[object Object]":
                    logger.error(
                        "[主动消息] 喵呜！LLM 返回了意料之外的 '[object Object]' 字符串喵！"
                    )
                    logger.warning(
                        "[主动消息] 这通常是因为上下文或 Prompt 中包含了无法解析的对象喵。已拦截本次发送喵。"
                    )
                    await self._schedule_next_chat_and_save(session_id)
                    return

                logger.info(f"[主动消息] LLM 已生成文本喵: '{response_text}'。")

                # 在发送消息前检查状态一致性
                # 如果在LLM调用期间用户发送了新消息，则丢弃本次生成结果
                current_state = {
                    "last_message_time": self.last_message_times.get(session_id, 0),
                    "unanswered_count": self.session_data.get(session_id, {}).get(
                        "unanswered_count", 0
                    ),
                }

                # 检查是否有新消息到达（比较时间戳和计数器）
                has_new_message = (
                    current_state["last_message_time"]
                    > task_start_state["last_message_time"]
                    or current_state["unanswered_count"]
                    < task_start_state["unanswered_count"]
                )

                if has_new_message:
                    logger.info(
                        "[主动消息] 检测到用户在LLM生成期间发送了新消息，丢弃本次主动消息喵。"
                    )
                    # 不重新调度任务，因为用户消息已经触发了新的任务创建
                    # on_private_message 已经处理了任务重新调度
                    return

                await self._send_proactive_message(session_id, response_text)

                # 调用新的、原子化的收尾函数
                await self._finalize_and_reschedule(
                    session_id,
                    conv_id,
                    final_user_simulation_prompt,
                    response_text,
                    unanswered_count,
                )

                # 任务成功完成后，区分不同情况来处理数据持久化，正确处理定时任务数据
                # 情况1: 群聊任务 - 清理数据，因为群聊使用沉默倒计时机制
                if self._is_group_session(session_id):
                    async with lock:
                        if (
                            session_id in self.session_data
                            and "next_trigger_time" in self.session_data[session_id]
                        ):
                            del self.session_data[session_id]["next_trigger_time"]
                            await self._save_data_internal()

                # 情况2: 私聊任务 - 保留数据，因为私聊使用APScheduler定时任务，直到任务真正执行或过期
                else:
                    # 安全更新数据，避免数据丢失
                    # 私聊任务保留next_trigger_time数据，用于程序重启时恢复
                    async with lock:
                        # 安全地更新数据，而不是完全替换，避免数据丢失
                        if session_id in self.session_data:
                            # 只更新必要的字段，保留其他现有数据
                            self.session_data[session_id]["unanswered_count"] = (
                                self.session_data[session_id].get("unanswered_count", 0)
                            )
                            # 确保next_trigger_time存在
                            if "next_trigger_time" not in self.session_data[session_id]:
                                self.session_data[session_id]["next_trigger_time"] = (
                                    None
                                )
                            await self._save_data_internal()

            else:
                logger.warning("[主动消息] LLM 调用失败或返回空内容，重新调度喵。")
                await self._schedule_next_chat_and_save(session_id)

        except Exception as e:
            # 改进错误日志记录，分类处理不同类型的错误
            error_type = type(e).__name__
            error_msg = str(e)

            logger.error("[主动消息] check_and_chat 任务发生致命错误喵:")
            logger.error(f"[主动消息] 错误类型喵: {error_type}")
            logger.error(f"[主动消息] 错误信息喵: {error_msg}")
            logger.debug(f"[主动消息] 详细堆栈信息喵:\n{traceback.format_exc()}")

            # 根据错误类型进行不同的处理
            if "RateLimitError" in error_type or "quota" in error_msg.lower():
                logger.warning("[主动消息] 检测到API限制错误，将延长重试间隔喵。")
                # 可以在这里添加更长的延迟逻辑
            elif "Connection" in error_type or "Timeout" in error_type:
                logger.warning("[主动消息] 检测到连接错误，可能需要检查网络设置喵。")
            elif "Authentication" in error_type or "auth" in error_msg.lower():
                logger.error("[主动消息] 认证错误，请检查API密钥配置喵。")
                # 认证错误通常需要手动干预，可以暂停任务
                return

            # 任务失败后也清理数据，避免残留
            try:
                if lock is None:
                    return
                async with lock:
                    if (
                        session_id in self.session_data
                        and "next_trigger_time" in self.session_data[session_id]
                    ):
                        del self.session_data[session_id]["next_trigger_time"]
                        await self._save_data_internal()
            except Exception as clean_e:
                logger.debug(f"[主动消息] 清理失败任务数据时出错喵: {clean_e}")

            # 错误恢复：尝试重新调度
            try:
                logger.info(
                    f"[主动消息] 尝试重新调度 {self._get_session_log_str(session_id, session_config)} 的主动消息任务喵。"
                )
                await self._schedule_next_chat_and_save(session_id)
                logger.info(
                    f"[主动消息] {self._get_session_log_str(session_id, session_config)} 的任务重新调度成功喵。"
                )
            except Exception as se:
                logger.error(f"[主动消息] 在错误处理中重新调度失败喵: {se}")
                logger.error(
                    f"[主动消息] {self._get_session_log_str(session_id, session_config)} 可能需要手动干预喵。"
                )

    # 清理过期会话状态函数
    def _cleanup_expired_session_states(self, current_time: float):
        """
        清理过期的会话状态，防止内存泄漏。

        删除超过5分钟的旧状态条目，确保内存使用保持合理。
        由于状态只在Bot回复时清理，如果Bot没有回复，状态会永久残留。
        """
        expired_sessions = []
        timeout_seconds = 300  # 5分钟超时

        for session_id, state in self.session_temp_state.items():
            last_user_time = state.get("last_user_time", 0)
            if current_time - last_user_time > timeout_seconds:
                expired_sessions.append(session_id)

        # 删除过期的会话状态
        for session_id in expired_sessions:
            del self.session_temp_state[session_id]


def is_quiet_time(quiet_hours_str: str, tz: zoneinfo.ZoneInfo | None) -> bool:
    """检查当前时间是否处于免打扰时段。"""
    try:
        start_str, end_str = quiet_hours_str.split("-")
        start_hour, end_hour = int(start_str), int(end_str)
        now = datetime.now(tz) if tz else datetime.now()
        # 处理跨天的情况 (例如 23-7)
        if start_hour <= end_hour:
            return start_hour <= now.hour < end_hour
        else:
            return now.hour >= start_hour or now.hour < end_hour
    # 捕获可能发生的多种异常
    except (ValueError, TypeError):
        return False
