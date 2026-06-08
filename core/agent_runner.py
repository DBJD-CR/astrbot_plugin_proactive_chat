"""主动消息链式工具调用与模型自动回退能力。

通过 AstrBot 的工具循环 Agent（``context.tool_loop_agent``），让主动消息的正文
生成具备两项能力（对应 issue #56）：

1. 链式工具调用：在一次主动消息里，由模型按需多次调用已注册的工具（如查天气、
   查新闻、读文件），再据此生成最终要发送的文本。
2. 模型自动回退：首选模型不可用（限流/报错/空输出）时，自动顺延切换到备选模型。
   复用本体 ``ToolLoopAgentRunner`` 的 ``fallback_providers`` 机制。

设计要点：
- 默认关闭：tool_mode=off 且未开回退时，调用方应继续走原有的单次 LLM 生成路径。
- 合成事件吞掉发送：工具在循环中调用 ``event.send`` 的中间产物不会真正发往平台，
  最终消息只取 Agent 的 ``completion_text``，避免把工具原始数据发给用户。
- 失败静默降级：任何环节出错都只记录日志并返回 None，由调用方回退为单次生成。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from astrbot.api import logger

try:
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.message.components import Image, Plain
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.provider.provider import Provider

    _AGENT_AVAILABLE = True
except ImportError as _e:  # pragma: no cover - 取决于宿主版本
    _AGENT_AVAILABLE = False
    Provider = object  # type: ignore[assignment, misc]
    logger.warning(f"[主动消息] 当前 AstrBot 版本不支持链式工具调用所需组件喵: {_e}")


# 占位符，便于在依赖缺失时仍能定义类型注解。
_BaseEvent = AstrMessageEvent if _AGENT_AVAILABLE else object


class ProactiveAgentEvent(_BaseEvent):  # type: ignore[misc, valid-type]
    """主动消息工具循环用的统一合成事件。

    构造一个不绑定真实平台连接的合成事件供工具循环 Agent 使用，并重写 ``send``：
    - 捕获其中的图片组件到 ``captured_images``（供配图能力取用）；
    - 其余内容一律吞掉，不真正发往平台。

    这样同一个事件既能服务于“正文工具循环”（只取 Agent 的 ``completion_text``），
    也能服务于“配图工具循环”（取 ``captured_images``），消除两套几乎一致的合成事件。
    """

    def __init__(
        self,
        *,
        context: Any,
        session: "MessageSession",
        message: str,
        message_type: "MessageType",
    ) -> None:
        platform_meta = PlatformMetadata(
            name="proactive_agent",
            description="ProactiveChat 工具循环合成事件",
            id=session.platform_id,
        )

        msg_obj = AstrBotMessage()
        msg_obj.type = message_type
        msg_obj.self_id = "astrbot"
        msg_obj.session_id = session.session_id
        msg_obj.message_id = uuid.uuid4().hex
        msg_obj.sender = MessageMember(user_id=session.session_id, nickname="主动消息")
        msg_obj.message = [Plain(message)]
        msg_obj.message_str = message
        msg_obj.raw_message = message
        msg_obj.timestamp = int(time.time())

        super().__init__(message, msg_obj, platform_meta, session.session_id)

        # 使用原始会话，保证工具内部读取 unified_msg_origin 等信息时一致。
        self.session = session
        self.context_obj = context
        self.is_at_or_wake_command = True
        self.is_wake = True

        # 收集被拦截的图片组件，供配图能力在 Agent 结束后取用。
        self.captured_images: list["Image"] = []

    async def send(self, message: Any) -> None:
        """拦截发送：收集图片组件，其余内容一律吞掉，均不真正发往平台。

        兼容多种传入形式：MessageChain（含 .chain）、组件列表/元组、单个组件——
        因为第三方工具调用 ``event.send`` 的写法不完全一致。
        """
        try:
            if message is not None:
                chain = getattr(message, "chain", None)
                if chain is not None:
                    comps = chain
                elif isinstance(message, (list, tuple)):
                    comps = message
                else:
                    comps = [message]
                for comp in comps:
                    if isinstance(comp, Image):
                        self.captured_images.append(comp)
        except Exception as e:  # noqa: BLE001 - 拦截阶段不应影响主流程
            logger.debug(f"[主动消息] 捕获组件时出现异常喵: {e!r}")
        # 故意不调用 super().send()，避免触发真实平台发送与统计。

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        async for chain in generator:
            await self.send(chain)


class AgentRunnerMixin:
    """为主动消息提供链式工具调用与模型回退能力的 Mixin。"""

    context: Any

    @staticmethod
    def _parse_tool_name_list(raw: Any) -> list[str]:
        """把配置值解析为工具名列表。

        兼容 list 与逗号/换行分隔的字符串两种写法，去重并保持顺序。
        """
        names: list[str] = []
        if isinstance(raw, str):
            for piece in raw.replace("\n", ",").split(","):
                piece = piece.strip()
                if piece:
                    names.append(piece)
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                piece = str(item).strip()
                if piece:
                    names.append(piece)
        seen: set[str] = set()
        result: list[str] = []
        for n in names:
            if n not in seen:
                seen.add(n)
                result.append(n)
        return result

    @staticmethod
    def _compute_max_steps(raw_value: Any, tool_count: int) -> int:
        """计算工具循环最大步数。

        raw_value<=0（或非法）：自动——按工具数推导（每个工具留“调用+追问”两步，
        再加 2 步余量），clamp 到 [3, 30]。
        raw_value>0：用户指定的固定上限，clamp 到 [1, 30]。
        """
        try:
            configured = int(raw_value or 0)
        except Exception:  # noqa: BLE001
            configured = 0
        if configured > 0:
            return max(1, min(configured, 30))
        count = max(0, int(tool_count or 0))
        return max(3, min(count * 2 + 2, 30))

    def _select_agent_tools(self, tool_manager: Any, agent_conf: dict) -> Any:
        """按 tool_mode 选出交给 Agent 的工具集（ToolSet，可能为空）。

        - whitelist：仅取 include_tools 命中的工具。
        - all：取全部已注册工具（func_list）。
        两种模式都会减去 exclude_tools。off 模式不应调用本方法。
        """
        mode = str((agent_conf or {}).get("tool_mode", "off") or "off").strip().lower()
        include = self._parse_tool_name_list(
            (agent_conf or {}).get("include_tools", [])
        )
        exclude = set(
            self._parse_tool_name_list((agent_conf or {}).get("exclude_tools", []))
        )

        tool_set = ToolSet()
        if mode == "whitelist":
            for name in include:
                if name in exclude:
                    continue
                tool = (
                    tool_manager.get_func(name)
                    if hasattr(tool_manager, "get_func")
                    else None
                )
                if tool is not None:
                    tool_set.add_tool(tool)
        elif mode == "all":
            try:
                all_tools = list(getattr(tool_manager, "func_list", []) or [])
            except Exception:  # noqa: BLE001
                all_tools = []
            for tool in all_tools:
                name = getattr(tool, "name", "") or ""
                if name and name not in exclude:
                    tool_set.add_tool(tool)
        return tool_set

    def _resolve_fallback_providers(
        self,
        session_id: str,
        primary_provider_id: str,
        agent_conf: dict,
    ) -> list[Any]:
        """解析模型回退用的备选 Provider 列表，对齐本体 _get_fallback_chat_providers。

        优先读插件本地的 model_fallback_models；留空则回退读取 AstrBot 全局
        provider_settings.fallback_chat_models。逐个解析为 Provider，仅保留有效的
        对话模型，去重并剔除主 Provider。
        """
        fallback_ids = self._parse_tool_name_list(
            (agent_conf or {}).get("model_fallback_models", [])
        )

        # 本地列表为空时，回退读取全局配置（与本体行为对齐）。
        if not fallback_ids:
            try:
                global_conf = self.context.get_config(session_id)
            except Exception:  # noqa: BLE001
                global_conf = None
            if global_conf is None:
                try:
                    global_conf = self.context.get_config()
                except Exception:  # noqa: BLE001
                    global_conf = None
            provider_settings = {}
            if isinstance(global_conf, dict):
                provider_settings = global_conf.get("provider_settings", {}) or {}
            elif global_conf is not None:
                provider_settings = getattr(global_conf, "provider_settings", {}) or {}
            raw_global = (
                provider_settings.get("fallback_chat_models", [])
                if isinstance(provider_settings, dict)
                else []
            )
            fallback_ids = self._parse_tool_name_list(raw_global)

        seen: set[str] = set()
        if primary_provider_id:
            seen.add(primary_provider_id)

        providers: list[Any] = []
        for fid in fallback_ids:
            if fid in seen:
                continue
            try:
                prov = self.context.get_provider_by_id(fid)
            except Exception:  # noqa: BLE001
                prov = None
            if prov is None:
                logger.warning(
                    f"[主动消息] 备选模型 Provider “{fid}” 未找到，已跳过喵。"
                )
                continue
            if _AGENT_AVAILABLE and not isinstance(prov, Provider):
                logger.warning(
                    f"[主动消息] 备选模型 Provider “{fid}” 类型不正确，已跳过喵。"
                )
                continue
            providers.append(prov)
            seen.add(fid)
        return providers

    async def _run_proactive_agent(
        self,
        session_id: str,
        prompt: str,
        contexts: list,
        system_prompt: str,
        agent_conf: dict,
    ) -> str | None:
        """用工具循环 Agent 生成主动消息正文，支持链式工具调用与模型回退。

        返回生成的文本；任何环节失败都返回 None，由调用方降级为单次生成。
        """
        if not _AGENT_AVAILABLE:
            logger.warning(
                "[主动消息] 当前 AstrBot 版本不支持工具循环 Agent，回退为单次生成喵。"
            )
            return None

        try:
            provider_id = await self.context.get_current_chat_provider_id(session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[主动消息] 获取当前对话 Provider 失败喵: {e!r}")
            return None
        if not provider_id:
            logger.info("[主动消息] 未找到可用对话 Provider，回退为单次生成喵。")
            return None

        mode = str((agent_conf or {}).get("tool_mode", "off") or "off").strip().lower()

        # 仅在需要工具时构建 ToolSet；off 模式（只为回退而走 Agent）不带工具。
        tool_set = None
        if mode in ("whitelist", "all"):
            try:
                tool_manager = self.context.get_llm_tool_manager()
            except Exception:  # noqa: BLE001
                tool_manager = None
            if tool_manager is not None:
                selected = self._select_agent_tools(tool_manager, agent_conf)
                if selected is not None and not selected.empty():
                    tool_set = selected
                    logger.info(
                        f"[主动消息] 链式工具调用已启用，共暴露 {len(tool_set.tools)} 个工具喵。"
                    )
                else:
                    logger.info(
                        "[主动消息] 未选出可用工具，本次仅按普通对话方式生成喵。"
                    )

        # 解析回退 Provider 列表（开启回退时）。
        fallback_providers = None
        if (agent_conf or {}).get("model_fallback_enable", False):
            resolved = self._resolve_fallback_providers(
                session_id, provider_id, agent_conf
            )
            if resolved:
                fallback_providers = resolved
                logger.info(
                    f"[主动消息] 模型自动回退已启用，备选模型 {len(resolved)} 个喵。"
                )

        # max_steps：0（或缺省/非法）表示自动——按本次暴露的工具数推导一个上限；
        # 填正数则视为用户指定的固定上限。
        tool_count = len(tool_set.tools) if tool_set is not None else 0
        max_steps = self._compute_max_steps(
            (agent_conf or {}).get("max_steps", 0), tool_count
        )
        logger.info(
            f"[主动消息] 工具循环最大步数为 {max_steps}（工具数 {tool_count}）喵。"
        )

        session = MessageSession.from_str(session_id)
        agent_event = ProactiveAgentEvent(
            context=self.context,
            session=session,
            message=prompt,
            message_type=session.message_type,
        )

        kwargs: dict[str, Any] = {
            "event": agent_event,
            "chat_provider_id": provider_id,
            "prompt": prompt,
            "contexts": contexts,
            "system_prompt": system_prompt,
            "max_steps": max_steps,
        }
        if tool_set is not None:
            kwargs["tools"] = tool_set
        if fallback_providers:
            kwargs["fallback_providers"] = fallback_providers

        try:
            resp = await self.context.tool_loop_agent(**kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[主动消息] 工具循环 Agent 执行失败，回退为单次生成喵: {e!r}"
            )
            return None

        text = (getattr(resp, "completion_text", "") or "").strip()
        if not text:
            logger.info("[主动消息] 工具循环 Agent 未产出文本，回退为单次生成喵。")
            return None
        return text
