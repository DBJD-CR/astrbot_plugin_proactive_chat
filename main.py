# 文件名: main.py (位于 data/plugins/astrbot_plugin_proactive_chat/ 目录下)
# 版本: 0.9.7 (稳定版)

# 导入标准库
import random
import time
import traceback
import json
import os
from datetime import datetime, timedelta
import zoneinfo
import asyncio

# 导入第三方库
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 导入 AstrBot 的核心 API 和组件
import astrbot.api.star as star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Record, Plain

# --- 全局常量定义 ---

# 修复：使用与插件名一致的、唯一的持久化文件名，以避免与其他插件产生潜在的命名冲突
# 这是高质量开源插件开发的最佳实践
SESSION_DATA_FILE = os.path.join(get_astrbot_data_path(), "astrbot_plugin_proactive_chat_data.json")

# --- 工具函数 ---

def load_session_data_from_file() -> dict:
    """从文件中加载会话数据。"""
    if os.path.exists(SESSION_DATA_FILE):
        try:
            with open(SESSION_DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            # 如果文件存在但无法解析，返回空字典以防止程序崩溃
            return {}
    return {}

def save_session_data_to_file(data: dict):
    """将会话数据保存到文件。"""
    try:
        # 使用 indent=4 和 ensure_ascii=False 来保证 JSON 文件的可读性
        with open(SESSION_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[主动消息] 保存会话数据失败: {e}")

def is_quiet_time(quiet_hours_str: str, tz: zoneinfo.ZoneInfo) -> bool:
    """检查当前时间是否处于免打扰时段。"""
    try:
        start_str, end_str = quiet_hours_str.split('-')
        start_hour, end_hour = int(start_str), int(end_str)
        now = datetime.now(tz) if tz else datetime.now()
        # 处理跨天的情况 (例如 23-7)
        if start_hour <= end_hour:
            return start_hour <= now.hour < end_hour
        else:
            return now.hour >= start_hour or now.hour < end_hour
    except Exception:
        # 如果配置格式错误，默认为非免打扰时段
        return False

# --- 插件主类 ---

@star.register(
    name="astrbot_plugin_proactive_chat",
    author="DBJD-CR & Gemini-2.5-Pro",
    version="0.9.7",
    desc="一个让Bot能够发起主动消息的插件，拥有上下文感知、动态情绪、免打扰时段和健壮的TTS集成。"
)
class Main(star.Star):
    """
    插件的主类，继承自 astrbot.api.star.Star。
    负责插件的生命周期管理、事件监听和核心逻辑执行。
    """
    def __init__(self, context: star.Context, config) -> None:
        """
        插件的构造函数。
        当 AstrBot 加载插件时被调用。
        """
        super().__init__(context)
        self.config = config  # 插件的配置对象，从 _conf_schema.json 读取
        self.scheduler = None # 定时任务调度器实例
        self.timezone = None  # 时区信息
        self.session_data = load_session_data_from_file() # 从文件加载持久化的会话数据
        logger.info("[主动消息] 插件实例已创建。")

    async def initialize(self):
        """
        插件的异步初始化函数。
        在 AstrBot 的主事件循环准备好后被调用。
        这是创建和启动异步任务（如 APScheduler）的正确位置。
        """
        try:
            # 从 AstrBot 主配置中获取时区设置
            self.timezone = zoneinfo.ZoneInfo(self.context.get_config().get("timezone"))
        except Exception:
            self.timezone = None
        
        # 创建一个独立的、属于本插件的异步调度器实例
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()
        
        # 从持久化数据中恢复因重启而中断的定时任务
        await self._init_jobs_from_data()
        logger.info("[主动消息] 调度器已初始化。")

    async def _init_jobs_from_data(self):
        """从文件中恢复定时任务。"""
        # 修复：从新的 "basic_settings" 分组中读取核心配置
        basic_conf = self.config.get("basic_settings", {})
        target_user_id = str(basic_conf.get("target_user_id", "")).strip()
        if not target_user_id: return
        
        for session_id, session_info in self.session_data.items():
            # 确保只恢复属于目标用户的私聊任务
            if f":{target_user_id}" in session_id and "private" in session_id:
                next_trigger = session_info.get("next_trigger_time", 0)
                # 如果任务的预定执行时间还没到，就重新安排它
                if time.time() < next_trigger:
                    run_date = datetime.fromtimestamp(next_trigger)
                    self.scheduler.add_job(
                        self.check_and_chat, 
                        'date', 
                        run_date=run_date, 
                        args=[session_id], 
                        id=session_id, 
                        replace_existing=True, 
                        misfire_grace_time=60 # 允许任务在错过触发时间后 60 秒内依然执行
                    )

    async def _schedule_next_chat(self, session_id: str):
        """安排下一次主动聊天的定时任务。"""
        # 修复：从分组后的配置中正确读取参数
        schedule_conf = self.config.get("schedule_settings", {})
        min_interval = int(schedule_conf.get("min_interval_minutes", 30)) * 60
        max_interval = max(min_interval, int(schedule_conf.get("max_interval_minutes", 900)) * 60)
        random_interval = random.randint(min_interval, max_interval)
        
        next_trigger_time = time.time() + random_interval
        run_date = datetime.fromtimestamp(next_trigger_time)
        
        # 添加一个新的定时任务
        self.scheduler.add_job(
            self.check_and_chat, 
            'date', 
            run_date=run_date, 
            args=[session_id], 
            id=session_id, 
            replace_existing=True, # 如果已存在同名任务，则替换
            misfire_grace_time=60
        )
        
        # 更新持久化数据
        self.session_data.setdefault(session_id, {})["next_trigger_time"] = next_trigger_time
        save_session_data_to_file(self.session_data)
        # 修复：优化日志输出，使其更直观
        logger.info(f"[主动消息] 已为会话 {session_id} 安排下一次主动聊天，时间：{run_date.strftime('%Y-%m-%d %H:%M:%S')}。")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=999)
    async def on_private_message(self, event: AstrMessageEvent):
        """
        监听所有私聊消息。
        这是我们重置计时器和计数器的入口。
        """
        # 修复：从新的 "basic_settings" 分组中读取核心配置
        basic_conf = self.config.get("basic_settings", {})
        if not basic_conf.get("enable", False): return
        
        target_user_id = str(basic_conf.get("target_user_id", "")).strip()
        if not target_user_id or event.get_sender_id() != target_user_id:
            return

        # 当目标用户回复时，重置未回复计数器，并重新安排下一次主动聊天
        session_id = event.unified_msg_origin
        self.session_data.setdefault(session_id, {})["unanswered_count"] = 0
        self.session_data[session_id]["last_msg_time"] = time.time()
        await self._schedule_next_chat(session_id)
        logger.info(f"[主动消息] 用户已回复。会话 {session_id} 的未回复计数已重置。")

    async def check_and_chat(self, session_id: str):
        """
        由定时任务触发的核心函数。
        负责检查条件、调用 LLM 并发送消息。
        """
        logger.info(f"[主动消息] 定时任务触发，会话ID: '{session_id}'。")
        try:
            # 修复：从新的 "basic_settings" 分组中读取核心配置
            basic_conf = self.config.get("basic_settings", {})
            schedule_conf = self.config.get("schedule_settings", {})
            
            # 检查插件是否启用和是否处于免打扰时段
            if not basic_conf.get("enable", False) or is_quiet_time(schedule_conf.get("quiet_hours", "1-7"), self.timezone):
                logger.info("[主动消息] 插件被禁用或当前为免打扰时段，跳过本次任务并重新调度。")
                await self._schedule_next_chat(session_id); return

            # 读取当前的未回复次数
            session_info = self.session_data.get(session_id, {})
            unanswered_count = session_info.get("unanswered_count", 0)
            logger.info(f"[主动消息] 开始生成 Prompt，当前未回复次数: {unanswered_count}。")
            
            # 获取当前会话使用的 LLM Provider
            provider = self.context.get_using_provider(umo=session_id)
            if not provider:
                logger.warning(f"[主动消息] 未找到适用于会话 {session_id} 的 LLM Provider，重新调度。")
                await self._schedule_next_chat(session_id); return

            # --- 核心：加载人格和历史 ---
            pure_history_messages = []
            str_history = []
            original_system_prompt = ""

            # 这是我们从 v0.4.9 版本学到的、最可靠的人格加载逻辑
            try:
                # 1. 尝试加载与当前会话绑定的专属人格
                conv_id = await self.context.conversation_manager.get_curr_conversation_id(session_id)
                if conv_id:
                    conversation = await self.context.conversation_manager.get_conversation(session_id, conv_id)
                    if conversation:
                        if conversation.history: str_history = json.loads(conversation.history)
                        if conversation.persona_id:
                            persona = await self.context.persona_manager.get_persona(conversation.persona_id)
                            if persona:
                                original_system_prompt = persona.system_prompt
                                logger.info(f"[主动消息] 已加载会话专属人格 '{persona.persona_id}'。")
                
                # 2. 如果找不到专属人格，则加载全局默认人格作为 fallback
                if not original_system_prompt:
                     default_persona_v3 = await self.context.persona_manager.get_default_persona_v3(umo=session_id)
                     if default_persona_v3:
                        original_system_prompt = default_persona_v3['prompt']
                        logger.info(f"[主动消息] 已加载全局默认人格 '{default_persona_v3['name']}'。")
            except Exception as e:
                logger.warning(f"[主动消息] 获取上下文失败: {e}")
            
            # 如果最终还是没能加载到任何人格，则放弃本次主动聊天
            if not original_system_prompt:
                logger.error("[主动消息] 关键错误：无法加载任何人格设定，放弃本次主动聊天。"); return

            if str_history:
                pure_history_messages = str_history
                logger.info(f"[主动消息] 已载入 {len(pure_history_messages)} 条纯文本历史消息。")

            # --- 核心：构造最终的 Prompt ---
            
            # 终局修复：从新的 "prompt_settings" 分组中读取核心配置
            prompt_conf = self.config.get("prompt_settings", {})
            motivation_template = prompt_conf.get("proactive_prompt", "")
            # 将动态的计数值注入模板
            final_user_simulation_prompt = motivation_template.replace("{{unanswered_count}}", str(unanswered_count))
            logger.info(f"[主动消息] 已生成包含动机的 Prompt。")

            # --- 核心：以正确的“三分离”架构调用 LLM ---
            llm_response_obj = await provider.text_chat(
                prompt=final_user_simulation_prompt, # 模拟的“用户”当前输入
                contexts=pure_history_messages,      # 纯净的历史
                system_prompt=original_system_prompt # 完整的、未被污染的人格
            )
            
            if llm_response_obj and llm_response_obj.completion_text:
                response_text = llm_response_obj.completion_text.strip()
                logger.info(f"[主动消息] LLM 已生成文本: '{response_text}'。")
                
                # --- 核心：使用正确的 API 和健壮的逻辑发送消息 ---
                is_tts_sent = False
                try:
                    # 修复：移除所有语言过滤逻辑，永远勇敢地尝试TTS
                    logger.info("[主动消息] 尝试为所有语言进行手动 TTS。")
                    
                    # 获取 TTS provider
                    tts_provider_or_list = self.context.get_using_tts_provider(umo=session_id)
                    tts_provider = tts_provider_or_list[0] if isinstance(tts_provider_or_list, list) and tts_provider_or_list else tts_provider_or_list
                    
                    if tts_provider:
                        # 调用 TTS 服务
                        audio_path = await tts_provider.get_audio(response_text)
                        if audio_path:
                            # 使用 MessageChain 封装语音组件
                            voice_chain = MessageChain([Record(file=audio_path)])
                            # 使用官方指定的 send_message API 发送
                            await self.context.send_message(session_id, voice_chain)
                            is_tts_sent = True
                            await asyncio.sleep(0.5) # 短暂等待，确保语音和文本消息的顺序
                except Exception as e:
                    # 捕获所有 TTS 相关的异常，记录日志，但不会让程序崩溃
                    logger.error(f"[主动消息] 手动 TTS 流程发生异常: {e}\n{traceback.format_exc()}")
                finally:
                    # 修复：从分组后的配置中正确读取参数
                    tts_conf = self.config.get("tts_settings", {})
                    # 无论 TTS 是否成功，都根据配置决定是否发送原文
                    if not is_tts_sent or tts_conf.get("always_send_text", True):
                        text_chain = MessageChain([Plain(text=response_text)])
                        await self.context.send_message(session_id, text_chain)
                    logger.info(f"[主动消息] 成功！所有消息已发送至 '{session_id}'。")
                
                # --- 核心：修正计数器逻辑 ---
                # 成功发送后，将计数值+1
                self.session_data.setdefault(session_id, {})["unanswered_count"] = unanswered_count + 1
                logger.info(f"[主动消息] 任务成功，未回复次数更新为: {unanswered_count + 1}。")
                await self._schedule_next_chat(session_id)
            else:
                logger.warning("[主动消息] LLM 调用失败或返回空内容，重新调度。")
                await self._schedule_next_chat(session_id)
        except Exception as e:
            logger.error(f"[主动消息] check_and_chat 任务发生致命错误: {e}\n{traceback.format_exc()}")
            await self._schedule_next_chat(session_id)

    async def terminate(self):
        """
        插件被卸载或停用时调用的清理函数。
        """
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown()
        save_session_data_to_file(self.session_data)
        logger.info("主动消息插件已终止。")

