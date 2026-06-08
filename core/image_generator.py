"""主动消息配图能力。

通过 AstrBot 的工具循环 Agent，让主 LLM 调用已安装的生图插件（向主 LLM
注册了工具的插件）来生成配图。生成的图片不会由生图插件直接发往平台，而是被一个
“捕获型”合成事件拦截、收集后交还给本插件，统一走主动消息既有的发送流程
（分段、装饰钩子、平台历史持久化）。

设计要点：
- provider 无关：不绑定任何特定生图插件，按关键词（工具名 + 描述）自动识别生图
  工具，并支持用户用 extra_tools 补充、exclude_tools 排除。
- 提示词由插件端生成：先由插件调用 LLM 生成画面描述，再交给 Agent 据此调用生图
  工具，而非让模型在工具循环里自行编写提示词。
- 失败静默降级：任何环节出错都只记录日志、回退为纯文本，绝不把错误信息塞进
  发送给用户的消息内容。
"""

from __future__ import annotations

from astrbot.api import logger

from .agent_runner import ProactiveAgentEvent

try:
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.platform.message_session import MessageSession

    _IMAGE_AGENT_AVAILABLE = True
except ImportError as _e:  # pragma: no cover - 取决于宿主版本
    _IMAGE_AGENT_AVAILABLE = False
    logger.warning(f"[主动消息] 当前 AstrBot 版本不支持配图 Agent 所需组件喵: {_e}")


