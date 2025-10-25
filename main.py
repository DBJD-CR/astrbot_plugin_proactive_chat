# 文件名: main.py (位于 data/plugins/proactive_chat/ 目录下)
# 版本: 0.9.5 (终局注释版)

# 导入标准库
import random
import time
import traceback
import json
import os
from datetime import datetime
import zoneinfo
import asyncio
import re

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

# 持久化数据文件的路径，用于保存每个会话的定时任务信息
SESSION_DATA_FILE = os.path.join(get_astrbot_data_path(), "proactive_chat_data.json")
# 用于检测 LLM 回复是否包含日语字符的正则表达式
JAPANESE_CHARS_PATTERN = re.compile(r'[\u3040-\u30ff\u30a0-\u30ff]')

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
        logger.error(f"[ProactiveChat] Failed to save session data: {e}")

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
    name="proactive_chat",
    author="DBJD-CR & Gemini-2.5-Pro",
    version="0.9.5",
    desc="一个让机器人能够主动发起私聊的插件，拥有上下文感知、持久化会话、动态情绪、免打扰时段和健壮的TTS集成。"
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
        logger.info("[ProactiveChat] Plugin instance created.")

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
        logger.info("[ProactiveChat] Scheduler initialized.")

    async def _init_jobs_from_data(self):
        """从文件中恢复定时任务。"""
        target_user_id = str(self.config.get("target_user_id", "")).strip()
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
        min_interval = int(self.config.get("min_interval_minutes", 30)) * 60
        max_interval = max(min_interval, int(self.config.get("max_interval_minutes", 90)) * 60)
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
        logger.info(f"[ProactiveChat] Scheduled next chat for {session_id} in {round(random_interval)} seconds.")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=999)
    async def on_private_message(self, event: AstrMessageEvent):
        """
        监听所有私聊消息。
        这是我们重置计时器和计数器的入口。
        """
        if not self.config.get("enable", False): return
        
        target_user_id = str(self.config.get("target_user_id", "")).strip()
        if not target_user_id or event.get_sender_id() != target_user_id:
            return

        # 当目标用户回复时，重置未回复计数器，并重新安排下一次主动聊天
        session_id = event.unified_msg_origin
        self.session_data.setdefault(session_id, {})["unanswered_count"] = 0
        self.session_data[session_id]["last_msg_time"] = time.time()
        await self._schedule_next_chat(session_id)
        logger.info(f"[ProactiveChat] User replied. Unanswered count for {session_id} has been reset.")

    async def check_and_chat(self, session_id: str):
        """
        由定时任务触发的核心函数。
        负责检查条件、调用 LLM 并发送消息。
        """
        logger.info(f"[ProactiveChat] ALARM: Date job triggered for '{session_id}'.")
        try:
            # 检查插件是否启用和是否处于免打扰时段
            if not self.config.get("enable", False) or is_quiet_time(self.config.get("quiet_hours", "0-6"), self.timezone):
                await self._schedule_next_chat(session_id); return

            # 读取当前的未回复次数
            session_info = self.session_data.get(session_id, {})
            unanswered_count = session_info.get("unanswered_count", 0)
            
            # 获取当前会话使用的 LLM Provider
            provider = self.context.get_using_provider(umo=session_id)
            if not provider:
                logger.warning(f"[ProactiveChat] No LLM provider found for session {session_id}. Rescheduling.")
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
                                logger.info(f"[ProactiveChat] Loaded session persona '{persona.persona_id}'.")
                
                # 2. 如果找不到专属人格，则加载全局默认人格作为 fallback
                if not original_system_prompt:
                     default_persona_v3 = await self.context.persona_manager.get_default_persona_v3(umo=session_id)
                     if default_persona_v3:
                        original_system_prompt = default_persona_v3['prompt']
                        logger.info(f"[ProactiveChat] Loaded default persona '{default_persona_v3['name']}'.")
            except Exception as e:
                logger.warning(f"[ProactiveChat] Failed to get context: {e}")
            
            # 如果最终还是没能加载到任何人格，则放弃本次主动聊天
            if not original_system_prompt:
                logger.error("[ProactiveChat] CRITICAL: Could not load any persona. Aborting."); return

            if str_history:
                pure_history_messages = str_history
                logger.info(f"[ProactiveChat] Assembled {len(pure_history_messages)} pure history messages.")

            # --- 核心：构造最终的 Prompt ---
            
            # 从配置中读取用户定义的“动机”模板
            motivation_template = self.config.get("proactive_prompt", "")
            # 将动态的计数值注入模板
            final_user_simulation_prompt = motivation_template.replace("{{unanswered_count}}", str(unanswered_count))
            logger.info(f"[ProactiveChat] Generated immersive prompt for LLM.")

            # --- 核心：以正确的“三分离”架构调用 LLM ---
            llm_response_obj = await provider.text_chat(
                prompt=final_user_simulation_prompt, # 模拟的“用户”当前输入
                contexts=pure_history_messages,      # 纯净的历史
                system_prompt=original_system_prompt # 完整的、未被污染的人格
            )
            
            if llm_response_obj and llm_response_obj.completion_text:
                response_text = llm_response_obj.completion_text.strip()
                logger.info(f"[ProactiveChat] LLM generated text: '{response_text}'.")
                
                # --- 核心：使用正确的 API 和健壮的逻辑发送消息 ---
                is_tts_sent = False
                try:
                    # 如果回复包含日语，则尝试进行 TTS
                    if JAPANESE_CHARS_PATTERN.search(response_text):
                        logger.info("[ProactiveChat] Japanese detected. Attempting manual TTS.")
                        tts_provider_or_list = self.context.get_using_tts_provider(umo=session_id)
                        tts_provider = tts_provider_or_list[0] if isinstance(tts_provider_or_list, list) and tts_provider_or_list else tts_provider_or_list
                        if tts_provider:
                            audio_path = await tts_provider.get_audio(response_text)
                            if audio_path:
                                # 使用 MessageChain 封装语音组件
                                voice_chain = MessageChain([Record(file=audio_path)])
                                # 使用官方指定的 send_message API 发送
                                await self.context.send_message(session_id, voice_chain)
                                is_tts_sent = True
                                await asyncio.sleep(0.5) # 短暂等待，确保语音和文本消息的顺序
                except Exception as e:
                    logger.error(f"[ProactiveChat] Manual TTS process raised an exception: {e}\n{traceback.format_exc()}")
                finally:
                    # 无论 TTS 是否成功，都根据配置决定是否发送原文
                    if not is_tts_sent or self.config.get("always_send_text", True):
                        text_chain = MessageChain([Plain(text=response_text)])
                        await self.context.send_message(session_id, text_chain)
                    logger.info(f"[ProactiveChat] SUCCESS! All messages sent to '{session_id}'.")
                
                # --- 核心：修正计数器逻辑 ---
                # 成功发送后，将计数值+1
                self.session_data.setdefault(session_id, {})["unanswered_count"] = unanswered_count + 1
                await self._schedule_next_chat(session_id)
            else:
                logger.warning("[ProactiveChat] LLM call failed. Rescheduling without incrementing count.")
                await self._schedule_next_chat(session_id)
        except Exception as e:
            logger.error(f"[ProactiveChat] FATAL ERROR in check_and_chat job: {e}\n{traceback.format_exc()}")
            await self._schedule_next_chat(session_id)

    async def terminate(self):
        """
        插件被卸载或停用时调用的清理函数。
        """
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown()
        save_session_data_to_file(self.session_data)
        logger.info("Proactive Chat plugin terminated.")
