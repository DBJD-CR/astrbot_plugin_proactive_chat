"""语境感知主动消息调度计划器。"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger


class ContextualSchedulePlanner:
    """根据最近消息语境生成下一次主动消息调度计划。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin

    def _get_schedule_bounds(self, schedule_conf: dict) -> tuple[int, int]:
        min_interval = int(schedule_conf.get("min_interval_minutes", 30)) * 60
        max_interval = max(
            min_interval, int(schedule_conf.get("max_interval_minutes", 900)) * 60
        )
        return min_interval, max_interval

    def _clamp_interval(
        self,
        seconds: int | float,
        min_interval: int,
        max_interval: int,
    ) -> int:
        try:
            value = int(seconds)
        except Exception:
            value = min_interval
        return max(min_interval, min(value, max_interval))

    def _get_settings(self, schedule_conf: dict) -> dict[str, Any]:
        enabled = schedule_conf.get("enable_contextual_timing", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"0", "false", "off", "no"}
        else:
            enabled = bool(enabled)

        try:
            history_count = int(schedule_conf.get("contextual_timing_history_count", 8))
        except Exception:
            history_count = 8
        history_count = max(1, min(history_count, 30))

        try:
            llm_timeout_seconds = int(
                schedule_conf.get("contextual_timing_llm_timeout_seconds", 15)
            )
        except Exception:
            llm_timeout_seconds = 15
        llm_timeout_seconds = max(3, min(llm_timeout_seconds, 60))

        return {
            "enabled": enabled,
            "history_count": history_count,
            "llm_timeout_seconds": llm_timeout_seconds,
        }

    def _normalize_text(self, text: Any) -> str:
        return " ".join(str(text or "").strip().lower().split())

    def _contains_marker(self, normalized_text: str, marker: str) -> bool:
        marker = self._normalize_text(marker)
        if not marker:
            return False
        if marker.isascii() and any(ch.isalpha() for ch in marker):
            pattern = rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])"
            return re.search(pattern, normalized_text) is not None
        return marker in normalized_text

    def _pick_jitter(self, minutes_min: int, minutes_max: int) -> int:
        lower = max(1, int(minutes_min)) * 60
        upper = max(lower, int(minutes_max) * 60)
        return random.randint(lower, upper)

    def _seconds_until_next_local_time(
        self,
        hour: int,
        minute: int = 0,
        *,
        force_next_day: bool = False,
    ) -> int:
        now = datetime.now(self.plugin.timezone)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if force_next_day:
            target = target + timedelta(days=1)
        if target.timestamp() <= now.timestamp():
            target = target + timedelta(days=1)
        return max(60, int(target.timestamp() - now.timestamp()))

    def _build_llm_prompt(
        self,
        texts: list[str],
        min_interval: int,
        max_interval: int,
    ) -> str:
        min_minutes = max(1, int(min_interval // 60))
        max_minutes = max(min_minutes, int(max_interval // 60))
        now_text = datetime.now(self.plugin.timezone).strftime("%Y-%m-%d %H:%M:%S")
        recent_lines = []
        for index, text in enumerate(texts[:12], start=1):
            cleaned = " ".join(str(text or "").split())
            if len(cleaned) > 500:
                cleaned = cleaned[:500] + "..."
            recent_lines.append(f"{index}. {cleaned}")

        return (
            "你正在为主动消息插件判断下一次主动开口的时间。\n"
            "请只根据最近用户侧消息判断：用户是否表达了明确的稍后、忙碌、休息、睡觉、明天、会议、通勤、吃饭、看电影等时间语境。\n"
            "如果没有明确时间语境，请把 delay_minutes 设为 null。\n"
            f"当前本地时间：{now_text}\n"
            f"允许的触发间隔范围：{min_minutes} 到 {max_minutes} 分钟。\n"
            "输出必须是严格 JSON，不要 Markdown，不要解释。格式：\n"
            '{"delay_minutes": 120, "reason": "用户表示稍后再聊", "confidence": 0.75}\n'
            '或 {"delay_minutes": null, "reason": "没有明确时间语境", "confidence": 0}\n'
            "delay_minutes 必须在允许范围内；confidence 为 0 到 1。\n"
            "最近用户侧消息：\n"
            + "\n".join(recent_lines)
        )

    def _extract_llm_json(self, response_text: str) -> dict | None:
        text = str(response_text or "").strip()
        if not text:
            return None

        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
        if fence_match:
            text = fence_match.group(1).strip()
        elif "{" in text and "}" in text:
            text = text[text.find("{") : text.rfind("}") + 1]

        try:
            parsed = json.loads(text)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _parse_llm_result(
        self,
        response_text: str,
        min_interval: int,
        max_interval: int,
    ) -> dict[str, Any] | None:
        parsed = self._extract_llm_json(response_text)
        if not parsed:
            return None

        delay_minutes = parsed.get("delay_minutes")
        if delay_minutes in (None, "", False):
            return None

        try:
            delay_seconds = float(delay_minutes) * 60
        except Exception:
            return None

        try:
            confidence = float(parsed.get("confidence", 0))
        except Exception:
            confidence = 0
        if confidence <= 0:
            return None

        reason = str(parsed.get("reason") or "llm_context").strip()
        if len(reason) > 80:
            reason = reason[:80]

        return {
            "interval_seconds": self._clamp_interval(
                delay_seconds,
                min_interval,
                max_interval,
            ),
            "strategy": "contextual",
            "rule": "llm_context",
            "reason": f"context:llm:{reason}",
        }

    async def _predict_with_llm(
        self,
        session_id: str,
        texts: list[str],
        min_interval: int,
        max_interval: int,
        timeout_seconds: int,
    ) -> dict[str, Any] | None:
        if not texts:
            return None

        context = getattr(self.plugin, "context", None)
        if context is None:
            return None

        prompt = self._build_llm_prompt(texts, min_interval, max_interval)
        system_prompt = (
            "你是主动消息插件的调度判断器，只输出可被 json.loads 解析的 JSON。"
        )

        async def _call_llm() -> str | None:
            llm_response_obj = None
            try:
                provider_id = await context.get_current_chat_provider_id(session_id)
                llm_response_obj = await context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    contexts=[],
                    system_prompt=system_prompt,
                )
            except Exception as new_api_error:
                logger.debug(
                    f"[主动消息] 语境调度 LLM 新接口失败，尝试传统接口喵: {new_api_error}"
                )
                try:
                    provider = context.get_using_provider(umo=session_id)
                    if provider:
                        llm_response_obj = await provider.text_chat(
                            prompt=prompt,
                            contexts=[],
                            system_prompt=system_prompt,
                        )
                except Exception as fallback_error:
                    logger.debug(
                        f"[主动消息] 语境调度 LLM 传统接口也失败喵: {fallback_error}"
                    )
                    return None

            completion_text = getattr(llm_response_obj, "completion_text", None)
            if not completion_text:
                return None
            return str(completion_text).strip()

        try:
            response_text = await asyncio.wait_for(
                _call_llm(),
                timeout=max(3, int(timeout_seconds)),
            )
        except asyncio.TimeoutError:
            logger.debug("[主动消息] 语境调度 LLM 判断超时，回退到规则判断喵。")
            return None
        except Exception as e:
            logger.debug(f"[主动消息] 语境调度 LLM 判断失败，回退到规则判断喵: {e}")
            return None

        if not response_text:
            return None

        prediction = self._parse_llm_result(response_text, min_interval, max_interval)
        if prediction:
            logger.debug(
                f"[主动消息] 语境调度 LLM 命中喵: {prediction.get('reason', '')}"
            )
        return prediction

    def _predict_from_text(
        self,
        text: str,
        min_interval: int,
        max_interval: int,
    ) -> dict[str, Any] | None:
        normalized = self._normalize_text(text)
        if not normalized:
            return None

        explicit_minutes = self._extract_explicit_delay_minutes(normalized)
        if explicit_minutes is not None:
            seconds = self._clamp_interval(
                explicit_minutes * 60, min_interval, max_interval
            )
            return {
                "interval_seconds": seconds,
                "strategy": "contextual",
                "rule": "explicit_delay",
                "reason": f"context:explicit_delay:{explicit_minutes}m",
            }

        tomorrow_markers = ("明天", "明早", "明日", "tomorrow")
        if any(self._contains_marker(normalized, marker) for marker in tomorrow_markers):
            target_hour = 8 if (
                "明早" in normalized or self._contains_marker(normalized, "morning")
            ) else 10
            seconds = self._seconds_until_next_local_time(
                target_hour,
                random.randint(0, 45),
                force_next_day=True,
            )
            seconds = self._clamp_interval(seconds, min_interval, max_interval)
            return {
                "interval_seconds": seconds,
                "strategy": "contextual",
                "rule": "tomorrow",
                "reason": "context:tomorrow",
            }

        rules: list[tuple[str, tuple[str, ...], tuple[int, int]]] = [
            (
                "do_not_disturb",
                ("勿扰", "别打扰", "不要打扰", "别找", "先别", "别发", "别吵", "do not disturb", "dnd"),
                (240, 480),
            ),
            (
                "sleep_night",
                ("晚安", "睡了", "睡觉", "先睡", "要睡", "困了", "good night", "gn", "sleep", "bed"),
                (420, 600),
            ),
            (
                "movie",
                ("看电影", "电影", "影院", "观影", "追剧", "看剧", "movie", "cinema"),
                (120, 180),
            ),
            (
                "meeting_or_class",
                ("开会", "会议", "上课", "考试", "面试", "在忙", "忙完", "工作", "meeting", "class", "exam"),
                (90, 180),
            ),
            (
                "commute",
                ("路上", "开车", "地铁", "公交", "通勤", "高铁", "火车", "飞机", "driving", "commute"),
                (45, 120),
            ),
            (
                "meal",
                ("吃饭", "午饭", "晚饭", "早饭", "做饭", "外卖", "吃完", "lunch", "dinner", "breakfast"),
                (45, 90),
            ),
            ("shower", ("洗澡", "洗头", "冲澡", "shower"), (30, 60)),
            (
                "game",
                ("打游戏", "游戏", "开一把", "排位", "game", "gaming"),
                (60, 150),
            ),
            (
                "short_later",
                ("等会", "等一下", "一会", "待会", "稍后", "马上", "later", "brb"),
                (20, 45),
            ),
        ]

        for rule, markers, minute_range in rules:
            if any(self._contains_marker(normalized, marker) for marker in markers):
                seconds = self._pick_jitter(*minute_range)
                seconds = self._clamp_interval(seconds, min_interval, max_interval)
                return {
                    "interval_seconds": seconds,
                    "strategy": "contextual",
                    "rule": rule,
                    "reason": f"context:{rule}",
                }

        return None

    def _extract_explicit_delay_minutes(self, text: str) -> int | None:
        if "半小时" in text or "半个小时" in text:
            return 30

        minute_match = re.search(
            r"(?<![\d.])(\d{1,3})(?![\d.])\s*(分钟|分|mins?|minutes?)\s*(后|later)?",
            text,
        )
        if minute_match:
            value = int(minute_match.group(1))
            if 1 <= value <= 1440:
                return value

        hour_match = re.search(
            r"(?<![\d.])(\d{1,2})(?![\d.])\s*(个)?\s*(小时|钟头|hours?|hrs?|h)\s*(后|later)?",
            text,
        )
        if hour_match:
            value = int(hour_match.group(1))
            if 1 <= value <= 48:
                return value * 60

        return None

    async def _collect_texts(self, session_id: str, history_count: int) -> list[str]:
        texts: list[str] = []
        async with self.plugin.data_lock:
            raw_temp_state = getattr(self.plugin, "session_temp_state", {}).get(
                session_id, {}
            )
            temp_state = (
                dict(raw_temp_state) if isinstance(raw_temp_state, dict) else {}
            )
        if isinstance(temp_state, dict):
            last_text = temp_state.get("last_user_text")
            if last_text:
                texts.append(str(last_text))

        load_records = getattr(self.plugin, "_load_platform_message_history_records", None)
        extract_text = getattr(self.plugin, "_extract_platform_message_text", None)
        is_bot_record = getattr(self.plugin, "_is_platform_bot_record", None)
        if callable(load_records) and callable(extract_text):
            try:
                records, _count = await load_records(session_id, history_count)
            except Exception as e:
                logger.debug(
                    f"[主动消息] 读取语境调度历史失败，回退到本地最近消息喵: {e}"
                )
                records = []

            for record in reversed(list(records or [])):
                try:
                    if callable(is_bot_record) and is_bot_record(record):
                        continue
                    content = (
                        record.get("content")
                        if isinstance(record, dict)
                        else getattr(record, "content", None)
                    )
                    text = extract_text(content)
                    if text:
                        texts.append(str(text))
                except Exception:
                    continue

        deduped: list[str] = []
        seen: set[str] = set()
        for item in texts:
            normalized = self._normalize_text(item)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(item)
        return deduped[:history_count]

    async def build_plan(self, session_id: str, session_config: dict) -> dict[str, Any]:
        schedule_conf = session_config.get("schedule_settings", {})
        min_interval, max_interval = self._get_schedule_bounds(schedule_conf)
        random_interval = random.randint(min_interval, max_interval)
        plan: dict[str, Any] = {
            "interval_seconds": random_interval,
            "min_interval_seconds": min_interval,
            "max_interval_seconds": max_interval,
            "strategy": "random",
            "reason": "random:fallback",
            "rule": "",
            "source": "random_interval",
        }

        contextual = self._get_settings(schedule_conf)
        if contextual["enabled"]:
            texts = await self._collect_texts(session_id, contextual["history_count"])
            llm_prediction = await self._predict_with_llm(
                session_id,
                texts,
                min_interval,
                max_interval,
                contextual["llm_timeout_seconds"],
            )
            if llm_prediction:
                plan.update(llm_prediction)
                plan["source"] = "llm_context"
            else:
                for item in texts:
                    prediction = self._predict_from_text(
                        item,
                        min_interval,
                        max_interval,
                    )
                    if prediction:
                        plan.update(prediction)
                        plan["source"] = "recent_context_fallback"
                        break

        scheduled_at = time.time()
        next_trigger_time = scheduled_at + int(plan["interval_seconds"])
        plan["scheduled_at"] = scheduled_at
        plan["next_trigger_time"] = next_trigger_time
        plan["run_date"] = datetime.fromtimestamp(
            next_trigger_time, tz=self.plugin.timezone
        )
        return plan