class ImageMixin:
    """为主动消息提供“配图”能力的 Mixin。"""

    async def _maybe_generate_proactive_images(
        self, session_id: str, text: str, session_config: dict
    ) -> list:
        """根据配置决定并生成主动消息的配图。

        返回一个图片组件列表（可能为空）。任何失败都会被吞掉并返回空列表，
        以保证主动消息至少能以纯文本形式发出。
        """
        image_conf = (session_config or {}).get("image_settings", {})
        mode = str(image_conf.get("mode", "off") or "off").strip().lower()
        if mode not in ("auto", "always"):
            return []

        if not _IMAGE_AGENT_AVAILABLE:
            logger.warning(
                "[主动消息] 当前 AstrBot 版本不支持配图所需的合成事件组件，跳过配图喵。"
            )
            return []

        try:
            return await self._run_image_agent(session_id, text, image_conf, mode)
        except Exception as e:  # noqa: BLE001 - 配图失败不可影响文本发送
            logger.warning(f"[主动消息] 生成主动消息配图失败，将仅发送文本喵: {e!r}")
            return []

    @staticmethod
    def _parse_name_list(raw) -> list[str]:
        """把配置值解析为名字列表。

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

    def _select_image_tools(self, tool_manager, image_conf: dict):
        """挑选交给 Agent 的生图工具。

        规则：按关键词（工具名 + 描述）自动识别生图工具；再叠加用户配置的
        extra_tools（强制补充，即使不含关键词）与 exclude_tools（强制排除）。
        返回一个 ToolSet（可能为空）。
        """
        extra = set(self._parse_name_list((image_conf or {}).get("extra_tools", [])))
        exclude = set(
            self._parse_name_list((image_conf or {}).get("exclude_tools", []))
        )

        try:
            all_tools = list(getattr(tool_manager, "func_list", []) or [])
        except Exception:  # noqa: BLE001
            all_tools = []

        tool_set = ToolSet()
        for tool in all_tools:
            name = getattr(tool, "name", "") or ""
            if name in exclude:
                continue
            desc = getattr(tool, "description", "") or ""
            hit_keyword = self._looks_like_image_tool(name, desc)
            if hit_keyword or name in extra:
                tool_set.add_tool(tool)

        # extra 里指向但上面没收进来的（理论上已收，双保险按名字补一次）
        for name in extra:
            if tool_set.get_tool(name) is None and name not in exclude:
                t = (
                    tool_manager.get_func(name)
                    if hasattr(tool_manager, "get_func")
                    else None
                )
                if t is not None:
                    tool_set.add_tool(t)
        return tool_set

    # 生图相关关键词（小写）。命中工具名或描述即视为候选生图工具。
    # 视频类（video）刻意不收，避免把视频生成工具当配图。
    _IMAGE_KEYWORDS = (
        "draw",
        "image",
        "paint",
        "pic",
        "photo",
        "illustr",
        "t2i",
        "text2image",
        "text-to-image",
        "stable",
        "diffusion",
        "midjourney",
        "dalle",
        "dall-e",
        "flux",
        "comfyui",
        "render",
        "生图",
        "绘图",
        "绘画",
        "画图",
        "配图",
        "图片",
        "作画",
        "出图",
        "画一",
        "生成图",
    )
    # 强生图信号：命中负向词后，只要工具名或描述里出现这些词，仍判定为生图工具。
    _IMAGE_STRONG = (
        "draw",
        "paint",
        "t2i",
        "text2image",
        "text-to-image",
        "generate image",
        "generate an image",
        "image generation",
        "生图",
        "绘图",
        "绘画",
        "绘制",
        "画图",
        "作画",
        "配图",
        "生成图片",
        "生成一张",
        "画一张",
        "画一幅",
    )
    # 明显非生图但可能含 image 字样的工具，关键词命中后再排除一层。
    _IMAGE_NEGATIVE = (
        "read",
        "ocr",
        "recogn",
        "识别",
        "video",
        "视频",
        "understand",
        "analy",
        "描述图",
    )

    @classmethod
    def _looks_like_image_tool(cls, name: str, description: str) -> bool:
        """按关键词判断一个工具是否像“文生图”工具。

        名字与描述双线匹配：任一命中生图关键词即为候选；命中负向词时，
        只要名字或描述里仍出现强生图信号，依然保留。
        """
        haystack = f"{name} {description}".lower()
        if not any(kw in haystack for kw in cls._IMAGE_KEYWORDS):
            return False
        # 命中负向词时，要求名字或描述里带强生图信号才保留，否则排除。
        if any(neg in haystack for neg in cls._IMAGE_NEGATIVE):
            return any(s in haystack for s in cls._IMAGE_STRONG)
        return True

    # 选不到生图工具的最大累计次数，超过则本次运行内永久回退为纯文本。
    _IMAGE_TOOLS_MAX_ATTEMPTS = 3

    def _ensure_image_tool_names(self, tool_manager, image_conf: dict) -> list[str]:
        """返回已选定并缓存的生图工具名；未选定则尝试选一次。

        - 一旦成功选定就缓存，后续直接复用，不再扫描。
        - 累计选不到达到上限后永久回退（本次运行内不再尝试）。
        """
        if getattr(self, "_image_tools_disabled", False):
            return []
        if getattr(self, "_image_tools_selected", False):
            return list(self._image_tools_cache)

        tool_set = self._select_image_tools(tool_manager, image_conf)
        names = [t.name for t in tool_set.tools] if tool_set else []
        if names:
            self._image_tools_cache = names
            self._image_tools_selected = True
            logger.info("[主动消息] 已找到可用的生图工具喵。")
            return list(names)

        # 没选到：累计计数，达到上限则永久回退。
        self._image_tools_attempts = getattr(self, "_image_tools_attempts", 0) + 1
        if self._image_tools_attempts >= self._IMAGE_TOOLS_MAX_ATTEMPTS:
            self._image_tools_disabled = True
            logger.info(
                "[主动消息] 多次未找到可用生图工具喵，已回退为不生图模式（本次运行内不再尝试）。"
            )
        else:
            logger.info("[主动消息] 暂未找到可用的生图工具喵，稍后重试。")
        return []

    async def prewarm_image_tools(self) -> None:
        """插件加载/空闲时预选一次生图工具，避免发送时才扫描。

        加载阶段生图插件可能尚未就绪，选不到属正常，不计入失败次数。
        """
        if not _IMAGE_AGENT_AVAILABLE:
            return
        if getattr(self, "_image_tools_selected", False) or getattr(
            self, "_image_tools_disabled", False
        ):
            return
        try:
            tool_manager = self.context.get_llm_tool_manager()
        except Exception:  # noqa: BLE001
            return
        # 预热用空配置即可（extra/exclude 在真正发送时按会话配置再算）；
        # 这里只为尽早把“有没有生图工具”探明并缓存。
        tool_set = self._select_image_tools(tool_manager, {})
        names = [t.name for t in tool_set.tools] if tool_set else []
        if names:
            self._image_tools_cache = names
            self._image_tools_selected = True
            logger.info("[主动消息] 已找到可用的生图工具喵。")
        # 预热选不到不记失败、不打 warning，留给发送时按需重试。

    # auto 模式下，模型判断无需配图时回复的固定标记。
    _NO_IMAGE_TOKEN = "NO_IMAGE"

    async def _generate_image_prompt(
        self, provider_id: str, text: str, image_conf: dict, mode: str
    ) -> str:
        """由插件端调用 LLM，把主动消息文本转成一段“画面描述”（生图提示词）。

        - always 模式：总是生成一段画面描述。
        - auto 模式：先让模型判断是否适合配图，不适合则返回空字符串。
        失败一律返回空字符串（跳过配图，不影响文本）。
        """
        extra = str((image_conf or {}).get("extra_prompt", "") or "").strip()
        if mode == "always":
            system_prompt = (
                "你是配图提示词助手。请根据给定的消息内容，写出一段适合用来生图的"
                "画面描述（中文或英文均可），聚焦可视画面：主体、场景、氛围、风格。"
                "只输出画面描述本身，不要解释、不要加引号。"
            )
        else:
            system_prompt = (
                "你是配图提示词助手。请判断给定的消息是否适合配一张图片：\n"
                f"- 不适合（如纯问候、提问、抽象表达）：只回复 {self._NO_IMAGE_TOKEN}。\n"
                "- 适合：写出一段适合用来生图的画面描述（聚焦主体、场景、氛围、风格），"
                "只输出画面描述本身，不要解释、不要加引号。"
            )
        if extra:
            system_prompt = f"{system_prompt}\n补充要求：{extra}"

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=f"消息内容：\n{text}",
                system_prompt=system_prompt,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[主动消息] 生成配图提示词失败喵: {e!r}")
            return ""

        prompt = (getattr(resp, "completion_text", "") or "").strip()
        if not prompt:
            return ""
        # auto 模式下命中“无需配图”标记则跳过。
        if mode != "always" and self._NO_IMAGE_TOKEN in prompt.upper():
            return ""
        return prompt

    async def _run_image_agent(
        self, session_id: str, text: str, image_conf: dict, mode: str
    ) -> list:
        """用工具循环 Agent 驱动任意已注册的生图工具，收集其产出的图片。"""
        context = self.context

        # 解析会话来源，构造合成事件所需的 MessageSession。
        session = MessageSession.from_str(session_id)

        # 拿到当前会话使用的对话 provider；没有则无法驱动 Agent。
        provider_id = await context.get_current_chat_provider_id(session_id)
        if not provider_id:
            logger.info("[主动消息] 未找到可用对话 provider，跳过配图喵。")
            return []

        # 取已选定（并缓存）的生图工具名；未选定则尝试选一次。
        tool_manager = context.get_llm_tool_manager()
        if not tool_manager:
            logger.info("[主动消息] 未找到 LLM 工具管理器，跳过配图喵。")
            return []
        tool_names = self._ensure_image_tool_names(tool_manager, image_conf)
        if not tool_names:
            # 已选不到（或已永久回退），_ensure_image_tool_names 内已记日志。
            return []

        # 用缓存的工具名重建本次调用的 ToolSet。
        tool_set = ToolSet()
        for name in tool_names:
            tool = tool_manager.get_func(name)
            if tool is not None:
                tool_set.add_tool(tool)
        if tool_set.empty():
            logger.info("[主动消息] 已选定的生图工具当前不可用，跳过配图喵。")
            return []

        # 第一步：由插件端先生成“画面描述”（生图提示词）。
        # auto 模式下若模型判断这条消息不适合配图，会返回空，此时直接跳过。
        image_prompt = await self._generate_image_prompt(
            provider_id, text, image_conf, mode
        )
        if not image_prompt:
            logger.info("[主动消息] 未生成配图提示词（判断无需配图），跳过配图喵。")
            return []

        capture_event = ProactiveAgentEvent(
            context=context,
            session=session,
            message=text,
            message_type=session.message_type,
        )

        # 第二步：把插件生成好的画面描述作为明确指令交给 Agent，让它据此调用生图
        # 工具——描述由插件掌控，要求模型原样使用、不要改写或自行编造。
        system_prompt = (
            "你的唯一任务是调用一个可用的生图工具，为下面给出的画面描述生成图片。\n"
            "请把画面描述原样作为生图工具的绘画提示词，不要改写、缩写或自行发挥；\n"
            "调用工具即可，不要输出多余文字。"
        )
        user_prompt = f"画面描述：\n{image_prompt}"

        await context.tool_loop_agent(
            event=capture_event,
            chat_provider_id=provider_id,
            prompt=user_prompt,
            tools=tool_set,
            system_prompt=system_prompt,
            max_steps=6,
        )

        images = list(capture_event.captured_images)
        if images:
            logger.info(f"[主动消息] 配图 Agent 共捕获 {len(images)} 张图片喵。")
        else:
            logger.info("[主动消息] 配图 Agent 未产出图片（工具未生图或调用失败）喵。")
        return images
