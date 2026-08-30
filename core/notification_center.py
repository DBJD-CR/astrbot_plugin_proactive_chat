"""通知中心模块。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import aiofiles
import aiofiles.os as aio_os

from astrbot.api import logger

from ..utils.version import get_plugin_version


class NotificationCenter:
    """负责远端通知拉取、本地缓存与已读状态维护。"""

    def __init__(self, plugin: Any):
        # plugin 为主插件实例；通知中心通过它访问配置、数据目录与 Web 广播能力。
        self.plugin = plugin
        # 保留配置引用，便于读取通知系统开关与轮询间隔。
        self.config = plugin.config
        # 通知缓存与已读状态复用插件数据目录，不额外引入数据库依赖。
        self.data_dir = Path(plugin.data_dir)
        self.cache_file = self.data_dir / "notifications_cache.json"
        # 单独的异步锁用于保护通知缓存读写，避免与会话数据锁互相耦合。
        self._lock = asyncio.Lock()
        # 后台轮询任务句柄，插件停止时需要显式取消。
        self._poll_task: asyncio.Task | None = None
        # 防止手动刷新与定时轮询同时发起远端请求，造成重复写缓存。
        self._sync_in_progress = False
        self._sync_state_lock = asyncio.Lock()
        # 统一缓存结构：同步时间、远端通知列表、本地已读映射。
        self._cache: dict[str, Any] = {
            "last_sync_at": None,
            "items": [],
            "read_map": {},
        }

    # 当前通知平台基础地址硬编码，不暴露到用户配置页。
    NOTIFICATION_BASE_URL = "https://plugincenter.aloys23.link"
    # 当前插件对应的 APP_SLUG，同样按常量维护，减少误改风险。
    NOTIFICATION_APP_SLUG = "f1b3592e-be98-4398-a3be-596e4a1ea90f"

    def _get_settings(self) -> dict[str, Any]:
        # 只读取 notification_settings 中允许用户调整的轻量项，如启用状态与轮询间隔。
        return dict(self.config.get("notification_settings", {}))

    def is_enabled(self) -> bool:
        settings = self._get_settings()
        # 通知系统默认开启；只有用户明确关闭时才停用轮询与同步逻辑。
        return bool(settings.get("enabled", True))

    async def _build_remote_url(self) -> str:
        # 统一在这里拼接远端接口地址，后续若路径变更，只需修改这一处。
        base_url = self.NOTIFICATION_BASE_URL.strip().rstrip("/")
        app_slug = self.NOTIFICATION_APP_SLUG.strip().strip("/")
        if not base_url or not app_slug:
            return ""

        plugin_version = await self._get_plugin_version()
        query = urlencode({"plugin_version": plugin_version})
        return f"{base_url}/api/v1/{app_slug}/notifications/updates?{query}"

    async def _get_plugin_version(self) -> str:
        # 远端通知接口要求携带插件版本；统一复用版本工具并去掉 v 前缀。
        plugin_version = (
            getattr(self.plugin, "version", None)
            or getattr(self.plugin, "__version__", None)
            or get_plugin_version(default="0.0.0", strip_v_prefix=True)
        )
        normalized = str(plugin_version).strip().lstrip("vV")
        return normalized or "0.0.0"

    def _get_poll_interval_seconds(self) -> int:
        settings = self._get_settings()
        value = settings.get("poll_interval_seconds", 300)
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            seconds = 300
        # 对轮询间隔设置下限，避免异常配置把远端服务打爆。
        return max(30, seconds)

    async def load_cache(self) -> None:
        async with self._lock:
            await self._load_cache_locked()

    async def _load_cache_locked(self) -> None:
        # 首次启动或尚未产生缓存文件时，直接使用空缓存结构。
        try:
            await aio_os.stat(self.cache_file)
        except FileNotFoundError:
            self._cache = {
                "last_sync_at": None,
                "items": [],
                "read_map": {},
            }
            return

        try:
            async with aiofiles.open(self.cache_file, encoding="utf-8") as f:
                content = await f.read()
            payload = await asyncio.to_thread(json.loads, content)
            if not isinstance(payload, dict):
                raise ValueError("通知缓存文件格式无效")
            self._cache = {
                "last_sync_at": payload.get("last_sync_at"),
                "items": payload.get("items")
                if isinstance(payload.get("items"), list)
                else [],
                "read_map": payload.get("read_map")
                if isinstance(payload.get("read_map"), dict)
                else {},
            }
        except Exception as e:
            logger.warning(f"[主动消息] 读取通知缓存失败喵: {e}，将使用空缓存继续。")
            self._cache = {
                "last_sync_at": None,
                "items": [],
                "read_map": {},
            }

    async def save_cache(self) -> None:
        async with self._lock:
            await self._save_cache_locked()

    async def _save_cache_locked(self) -> None:
        try:
            # 每次写缓存前都确保数据目录存在，兼容首次运行或目录被清理的场景。
            await aio_os.makedirs(self.data_dir, exist_ok=True)
            async with aiofiles.open(self.cache_file, "w", encoding="utf-8") as f:
                content = await asyncio.to_thread(
                    json.dumps, self._cache, indent=4, ensure_ascii=False
                )
                await f.write(content)
        except Exception as e:
            logger.warning(f"[主动消息] 保存通知缓存失败喵: {e}")

    def _normalize_item(self, raw: Any) -> dict[str, Any] | None:
        # 远端接口理论上返回对象数组；命中异常项时直接忽略，避免污染缓存。
        if not isinstance(raw, dict):
            return None

        required_keys = {"id", "title", "content", "type", "created_at", "is_active"}
        if not required_keys.issubset(raw.keys()):
            return None

        try:
            notification_id = int(raw.get("id"))
        except (TypeError, ValueError):
            return None

        title = str(raw.get("title", "")).strip()
        content = str(raw.get("content", "")).strip()
        notification_type = str(raw.get("type", "")).strip().upper()
        created_at = str(raw.get("created_at", "")).strip()
        is_active = raw.get("is_active")
        content_format = str(raw.get("content_format", "text")).strip().lower()
        if content_format in {"plain", "plaintext"}:
            content_format = "text"
        if content_format not in {"text", "markdown"}:
            content_format = "text"

        if (
            not title
            or not content
            or not notification_type
            or not isinstance(is_active, bool)
        ):
            return None

        try:
            normalized_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return None

        return {
            "id": notification_id,
            "app_id": raw.get("app_id"),
            "title": title,
            "content": content,
            "type": notification_type,
            "created_at": normalized_dt.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "is_active": is_active,
            "content_format": content_format,
        }

    def _sort_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            items,
            key=lambda item: (item.get("created_at", ""), item.get("id", 0)),
            reverse=True,
        )

    async def _fetch_remote_items(self) -> list[dict[str, Any]]:
        # 抽离单独的拉取函数，便于复用于“启动立即同步”和“手动刷新”。
        url = await self._build_remote_url()
        if not url:
            return []

        def _request() -> list[dict[str, Any]]:
            # 某些站点会对“过于裸”的默认 Python 请求做拦截，因此这里补齐常见请求头，
            # 让通知拉取行为更接近正常浏览器 / 前端 fetch 请求，降低被 403 拒绝的概率。
            request = Request(
                url,
                method="GET",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/133.0.0.0 Safari/537.36"
                    ),
                },
            )
            with urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
            payload = json.loads(body)
            # 规范要求返回体必须是数组；若格式变化，直接抛错给上层处理。
            if not isinstance(payload, list):
                raise ValueError("通知接口返回体不是数组")
            return payload

        try:
            raw_items = await asyncio.to_thread(_request)
        except HTTPError as e:
            raise RuntimeError(f"通知接口请求失败，HTTP {e.code}") from e
        except URLError as e:
            raise RuntimeError(f"通知接口连接失败: {e.reason}") from e
        except TimeoutError as e:
            raise RuntimeError("通知接口请求超时") from e

        normalized_items = []
        for raw in raw_items:
            item = self._normalize_item(raw)
            if not item:
                continue
            if not item.get("is_active", False):
                continue
            normalized_items.append(item)
        return self._sort_items(normalized_items)

    def _items_signature(self, items: list[dict[str, Any]]) -> str:
        try:
            return json.dumps(items, ensure_ascii=False, sort_keys=True)
        except TypeError as e:
            logger.warning(f"[主动消息] 创建通知签名时遇到不可序列化的项喵: {e}")
            return str(items)

    def _build_meta_locked(self) -> dict[str, Any]:
        # 元信息由缓存即时计算，不单独冗余存储，降低一致性维护成本。
        unread_count = 0
        read_map = self._cache.get("read_map", {})
        for item in self._cache.get("items", []):
            if not read_map.get(str(item.get("id")), False):
                unread_count += 1
        return {
            "unread_count": unread_count,
            "last_sync_at": self._cache.get("last_sync_at"),
            "total_count": len(self._cache.get("items", [])),
        }

    async def get_meta(self) -> dict[str, Any]:
        async with self._lock:
            # 仅返回通知元信息，供轻量广播路径复用，避免额外构造完整 items 列表。
            return self._build_meta_locked()

    async def get_payload(self) -> dict[str, Any]:
        async with self._lock:
            read_map = self._cache.get("read_map", {})
            # 在对前端出参时动态补齐 _read，保证缓存源数据依然保持贴近远端结构。
            items = [
                {
                    **item,
                    "_read": bool(read_map.get(str(item.get("id")), False)),
                }
                for item in self._cache.get("items", [])
            ]
            return {
                "items": items,
                "meta": self._build_meta_locked(),
            }

    async def mark_as_read(self, notification_id: int) -> dict[str, Any]:
        async with self._lock:
            # 已读映射以字符串化 ID 作为 key，便于 JSON 序列化后保持稳定。
            self._cache.setdefault("read_map", {})[str(notification_id)] = True
            await self._save_cache_locked()
            return {
                "ok": True,
                "id": notification_id,
                "meta": self._build_meta_locked(),
            }

    async def mark_all_as_read(self) -> dict[str, Any]:
        async with self._lock:
            read_map = self._cache.setdefault("read_map", {})
            for item in self._cache.get("items", []):
                read_map[str(item.get("id"))] = True
            await self._save_cache_locked()
            return {
                "ok": True,
                "meta": self._build_meta_locked(),
            }

    async def refresh(self) -> bool:
        # 若已有同步在跑，则直接跳过，避免多协程重复覆盖缓存。
        async with self._sync_state_lock:
            if self._sync_in_progress:
                return False
            self._sync_in_progress = True

        try:
            remote_items = await self._fetch_remote_items()
            async with self._lock:
                old_signature = self._items_signature(self._cache.get("items", []))
                new_signature = self._items_signature(remote_items)
                changed = old_signature != new_signature

                current_read_map = self._cache.setdefault("read_map", {})
                active_ids = {str(item.get("id")) for item in remote_items}
                # 同步完成后顺手裁剪掉已经不存在的已读记录，避免 read_map 无限制膨胀。
                self._cache["read_map"] = {
                    key: value
                    for key, value in current_read_map.items()
                    if key in active_ids
                }
                self._cache["items"] = remote_items
                self._cache["last_sync_at"] = (
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                )
                await self._save_cache_locked()
                return changed
        except Exception as e:
            logger.warning(f"[主动消息] 同步远端通知失败喵: {e} (可忽略)")
            return False
        finally:
            async with self._sync_state_lock:
                self._sync_in_progress = False

    async def start(self) -> None:
        # 启动时先恢复本地缓存，确保前端即便远端短暂不可达也能看到历史通知。
        await self.load_cache()
        if not self.is_enabled():
            logger.info("[主动消息] 通知系统未启用或配置不完整喵。")
            return

        changed = await self.refresh()
        if getattr(self.plugin, "web_admin_server", None):
            if changed:
                await self.plugin.web_admin_server._broadcast_update("notifications")
            else:
                await self.plugin.web_admin_server._broadcast_notification_meta_update(
                    "notifications-meta"
                )

        async def _poll_loop() -> None:
            while True:
                try:
                    # 采用 sleep + refresh 的简单轮询模型即可满足一期通知同步需求。
                    await asyncio.sleep(self._get_poll_interval_seconds())
                    changed = await self.refresh()
                    if getattr(self.plugin, "web_admin_server", None):
                        if changed:
                            # 仅在通知内容变化时推送完整通知载荷，避免无意义的大包重传。
                            await self.plugin.web_admin_server._broadcast_update(
                                "notifications"
                            )
                        else:
                            # 内容未变化时只同步元信息，确保“上次同步”时间标签自动刷新。
                            await self.plugin.web_admin_server._broadcast_notification_meta_update(
                                "notifications-meta"
                            )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"[主动消息] 通知轮询任务异常喵: {e} (可忽略)")

        self._poll_task = asyncio.create_task(_poll_loop())
        logger.info("[主动消息] 通知系统已启动喵。")

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except Exception:
                pass
            self._poll_task = None
        await self.save_cache()
        logger.info("[主动消息] 通知系统已停止喵。")
