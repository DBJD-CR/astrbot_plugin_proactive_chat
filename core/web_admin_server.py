"""主动消息插件 Web 管理端服务。"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from astrbot.api import logger

from ..utils.version import get_plugin_version

try:
    # Web 管理端完全基于 FastAPI / Uvicorn 提供 HTTP 与 WebSocket 能力。
    import uvicorn
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    FASTAPI_AVAILABLE = True
except ImportError:
    # 允许插件主体在缺少 FastAPI 依赖时继续工作，只是禁用 Web 控制台。
    FASTAPI_AVAILABLE = False
    logger.warning(
        "[主动消息] FastAPI 未安装喵，Web 管理端不可用喵。请安装: pip install fastapi uvicorn"
    )

try:
    from quart import request as quart_request
except ImportError:
    quart_request = None


def _patch_starlette_router_startup_kwargs() -> None:
    """兼容 FastAPI 与较新 Starlette Router 的启动参数签名差异。"""
    try:
        from starlette.routing import Router
    except Exception as e:
        logger.debug(f"[主动消息] 检查 Starlette Router 兼容性失败喵: {e}")
        return

    init = Router.__init__
    if getattr(init, "_proactive_chat_startup_patch", False):
        return

    try:
        params = inspect.signature(init).parameters
    except (TypeError, ValueError) as e:
        logger.debug(f"[主动消息] 读取 Starlette Router 签名失败喵: {e}")
        return

    unsupported = {name for name in ("on_startup", "on_shutdown") if name not in params}
    if not unsupported:
        return

    def patched_init(self, *args, **kwargs):
        for name in unsupported:
            kwargs.pop(name, None)
        return init(self, *args, **kwargs)

    patched_init._proactive_chat_startup_patch = True
    Router.__init__ = patched_init
    logger.info("[主动消息] 已应用 FastAPI / Starlette Router 启动参数兼容补丁喵。")


def _is_running_in_docker() -> bool:
    """检测当前进程是否运行在 Docker / 容器环境中。"""
    # /.dockerenv 是最常见的容器特征文件，若存在可直接判定为容器环境。
    if os.path.exists("/.dockerenv"):
        return True

    try:
        cgroup_path = Path("/proc/self/cgroup")
        if cgroup_path.exists():
            # Linux 容器通常会在 cgroup 信息中暴露 docker / kubepods 等路径片段。
            content = cgroup_path.read_text(encoding="utf-8", errors="ignore")
            if "/docker/" in content or "/kubepods/" in content:
                return True
    except Exception:
        # 环境探测失败时宁可保守忽略，不影响主流程。
        pass

    # 额外兼容某些自定义镜像通过环境变量主动标记容器场景的做法。
    return os.environ.get("DOCKER_CONTAINER") == "true"


class WebAdminServer:
    """主动消息插件 Web 管理端服务器。"""

    ASTRBOT_PLUGIN_NAME = "astrbot_plugin_proactive_chat"
    ASTRBOT_PAGE_API_ENDPOINT = "dashboard"
    ASTRBOT_PAGE_API_PATH = f"/{ASTRBOT_PLUGIN_NAME}/{ASTRBOT_PAGE_API_ENDPOINT}"

    def __init__(self, plugin: Any):
        # plugin 是主插件实例，Web 端所有状态与操作都通过它间接访问。
        self.plugin = plugin
        # 直接缓存配置引用，便于路由中统一读写。
        self.config = plugin.config
        self._native_config = self.config if hasattr(self.config, "save_config") else None
        # FastAPI 应用实例，仅在依赖存在且初始化成功时设置。
        self.app: FastAPI | None = None
        # Uvicorn Server 实例，用于控制启动与停止。
        self.server = None
        # 后台运行的 serve 任务，stop 时需要等待其退出。
        self.server_task: asyncio.Task | None = None
        # 定时清理过期 token 的后台任务。
        self._token_cleanup_task: asyncio.Task | None = None
        # 当前已建立的 WebSocket 连接列表，用于广播 UI 更新。
        self._ws_connections: list[WebSocket] = []
        # 登录令牌默认有效期 24 小时。
        self._token_expire_seconds = 60 * 60 * 24
        # 简单的内存令牌表：token -> 过期时间戳。
        self._tokens: dict[str, float] = {}
        self._background_tasks: set[asyncio.Task] = set()
        # 仅当配置中设置了密码时才开启鉴权。
        self._auth_enabled = bool(self.config.get("web_admin", {}).get("password", ""))
        # 缓存插件版本，避免在高频状态轮询与广播中重复读取文件。
        self._metadata_version = get_plugin_version(default="未知版本")
        # 标记 Web 管理端当前是否可用，便于启动阶段做更精确的降级判断。
        self._web_admin_available = False
        # 记录最近一次初始化失败原因，便于日志诊断依赖冲突或运行环境问题。
        self._web_admin_init_error: str | None = None

        if FASTAPI_AVAILABLE:
            # 只有环境具备依赖时才尝试构建 Web 应用；若构建失败则降级禁用 Web 端，不影响插件主体。
            try:
                self._setup_app()
                self._web_admin_available = self.app is not None
            except Exception as e:
                self.app = None
                self._web_admin_available = False
                self._web_admin_init_error = str(e)
                logger.error(
                    "[主动消息] Web 管理端初始化失败喵，已自动禁用，不影响插件主体功能。"
                    f" 可能是 FastAPI / Pydantic 依赖版本不兼容: {e}"
                )

    def _decode_route_umo(self, umo: str) -> str:
        """Decode URL-encoded UMO path parameters from Pages/Web API routes."""
        try:
            return unquote(str(umo or ""))
        except Exception:
            return str(umo or "")

    def _track_background_task(self, task: asyncio.Task) -> None:
        track_task = getattr(self.plugin, "_track_task", None)
        if callable(track_task):
            track_task(task)
            return
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def register_astrbot_page_api(self) -> None:
        """向 AstrBot WebUI 插件 Page 暴露完整管理端接口。"""
        context = getattr(self.plugin, "context", None)
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            logger.debug(
                "[主动消息] 当前 AstrBot 版本未提供 register_web_api，跳过插件卡片接口注册喵。"
            )
            return

        routes = (
            ("dashboard", self.build_astrbot_page_payload, ["GET"]),
            ("embedded-dashboard", self.build_astrbot_page_payload, ["GET"]),
            ("auth-info", self._page_auth_info, ["GET"]),
            ("login", self._page_login, ["POST"]),
            ("status", self._page_status, ["GET"]),
            ("config", self._page_config, ["GET", "POST"]),
            ("config-save", self._page_update_config, ["POST"]),
            ("get_config", self._page_get_config, ["GET"]),
            ("save_config", self._page_save_config, ["POST"]),
            ("config-schema", self._page_get_config_schema, ["GET"]),
            ("session-config/sessions", self._page_list_session_configs, ["GET"]),
            ("session-config/<path:umo>", self._page_session_config, ["GET", "POST"]),
            (
                "session-config-save/<path:umo>",
                self._page_update_session_config,
                ["POST"],
            ),
            (
                "session-config-delete/<path:umo>",
                self._page_reset_session_config,
                ["POST"],
            ),
            ("jobs", self._page_list_jobs, ["GET"]),
            ("jobs/<path:umo>/reschedule", self._page_reschedule_job, ["POST"]),
            ("jobs/<path:umo>/trigger", self._page_trigger_job, ["POST"]),
            ("jobs-cancel/<path:umo>", self._page_cancel_job, ["POST"]),
        )

        registered_count = 0
        for endpoint, handler, methods in routes:
            path = f"/{self.ASTRBOT_PLUGIN_NAME}/{endpoint}"
            if self._register_astrbot_page_route(register, path, handler, methods):
                registered_count += 1

        logger.info(
            f"[主动消息] 已注册 AstrBot 插件卡片管理端接口 {registered_count}/{len(routes)} 个喵。"
        )

    def _register_astrbot_page_route(
        self,
        register,
        path: str,
        handler,
        methods: list[str],
    ) -> bool:
        """兼容不同 AstrBot 版本的 register_web_api 签名。"""
        attempts = (
            lambda: register(path, handler, methods, "Proactive chat WebUI API"),
            lambda: register(path, handler, methods=methods),
            lambda: register(methods[0], path, handler),
            lambda: register(path, handler),
        )

        last_error: Exception | None = None
        for attempt in attempts:
            try:
                attempt()
                return True
            except TypeError as e:
                last_error = e
                continue
            except Exception as e:
                logger.warning(
                    f"[主动消息] 注册 AstrBot 插件卡片接口失败喵: {path}: {e}"
                )
                return False

        if last_error:
            logger.debug(
                f"[主动消息] AstrBot 插件卡片接口签名不兼容，已跳过 {path} 喵: {last_error}"
            )
        return False

    async def _read_astrbot_page_json(self) -> dict[str, Any]:
        """Read JSON sent through AstrBot Pages' Quart-based plugin API bridge."""
        if quart_request is None:
            return {}

        payload = None
        for kwargs in ({"silent": True}, {"force": True}, {}):
            try:
                payload = await quart_request.get_json(**kwargs)
                if payload is not None:
                    break
            except TypeError:
                continue
            except Exception:
                continue
        return self._unwrap_page_payload(payload)

    def _unwrap_page_payload(self, payload: Any) -> dict[str, Any]:
        """Unwrap bridge envelopes such as {"body": {...}} without changing normal JSON."""
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return {}
        if not isinstance(payload, dict):
            return {}

        for key in ("body", "data", "payload"):
            if set(payload.keys()) != {key}:
                continue
            nested = payload.get(key)
            if isinstance(nested, str):
                try:
                    nested = json.loads(nested)
                except Exception:
                    return {}
            if isinstance(nested, dict):
                return nested
        return payload

    def _get_astrbot_page_method(self) -> str:
        if quart_request is None:
            return "GET"
        return str(getattr(quart_request, "method", "GET") or "GET").upper()

    async def _page_auth_info(self):
        return {"auth_required": False}

    async def _page_login(self):
        return {"token": "page-bridge", "auth_required": False}

    async def _page_status(self):
        return self._build_status_payload()

    async def _page_get_config(self):
        return self._build_config_payload()

    async def _page_update_config(self):
        payload = await self._read_astrbot_page_json()
        return await self._apply_config_payload(payload)

    async def _page_save_config(self):
        payload = await self._read_astrbot_page_json()
        logger.info(
            f"[主动消息] Pages save_config 收到保存请求喵，字段: {sorted(payload.keys())}"
        )
        result = await self._apply_config_payload(payload)
        if not result.get("ok", False):
            return {
                "success": False,
                "error": result.get("error") or result.get("message") or "保存失败",
                "received": True,
                "received_keys": sorted(payload.keys()),
            }
        return {
            "success": True,
            "received": True,
            "received_keys": sorted(payload.keys()),
            "config": result.get("config") or self._build_config_payload(),
        }

    async def _page_config(self):
        if self._get_astrbot_page_method() == "POST":
            return await self._page_update_config()
        return await self._page_get_config()

    async def _page_get_config_schema(self):
        return await self._load_config_schema_payload()

    async def _page_list_session_configs(self):
        return {"sessions": self._list_session_config_payloads()}

    async def _page_get_session_config(self, umo: str):
        return self._build_session_config_payload(umo)

    async def _page_update_session_config(self, umo: str):
        payload = await self._read_astrbot_page_json()
        return await self._apply_session_config_payload(umo, payload)

    async def _page_session_config(self, umo: str):
        if self._get_astrbot_page_method() == "POST":
            return await self._page_update_session_config(umo)
        return await self._page_get_session_config(umo)

    async def _page_reset_session_config(self, umo: str):
        return await self._reset_session_config_payload(umo)

    async def _page_list_jobs(self):
        return {"jobs": self._collect_jobs()}

    async def _page_reschedule_job(self, umo: str):
        return await self._reschedule_job_payload(umo)

    async def _page_trigger_job(self, umo: str):
        return await self._trigger_job_payload(umo)

    async def _page_cancel_job(self, umo: str):
        return await self._cancel_job_payload(umo)

    def _setup_app(self) -> None:
        _patch_starlette_router_startup_kwargs()
        # 创建 FastAPI 应用，版本号用于控制台元信息展示。
        self.app = FastAPI(
            title="主动消息管理端",
            description="主动消息插件独立 WebUI",
        )

        # 管理端通常运行在本地独立端口；在允许凭据时使用显式本地来源列表，避免 "*" 带来的安全/兼容问题。
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "http://localhost:4100",
                "http://127.0.0.1:4100",
                "http://localhost",
                "http://127.0.0.1",
            ],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @self.app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            # 未启用密码保护时，所有请求直接放行。
            if not self._auth_enabled:
                return await call_next(request)

            path = request.url.path
            # 登录接口与鉴权信息探测接口必须允许匿名访问，否则前端无法完成登录。
            if path in {"/api/login", "/api/auth-info"}:
                return await call_next(request)

            # 非 API 路径主要是静态文件，不在这里拦截，前端自行处理启动页逻辑。
            if not path.startswith("/api"):
                return await call_next(request)

            # API 请求统一使用 Bearer Token 认证，避免把 token 暴露在 query 参数里。
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return JSONResponse({"error": "未授权"}, status_code=401)

            token = auth_header[7:]
            # 令牌不存在、已过期或不合法时，返回 401 让前端重新登录。
            if not self._verify_token(token):
                return JSONResponse({"error": "登录已过期"}, status_code=401)

            return await call_next(request)

        # 路由与静态资源挂载分开处理，方便后续维护。
        self._register_routes()
        self._mount_static_files()

    def _mount_static_files(self) -> None:
        if not self.app:
            return

        # admin 目录位于插件根目录下，是整个前端控制台的静态资源根路径。
        admin_dir = Path(__file__).resolve().parent.parent / "admin"
        if admin_dir.exists():
            # 将根路径直接挂到静态文件目录，便于通过 / 访问前端页面。
            self.app.mount(
                "/", StaticFiles(directory=str(admin_dir), html=True), name="admin"
            )
        else:
            logger.warning(f"[主动消息] 未找到管理端静态目录喵: {admin_dir}")

    def _register_routes(self) -> None:
        if not self.app:
            return

        @self.app.get("/api/auth-info")
        async def auth_info():
            # 前端启动时会先调用该接口，判断是否需要展示登录流程。
            return {"auth_required": self._auth_enabled}

        @self.app.post("/api/login")
        async def login(credentials: dict[str, Any]):
            # 从配置中读取管理端密码；未配置密码时视为关闭鉴权。
            password = self.config.get("web_admin", {}).get("password", "")
            if not password:
                # 返回固定 no-auth token，便于前端保持统一的请求头处理逻辑。
                return {"token": "no-auth", "auth_required": False}

            input_password = str(credentials.get("password", ""))
            # 使用常量时间比较，避免简单的时序侧信道问题。
            if not secrets.compare_digest(input_password, password):
                return JSONResponse({"error": "密码错误"}, status_code=401)

            token = self._issue_token()
            return {"token": token, "auth_required": True}

        @self.app.get("/logo.png")
        async def get_logo():
            # 兼容前端在不同相对路径下请求 logo 的场景。
            logo_path = Path(__file__).resolve().parent.parent / "logo.png"
            if logo_path.exists():
                return FileResponse(str(logo_path), media_type="image/png")
            return JSONResponse({"error": "logo not found"}, status_code=404)

        @self.app.get("/api/status")
        async def get_status():
            # 汇总插件运行状态、计时器与连接数，供首页卡片与轮询逻辑使用。
            return self._build_status_payload()

        @self.app.get("/api/embedded-dashboard")
        async def embedded_dashboard():
            # 给 AstrBot 插件 Page 或其它轻量入口复用的聚合快照。
            return await self.build_astrbot_page_payload()

        @self.app.get("/api/markdown-files")
        async def list_markdown_files():
            # 仅暴露插件目录内明确允许浏览的 Markdown 文档，避免前端任意探测文件系统。
            return {"items": self._list_markdown_documents()}

        @self.app.get("/api/markdown-files/{file_path:path}")
        async def get_markdown_file(file_path: str):
            # FastAPI 已对 path 参数完成一次 URL 解码，这里直接交给白名单解析，避免重复解码破坏合法文件名。
            resolved = self._resolve_markdown_document(file_path)
            if not resolved:
                return JSONResponse(
                    {"error": "文档不存在或不允许访问"}, status_code=404
                )

            try:
                # 文件读取放到线程池中执行，避免阻塞事件循环影响 WebSocket 或其它 HTTP 请求。
                content = await asyncio.to_thread(resolved.read_text, encoding="utf-8")
            except UnicodeDecodeError:
                # 前端当前只按 UTF-8 渲染 Markdown；若编码不匹配，直接返回可理解错误提示。
                return JSONResponse(
                    {"error": "文档编码不受支持，仅支持 UTF-8 Markdown 文件"},
                    status_code=400,
                )
            except Exception as e:
                logger.error(f"[主动消息] 读取 Markdown 文档失败喵: {e}")
                return JSONResponse(
                    {"error": "读取文档失败", "message": str(e)}, status_code=500
                )

            return {
                # path 返回工作区相对路径，便于前端做目录列表高亮和当前文档定位。
                "path": self._to_workspace_relative_path(resolved),
                # title 直接取 stem，减少前端再做文件名拆分。
                "title": resolved.stem,
                # content 保留原始 Markdown 文本，由前端统一负责渲染。
                "content": content,
                # 显式告诉前端这是 Markdown 内容，方便后续复用统一渲染管线。
                "content_format": "markdown",
            }

        @self.app.get("/api/config")
        async def get_config():
            # 返回配置时显式过滤密码字段，避免管理端读取到明文密码。
            web_admin = {
                k: v
                for k, v in self.config.get("web_admin", {}).items()
                if k != "password"
            }
            return {
                "friend_settings": dict(self.config.get("friend_settings", {})),
                "group_settings": dict(self.config.get("group_settings", {})),
                "web_admin": web_admin,
                "notification_settings": dict(
                    self.config.get("notification_settings", {})
                ),
            }

        @self.app.get("/api/config-schema")
        async def get_config_schema():
            # Schema 用于前端动态渲染配置表单，而不是写死表单结构。
            schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
            if schema_path.exists():
                try:
                    # Schema 文件可能较大，因此同样放在线程池读取，减少主循环阻塞。
                    schema_text = await asyncio.to_thread(
                        schema_path.read_text, encoding="utf-8"
                    )
                    return json.loads(schema_text)
                except Exception as e:
                    logger.error(f"[主动消息] 读取 Schema 失败喵: {e}")
            return {}

        @self.app.post("/api/config")
        async def update_config(payload: dict[str, Any]):
            result = await self._apply_config_payload(payload)
            if not result.get("ok", False):
                return JSONResponse(result, status_code=400)
            return result

        @self.app.get("/api/session-config/sessions")
        async def list_session_configs():
            # 汇总所有已知会话，给前端会话差异配置页做选择器与列表展示。
            sessions = self._list_known_sessions()
            result = []
            for session in sessions:
                override = self.plugin.session_override_manager.get_override(session)
                effective = self.plugin._get_session_config(session)
                session_name = self.plugin._get_session_name(session, effective)
                result.append(
                    {
                        "session": session,
                        "session_name": session_name,
                        "session_display_name": self.plugin._get_session_display_name(
                            session, effective
                        ),
                        # 标记是否存在会话级覆写，前端可据此展示提示标签。
                        "has_override": bool(override),
                        # 额外把 override keys 暴露给前端，便于提示“哪些配置项被会话级改写”。
                        "override_keys": list(override.keys()),
                        # effective 可能为空，因此这里需要防御式布尔判断。
                        "enabled": bool(effective and effective.get("enable", False)),
                        # 从运行时会话数据中拿到下一次触发时间，用于列表辅助信息展示。
                        "next_trigger_time": self.plugin.session_data.get(
                            session, {}
                        ).get("next_trigger_time"),
                        "unanswered_count": self.plugin.session_data.get(
                            session, {}
                        ).get("unanswered_count", 0),
                    }
                )
            return {"sessions": result}

        @self.app.get("/api/session-config/{umo:path}")
        async def get_session_config(umo: str):
            # 路径参数使用 path 转换器，允许会话 ID 中包含斜杠等特殊字符。
            normalized = self.plugin._normalize_session_id(self._decode_route_umo(umo))
            base = self.plugin._get_base_session_config(normalized)
            return {
                "session": normalized,
                # base 表示命中 friend/group 全局配置后的基础配置。
                "base": base,
                # override 是该会话显式保存的差异字段。
                "override": self.plugin.session_override_manager.get_override(
                    normalized
                ),
                # effective 是基础配置与覆写合并后的最终生效配置。
                "effective": self.plugin._get_session_config(normalized),
            }

        @self.app.post("/api/session-config/{umo:path}")
        async def update_session_config(umo: str, payload: dict[str, Any]):
            normalized = self.plugin._normalize_session_id(self._decode_route_umo(umo))
            # mode 用于兼容两种写法：直接提交 override，或提交最终 effective 配置。
            mode = payload.get("mode", "effective")

            if mode == "override":
                override = payload.get("override", {})
                if not isinstance(override, dict):
                    return JSONResponse(
                        {"error": "override 必须是对象"}, status_code=400
                    )
                # override 模式由前端显式提交差异配置，后端不再做反推。
                await self.plugin.session_override_manager.set_override(
                    normalized, override
                )
            else:
                effective = payload.get("effective", {})
                if not isinstance(effective, dict):
                    return JSONResponse(
                        {"error": "effective 必须是对象"}, status_code=400
                    )
                base = self.plugin._get_base_session_config(normalized)
                if not base:
                    # 没有基础配置时无法反推出差异项，因此拒绝保存 effective。
                    return JSONResponse(
                        {
                            "error": "会话未命中 friend/group 全局配置，无法保存 effective"
                        },
                        status_code=400,
                    )
                await (
                    self.plugin.session_override_manager.update_session_from_effective(
                        normalized,
                        base,
                        effective,
                    )
                )

            await self._broadcast_update("session-config")
            return {
                "ok": True,
                "session": normalized,
                "override": self.plugin.session_override_manager.get_override(
                    normalized
                ),
                "effective": self.plugin._get_session_config(normalized),
            }

        @self.app.delete("/api/session-config/{umo:path}")
        async def reset_session_config(umo: str):
            # 删除覆写后，会话会重新完全继承全局配置。
            normalized = self.plugin._normalize_session_id(self._decode_route_umo(umo))
            await self.plugin.session_override_manager.delete_override(normalized)
            await self._broadcast_update("session-config")
            return {
                "ok": True,
                "session": normalized,
                "override": {},
                "effective": self.plugin._get_session_config(normalized),
            }

        @self.app.get("/api/jobs")
        async def list_jobs():
            # 返回调度器中的待执行任务列表，供任务页卡片展示。
            return {"jobs": self._collect_jobs()}

        @self.app.post("/api/jobs/{umo:path}/reschedule")
        async def reschedule_job(umo: str):
            normalized = self.plugin._normalize_session_id(self._decode_route_umo(umo))
            session_config = self.plugin._get_session_config(normalized)
            if not session_config or not session_config.get("enable", False):
                return JSONResponse(
                    {
                        "ok": False,
                        "session": normalized,
                        "error": "会话未启用或配置不存在，无法重新调度",
                    },
                    status_code=400,
                )

            await self.plugin._schedule_next_chat_and_save(
                normalized, reset_counter=False
            )
            await self._broadcast_update("jobs")
            return {
                "ok": True,
                "session": normalized,
                "message": "已重新调度下一次主动消息时间",
            }

        @self.app.get("/api/notifications")
        async def get_notifications():
            # 通知列表统一从插件本地缓存读取，前端不直接访问外部通知平台。
            # 复用统一的通知载荷构造函数，确保 HTTP 与 WebSocket 输出结构保持一致。
            return await self._build_notification_payload()

        @self.app.post("/api/notifications/read")
        async def mark_notification_read(payload: dict[str, Any]):
            # 单条已读只影响插件本地缓存中的 read_map，不涉及远端接口写回。
            if not getattr(self.plugin, "notification_center", None):
                return JSONResponse({"error": "通知系统不可用"}, status_code=503)

            notification_id = payload.get("id")
            if notification_id is None:
                return JSONResponse({"error": "缺少必填字段 id"}, status_code=400)
            try:
                # 前端传值可能是字符串，因此这里统一转成 int，方便下游逻辑处理。
                normalized_id = int(notification_id)
            except (TypeError, ValueError):
                return JSONResponse({"error": "id 必须是数字"}, status_code=400)

            result = await self.plugin.notification_center.mark_as_read(normalized_id)
            await self._broadcast_update("notifications")
            return result

        @self.app.post("/api/notifications/read-all")
        async def mark_all_notifications_read():
            # 批量已读后立即广播，保证多个已打开页面的未读角标同步归零。
            if not getattr(self.plugin, "notification_center", None):
                return JSONResponse({"error": "通知系统不可用"}, status_code=503)
            result = await self.plugin.notification_center.mark_all_as_read()
            await self._broadcast_update("notifications")
            return result

        @self.app.post("/api/notifications/refresh")
        async def refresh_notifications():
            # 供前端“立即同步”按钮调用，强制拉取远端最新通知并回传完整快照。
            if not getattr(self.plugin, "notification_center", None):
                return JSONResponse({"error": "通知系统不可用"}, status_code=503)
            changed = await self.plugin.notification_center.refresh()
            # 即便 changed 为 False，也广播一次，确保当前页面拿到最新同步时间等元信息。
            await self._broadcast_update("notifications")
            payload = await self.plugin.notification_center.get_payload()
            return {
                "ok": True,
                "changed": changed,
                "items": payload.get("items", []),
                "meta": payload.get("meta", {}),
            }

        @self.app.post("/api/open-directory")
        async def open_directory(payload: dict[str, Any]):
            # 允许前端请求打开插件目录或数据目录，便于管理员快速定位文件。
            target = str(payload.get("path", "plugin")).strip().lower()
            if target == "data":
                directory = Path(self.plugin.data_dir)
            else:
                # 默认回退到插件根目录，保证前端传值异常时仍有一个安全目标。
                directory = Path(__file__).resolve().parent.parent

            try:
                # 确保目录存在，再根据当前系统选择合适的打开方式。
                directory.mkdir(parents=True, exist_ok=True)
                dir_str = str(directory)

                if _is_running_in_docker():
                    return JSONResponse(
                        {
                            "error": "Docker 环境下不支持在宿主机直接打开目录，请手动查看挂载路径",
                            "path": dir_str,
                        },
                        status_code=400,
                    )

                if os.name == "nt":
                    # Windows 使用系统默认资源管理器，封装为异步避免阻塞事件循环。
                    await asyncio.to_thread(os.startfile, dir_str)
                elif sys.platform == "darwin":
                    # macOS 通过 open 命令调起 Finder；失败时把 stderr 带回前端便于定位。
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["open", dir_str],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout or "未知错误").strip()
                        return JSONResponse(
                            {
                                "error": "打开目录失败（macOS）",
                                "message": f"open 命令执行失败: {detail}",
                                "path": dir_str,
                            },
                            status_code=500,
                        )
                else:
                    # 其它类 Unix 系统优先尝试 xdg-open，兼容常见 Linux 桌面环境。
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["xdg-open", dir_str],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout or "未知错误").strip()
                        return JSONResponse(
                            {
                                "error": "打开目录失败（Linux）",
                                "message": (
                                    "xdg-open 执行失败，服务器可能缺少桌面环境或未安装 xdg-open: "
                                    f"{detail}"
                                ),
                                "path": dir_str,
                            },
                            status_code=500,
                        )

                return {
                    "ok": True,
                    "path": dir_str,
                    "message": "已在系统文件管理器中打开目录",
                }
            except FileNotFoundError as e:
                logger.error(f"[主动消息] 打开目录失败（命令缺失）喵: {e}")
                return JSONResponse(
                    {
                        "error": "打开目录失败：系统缺少所需命令",
                        "message": "请确认系统已安装对应文件管理器命令（如 open / xdg-open）",
                        "path": str(directory),
                    },
                    status_code=500,
                )
            except PermissionError as e:
                logger.error(f"[主动消息] 打开目录失败（权限不足）喵: {e}")
                return JSONResponse(
                    {
                        "error": "打开目录失败：权限不足",
                        "message": str(e),
                        "path": str(directory),
                    },
                    status_code=500,
                )
            except Exception as e:
                logger.error(f"[主动消息] 打开目录失败喵: {e}")
                return JSONResponse(
                    {
                        "error": "打开目录失败",
                        "message": str(e),
                        "path": str(directory),
                    },
                    status_code=500,
                )

        @self.app.post("/api/jobs/{umo:path}/trigger")
        async def trigger_job(umo: str):
            # 立即手动触发一次指定会话的检查与发言流程；同一会话在执行完成前禁止重复触发。
            normalized = self.plugin._normalize_session_id(self._decode_route_umo(umo))
            if normalized in self.plugin.manual_trigger_sessions:
                return JSONResponse(
                    {
                        "ok": False,
                        "session": normalized,
                        "in_progress": True,
                        "message": "该任务正在立即触发中，请等待当前执行完成",
                    },
                    status_code=409,
                )

            self.plugin.manual_trigger_sessions.add(normalized)
            # 主动创建后台任务，避免前端请求长时间挂起等待业务执行完成。
            asyncio.create_task(self.plugin.check_and_chat(normalized))
            await self._broadcast_update("jobs")
            return {
                "ok": True,
                "session": normalized,
                "in_progress": True,
                "message": "已开始立即触发，正在等待 LLM 完成回复",
            }

        @self.app.delete("/api/jobs/{umo:path}")
        async def cancel_job(umo: str):
            normalized = self.plugin._normalize_session_id(self._decode_route_umo(umo))
            removed = False
            try:
                # APScheduler 中的 job id 直接使用规范化后的 session id。
                self.plugin.scheduler.remove_job(normalized)
                removed = True
            except Exception:
                # 任务不存在时保持幂等，不把异常直接抛给前端。
                pass

            async with self.plugin.data_lock:
                if normalized in self.plugin.session_data:
                    # 同步清理持久化调度字段，避免界面显示过期倒计时。
                    if self.plugin._clear_session_schedule_state(normalized):
                        await self.plugin._save_data_internal()

            if removed:
                logger.info(
                    f"[主动消息] Web 管理端已取消 {self.plugin._get_session_log_str(normalized)} 的调度任务喵。"
                )
            else:
                logger.warning(
                    f"[主动消息] Web 管理端请求取消 {self.plugin._get_session_log_str(normalized)} 的调度任务喵，但当前未找到可取消任务。"
                )

            await self._broadcast_update("jobs")
            return {"ok": True, "session": normalized, "removed": removed}

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            # 当前 WebSocket 通道统一承载运行状态、任务、会话摘要与通知系统的实时同步。
            if self._auth_enabled:
                # WebSocket 无法沿用普通中间件，这里单独做一次 token 校验。
                token = websocket.query_params.get("token", "")
                if not token:
                    auth_header = websocket.headers.get("Authorization", "")
                    if auth_header.startswith("Bearer "):
                        token = auth_header[7:]
                if not self._verify_token(token):
                    # 1008 表示策略违规，适合表达认证失败。
                    await websocket.close(code=1008)
                    return

            await websocket.accept()
            self._ws_connections.append(websocket)

            try:
                # 连接建立后先推送一次完整快照，避免前端依赖额外首次拉取。
                await websocket.send_json(
                    {
                        "type": "full_update",
                        "data": {
                            "status": self._build_status_payload(),
                            "jobs": self._collect_jobs(),
                            "sessions": self._list_known_session_summaries(),
                            "notifications": await self._build_notification_payload(),
                        },
                    }
                )

                while True:
                    # 前端只需发送轻量消息：ping 保活、refresh 主动请求全量刷新。
                    data = await websocket.receive_text()
                    try:
                        msg = json.loads(data)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        logger.debug(
                            f"[主动消息] WebSocket 收到无效 JSON 数据喵: {str(data)[:100]}"
                        )
                        continue

                    if not isinstance(msg, dict):
                        logger.debug(
                            "[主动消息] WebSocket 收到的 JSON 不是对象，已忽略喵。"
                        )
                        continue

                    msg_type = msg.get("type")
                    if msg_type == "ping":
                        await websocket.send_json({"type": "pong"})
                    elif msg_type == "refresh":
                        # refresh 语义是“请立即把当前全量状态重新推送一次”。
                        await websocket.send_json(
                            {
                                "type": "full_update",
                                "data": {
                                    "status": self._build_status_payload(),
                                    "jobs": self._collect_jobs(),
                                    "sessions": self._list_known_session_summaries(),
                                    "notifications": await self._build_notification_payload(),
                                },
                            }
                        )
            except WebSocketDisconnect:
                # 浏览器主动关闭标签页时会进入这里，属于正常流程。
                pass
            except Exception as e:
                logger.debug(f"[主动消息] WebSocket 连接异常喵: {e}")
            finally:
                # 无论异常还是正常断开，都必须回收连接引用，避免广播时残留死连接。
                if websocket in self._ws_connections:
                    self._ws_connections.remove(websocket)

    def _set_config_section(self, key: str, value: Any) -> None:
        self.config[key] = value

    def _save_plugin_config(self) -> tuple[bool, str | None]:
        """Persist config through AstrBot's native object and notify newer contexts."""
        try:
            # AstrBot 配置对象通常提供 save_config 方法，这里做鸭子类型兼容。
            native = self._native_config or (
                self.config if hasattr(self.config, "save_config") else None
            )
            if native is not None:
                if native is not self.config:
                    native.clear()
                    native.update(self.config)
                native.save_config()

            context = getattr(self.plugin, "context", None)
            update_config = getattr(context, "update_config", None)
            if callable(update_config):
                updated = update_config(self.config)
                if inspect.isawaitable(updated):
                    self._track_background_task(asyncio.create_task(updated))
            return True, None
        except Exception as e:
            logger.warning(f"[主动消息] 保存配置失败喵: {e}")
            return False, str(e)

    def _issue_token(self) -> str:
        # 生成适合放入 URL / Header 的安全随机 token。
        token = secrets.token_urlsafe(24)
        self._tokens[token] = time.time() + self._token_expire_seconds
        return token

    def _verify_token(self, token: str) -> bool:
        # 空 token 直接失败，避免后续字典查找与比较的无意义开销。
        if not token:
            return False
        if token == "no-auth":
            # 在未启用鉴权时允许该哨兵令牌直接通过。
            return True
        expire_at = self._tokens.get(token)
        if not expire_at:
            return False
        if time.time() > expire_at:
            # 过期即顺手删除，避免内存令牌表无限增长。
            self._tokens.pop(token, None)
            return False
        return True

    def _build_config_payload(self) -> dict[str, Any]:
        web_admin = {
            k: v for k, v in self.config.get("web_admin", {}).items() if k != "password"
        }
        return {
            "friend_settings": dict(self.config.get("friend_settings", {})),
            "group_settings": dict(self.config.get("group_settings", {})),
            "web_admin": web_admin,
            "notification_settings": dict(self.config.get("notification_settings", {})),
        }

    async def _apply_config_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Pages 只能写入 WebUI 暴露的四个顶层配置段，避免误把运行时状态写进主配置。
        allowed_keys = {
            "friend_settings",
            "group_settings",
            "web_admin",
            "notification_settings",
        }
        changed = False
        for key in allowed_keys:
            if key not in payload:
                continue
            if key != "web_admin" and not isinstance(payload[key], dict):
                return {"ok": False, "error": f"{key} must be an object"}
            if key == "web_admin":
                if not isinstance(payload.get("web_admin"), dict):
                    return {"ok": False, "error": "web_admin must be an object"}
                old = dict(self.config.get("web_admin", {}))
                old.update(payload.get("web_admin", {}))
                if "password" in payload.get("web_admin", {}):
                    old["password"] = payload["web_admin"]["password"]
                self._set_config_section("web_admin", old)
            else:
                self._set_config_section(key, payload[key])
            changed = True

        if not changed:
            return {"ok": False, "error": "No supported config keys received"}

        saved, error = self._save_plugin_config()
        if not saved:
            return {"ok": False, "error": error or "Config save failed"}
        self._auth_enabled = bool(self.config.get("web_admin", {}).get("password", ""))
        await self._broadcast_update("config")
        return {"ok": True, "config": self._build_config_payload()}

    async def _load_config_schema_payload(self) -> dict[str, Any]:
        schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
        if schema_path.exists():
            try:
                schema_text = await asyncio.to_thread(
                    schema_path.read_text, encoding="utf-8"
                )
                return json.loads(schema_text)
            except Exception as e:
                logger.error(f"[主动消息] 读取 Schema 失败喵: {e}")
        return {}

    def _list_session_config_payloads(self) -> list[dict[str, Any]]:
        result = []
        for session in self._list_known_sessions():
            override = self.plugin.session_override_manager.get_override(session)
            effective = self.plugin._get_session_config(session)
            session_name = self.plugin._get_session_name(session, effective)
            result.append(
                {
                    "session": session,
                    "session_name": session_name,
                    "session_display_name": self.plugin._get_session_display_name(
                        session, effective
                    ),
                    "has_override": bool(override),
                    "override_keys": list(override.keys()),
                    "enabled": bool(effective and effective.get("enable", False)),
                    "next_trigger_time": self.plugin.session_data.get(session, {}).get(
                        "next_trigger_time"
                    ),
                    "unanswered_count": self.plugin.session_data.get(session, {}).get(
                        "unanswered_count", 0
                    ),
                }
            )
        return result

    def _build_session_config_payload(self, umo: str) -> dict[str, Any]:
        normalized = self.plugin._normalize_session_id(self._decode_route_umo(umo))
        base = self.plugin._get_base_session_config(normalized)
        return {
            "session": normalized,
            "base": base,
            "override": self.plugin.session_override_manager.get_override(normalized),
            "effective": self.plugin._get_session_config(normalized),
        }

    async def _apply_session_config_payload(
        self, umo: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        normalized = self.plugin._normalize_session_id(self._decode_route_umo(umo))
        mode = payload.get("mode", "effective")

        if mode == "override":
            # override 模式直接保存差异片段，适合高级用户或后续批量编辑入口。
            override = payload.get("override", {})
            if not isinstance(override, dict):
                return {"ok": False, "error": "override 必须是对象"}
            await self.plugin.session_override_manager.set_override(
                normalized, override
            )
        else:
            # effective 模式让前端提交完整会话配置，后端负责计算与全局配置的最小差异。
            effective = payload.get("effective", {})
            if not isinstance(effective, dict):
                return {"ok": False, "error": "effective 必须是对象"}
            base = self.plugin._get_base_session_config(normalized)
            if not base:
                return {
                    "ok": False,
                    "error": "会话未命中 friend/group 全局配置，无法保存 effective",
                }
            await self.plugin.session_override_manager.update_session_from_effective(
                normalized,
                base,
                effective,
            )

        await self._broadcast_update("session-config")
        return {
            "ok": True,
            "session": normalized,
            "override": self.plugin.session_override_manager.get_override(normalized),
            "effective": self.plugin._get_session_config(normalized),
        }

    async def _reset_session_config_payload(self, umo: str) -> dict[str, Any]:
        normalized = self.plugin._normalize_session_id(self._decode_route_umo(umo))
        await self.plugin.session_override_manager.delete_override(normalized)
        await self._broadcast_update("session-config")
        return {
            "ok": True,
            "session": normalized,
            "override": {},
            "effective": self.plugin._get_session_config(normalized),
        }

    async def _reschedule_job_payload(self, umo: str) -> dict[str, Any]:
        normalized = self.plugin._normalize_session_id(self._decode_route_umo(umo))
        session_config = self.plugin._get_session_config(normalized)
        if not session_config or not session_config.get("enable", False):
            return {
                "ok": False,
                "session": normalized,
                "error": "会话未启用或配置不存在，无法重新调度",
            }

        await self.plugin._schedule_next_chat_and_save(normalized, reset_counter=False)
        await self._broadcast_update("jobs")
        return {
            "ok": True,
            "session": normalized,
            "message": "已重新调度下一次主动消息时间",
        }

    async def _trigger_job_payload(self, umo: str) -> dict[str, Any]:
        normalized = self.plugin._normalize_session_id(self._decode_route_umo(umo))
        if normalized in self.plugin.manual_trigger_sessions:
            return {
                "ok": False,
                "session": normalized,
                "in_progress": True,
                "message": "该任务正在立即触发中，请等待当前执行完成",
            }

        self.plugin.manual_trigger_sessions.add(normalized)
        self._track_background_task(
            asyncio.create_task(self.plugin.check_and_chat(normalized))
        )
        await self._broadcast_update("jobs")
        return {
            "ok": True,
            "session": normalized,
            "in_progress": True,
            "message": "已开始立即触发，正在等待 LLM 完成回复",
        }

    async def _cancel_job_payload(self, umo: str) -> dict[str, Any]:
        normalized = self.plugin._normalize_session_id(self._decode_route_umo(umo))
        removed = False
        try:
            self.plugin.scheduler.remove_job(normalized)
            removed = True
        except Exception:
            pass

        async with self.plugin.data_lock:
            if normalized in self.plugin.session_data:
                if self.plugin._clear_session_schedule_state(normalized):
                    await self.plugin._save_data_internal()

        await self._broadcast_update("jobs")
        return {"ok": True, "session": normalized, "removed": removed}

    async def _mark_notification_read_payload(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if not getattr(self.plugin, "notification_center", None):
            return {"ok": False, "error": "通知系统不可用"}

        notification_id = payload.get("id")
        if notification_id is None:
            return {"ok": False, "error": "缺少必填字段 id"}
        try:
            normalized_id = int(notification_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "id 必须是数字"}

        result = await self.plugin.notification_center.mark_as_read(normalized_id)
        await self._broadcast_update("notifications")
        return result

    async def _mark_all_notifications_read_payload(self) -> dict[str, Any]:
        if not getattr(self.plugin, "notification_center", None):
            return {"ok": False, "error": "通知系统不可用"}
        result = await self.plugin.notification_center.mark_all_as_read()
        await self._broadcast_update("notifications")
        return result

    async def _refresh_notifications_payload(self) -> dict[str, Any]:
        if not getattr(self.plugin, "notification_center", None):
            return {"ok": False, "error": "通知系统不可用"}
        changed = await self.plugin.notification_center.refresh()
        await self._broadcast_update("notifications")
        payload = await self.plugin.notification_center.get_payload()
        return {
            "ok": True,
            "changed": changed,
            "items": payload.get("items", []),
            "meta": payload.get("meta", {}),
        }

    async def _build_markdown_file_payload(self, file_path: str) -> dict[str, Any]:
        resolved = self._resolve_markdown_document(file_path)
        if not resolved:
            return {"ok": False, "error": "文档不存在或不允许访问"}

        try:
            content = await asyncio.to_thread(resolved.read_text, encoding="utf-8")
        except UnicodeDecodeError:
            return {
                "ok": False,
                "error": "文档编码不受支持，仅支持 UTF-8 Markdown 文件",
            }
        except Exception as e:
            logger.error(f"[主动消息] 读取 Markdown 文档失败喵: {e}")
            return {"ok": False, "error": "读取文档失败", "message": str(e)}

        return {
            "path": self._to_workspace_relative_path(resolved),
            "title": resolved.stem,
            "content": content,
            "content_format": "markdown",
        }

    async def _open_directory_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = str(payload.get("path", "plugin")).strip().lower()
        directory = (
            Path(self.plugin.data_dir)
            if target == "data"
            else Path(__file__).resolve().parent.parent
        )

        try:
            directory.mkdir(parents=True, exist_ok=True)
            dir_str = str(directory)

            if _is_running_in_docker():
                return {
                    "ok": False,
                    "error": "Docker 环境下不支持在宿主机直接打开目录，请手动查看挂载路径",
                    "path": dir_str,
                }

            if os.name == "nt":
                await asyncio.to_thread(os.startfile, dir_str)
            elif sys.platform == "darwin":
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["open", dir_str],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "未知错误").strip()
                    return {
                        "ok": False,
                        "error": "打开目录失败（macOS）",
                        "message": f"open 命令执行失败: {detail}",
                        "path": dir_str,
                    }
            else:
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["xdg-open", dir_str],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "未知错误").strip()
                    return {
                        "ok": False,
                        "error": "打开目录失败（Linux）",
                        "message": (
                            "xdg-open 执行失败，服务器可能缺少桌面环境或未安装 xdg-open: "
                            f"{detail}"
                        ),
                        "path": dir_str,
                    }

            return {
                "ok": True,
                "path": dir_str,
                "message": "已在系统文件管理器中打开目录",
            }
        except Exception as e:
            logger.error(f"[主动消息] 打开目录失败喵: {e}")
            return {
                "ok": False,
                "error": "打开目录失败",
                "message": str(e),
                "path": str(directory),
            }

    def _safe_timer_meta(self, timer: Any, now: float) -> dict[str, float | int | None]:
        # 某些会话可能当前没有有效 timer，此时直接返回空元信息。
        if timer is None:
            return {"remaining_seconds": None, "target_time": None}

        try:
            # 某些定时句柄可能已取消；这里优先过滤掉不可用状态。
            if getattr(timer, "cancelled", lambda: False)():
                return {"remaining_seconds": None, "target_time": None}
        except Exception:
            return {"remaining_seconds": None, "target_time": None}

        # asyncio 定时句柄通常暴露 when() 方法，返回 loop 单调时钟上的目标时刻。
        when_method = getattr(timer, "when", None)
        if not callable(when_method):
            return {"remaining_seconds": None, "target_time": None}

        try:
            loop_time = when_method()
            loop = getattr(timer, "_loop", None)
            current_loop_time = loop.time() if loop else None
            if current_loop_time is None:
                return {"remaining_seconds": None, "target_time": None}

            # 用单调时钟差值推导剩余秒数，再换算成当前 Unix 时间戳，避免受系统时间跳变影响。
            remaining_precise = max(0.0, loop_time - current_loop_time)
            target_time = now + remaining_precise
            return {
                # 向上取整，保证 UI 倒计时不会过早显示为 0。
                "remaining_seconds": max(0, int(math.ceil(remaining_precise))),
                "target_time": target_time,
            }
        except Exception:
            return {"remaining_seconds": None, "target_time": None}

    def _detect_session_category(self, session_id: str) -> str:
        # 优先使用插件已有解析逻辑识别会话类型，避免前后端规则不一致。
        parsed = self.plugin._parse_session_id(session_id)
        if not parsed:
            lowered = str(session_id).lower()
            # 兜底规则只在插件解析失败时启用，尽量保证前端仍有可用分类。
            return "group" if "group" in lowered else "friend"

        _, msg_type, _ = parsed
        return "group" if "group" in msg_type.lower() else "friend"

    def _collect_timer_cards(self, now: float) -> dict[str, list[dict[str, Any]]]:
        # auto_cards：自动触发检测计时器；group_cards：群沉默计时器。
        auto_cards: list[dict[str, Any]] = []
        group_cards: list[dict[str, Any]] = []
        # 群计时器优先展示为 group_silence，避免同一群会话被重复渲染两种卡片。
        active_group_sessions = {
            str(session_id) for session_id in self.plugin.group_timers.keys()
        }

        for session_id, timer in list(self.plugin.auto_trigger_timers.items()):
            normalized_session_id = self.plugin._normalize_session_id(str(session_id))
            if normalized_session_id in active_group_sessions:
                continue

            session_config = self.plugin._get_session_config(session_id) or {}
            session_data = self.plugin.session_data.get(session_id, {})
            auto_settings = session_config.get("auto_trigger_settings", {})
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            trigger_delay_minutes = int(
                auto_settings.get("auto_trigger_after_minutes", 0) or 0
            )
            # 前端展示与进度计算统一按秒处理，因此这里先把分钟窗口换算为秒。
            trigger_delay_seconds = max(0, trigger_delay_minutes * 60)
            timer_meta = self._safe_timer_meta(timer, now)
            remaining_seconds = timer_meta["remaining_seconds"]
            target_time = timer_meta["target_time"]
            # 若拿不到真实开始时间，则退化为“插件启动时间”或根据窗口长度反推一个近似值。
            started_at = max(self.plugin.plugin_start_time, now - trigger_delay_seconds)
            progress_percent = 0
            if trigger_delay_seconds > 0 and remaining_seconds is not None:
                consumed = max(0, trigger_delay_seconds - remaining_seconds)
                progress_percent = max(
                    0, min(100, round((consumed / trigger_delay_seconds) * 100))
                )

            auto_cards.append(
                {
                    "session_id": normalized_session_id,
                    "session_name": self.plugin._get_session_name(
                        normalized_session_id, session_config
                    ),
                    "session_display_name": self.plugin._get_session_display_name(
                        normalized_session_id, session_config
                    ),
                    "session_category": self._detect_session_category(
                        normalized_session_id
                    ),
                    "source_mode": context_settings.get(
                        "source_mode", "conversation_history"
                    ),
                    "max_unanswered_times": schedule_settings.get(
                        "max_unanswered_times", 0
                    ),
                    "timer_kind": "auto_trigger",
                    "title": "自动触发检测",
                    # remaining_seconds 可用时说明计时器处于有效运行状态，否则只能标为 unknown。
                    "status": "running" if remaining_seconds is not None else "unknown",
                    "remaining_seconds": remaining_seconds,
                    "target_time": target_time,
                    "started_at": started_at,
                    "window_seconds": trigger_delay_seconds,
                    "progress_percent": progress_percent,
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    "auto_trigger_after_minutes": trigger_delay_minutes,
                }
            )

        for session_id, timer in list(self.plugin.group_timers.items()):
            normalized_session_id = self.plugin._normalize_session_id(str(session_id))
            session_config = (
                self.plugin._get_session_config(normalized_session_id) or {}
            )
            session_data = self.plugin.session_data.get(normalized_session_id, {})
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            idle_minutes = int(session_config.get("group_idle_trigger_minutes", 0) or 0)
            idle_seconds = max(0, idle_minutes * 60)
            timer_meta = self._safe_timer_meta(timer, now)
            remaining_seconds = timer_meta["remaining_seconds"]
            target_time = timer_meta["target_time"]
            # 群沉默计时器更适合以“最后一条用户消息时间”作为窗口起点。
            last_message_time = self.plugin.last_message_times.get(
                normalized_session_id, 0
            )
            temp_state = self.plugin.session_temp_state.get(normalized_session_id, {})
            last_user_time = (
                temp_state.get("last_user_time") or last_message_time or None
            )
            # 若历史时间缺失，则根据剩余时间反推一个近似 started_at。
            started_at = last_user_time or (
                now - max(0, idle_seconds - (remaining_seconds or 0))
            )
            progress_percent = 0
            if idle_seconds > 0 and remaining_seconds is not None:
                consumed = max(0, idle_seconds - remaining_seconds)
                progress_percent = max(
                    0, min(100, round((consumed / idle_seconds) * 100))
                )

            group_cards.append(
                {
                    "session_id": normalized_session_id,
                    "session_name": self.plugin._get_session_name(
                        normalized_session_id, session_config
                    ),
                    "session_display_name": self.plugin._get_session_display_name(
                        normalized_session_id, session_config
                    ),
                    "session_category": self._detect_session_category(
                        normalized_session_id
                    ),
                    "source_mode": context_settings.get(
                        "source_mode", "platform_message_history"
                    ),
                    "max_unanswered_times": schedule_settings.get(
                        "max_unanswered_times", 0
                    ),
                    "timer_kind": "group_silence",
                    "title": "群沉默检测",
                    # 群沉默卡的状态定义与 auto_trigger 保持一致，便于前端复用状态渲染逻辑。
                    "status": "running" if remaining_seconds is not None else "unknown",
                    "remaining_seconds": remaining_seconds,
                    "target_time": target_time,
                    "started_at": started_at if started_at else None,
                    "window_seconds": idle_seconds,
                    "progress_percent": progress_percent,
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    "group_idle_trigger_minutes": idle_minutes,
                    "last_message_time": last_message_time or None,
                    "last_user_time": last_user_time,
                    # 显式标记这是实时群计时器，便于前端做差异化展示或调试。
                    "is_live_group_timer": True,
                }
            )

        # 统一按剩余时间升序排序，让最接近触发的卡片优先显示。
        live_auto_sessions = {
            str(card.get("session_id", "")) for card in auto_cards if card.get("session_id")
        }
        live_group_sessions = {
            str(card.get("session_id", ""))
            for card in group_cards
            if card.get("session_id")
        }

        # Expose configured sessions even when no asyncio timer handle is currently
        # registered. Without these placeholders the Pages dashboard looks empty,
        # while the real state is often "waiting for the first message/event".
        for session_id in self._list_known_sessions():
            normalized_session_id = self.plugin._normalize_session_id(str(session_id))
            session_config = self.plugin._get_session_config(normalized_session_id)
            if not session_config or not session_config.get("enable", False):
                continue

            session_category = self._detect_session_category(normalized_session_id)
            session_data = self.plugin.session_data.get(
                normalized_session_id, self.plugin.session_data.get(str(session_id), {})
            )
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            common_payload = {
                "session_id": normalized_session_id,
                "session_name": self.plugin._get_session_name(
                    normalized_session_id, session_config
                ),
                "session_display_name": self.plugin._get_session_display_name(
                    normalized_session_id, session_config
                ),
                "session_category": session_category,
                "source_mode": context_settings.get(
                    "source_mode",
                    "platform_message_history"
                    if session_category == "group"
                    else "conversation_history",
                ),
                "max_unanswered_times": schedule_settings.get(
                    "max_unanswered_times", 0
                ),
                "remaining_seconds": None,
                "target_time": None,
                "progress_percent": 0,
                "unanswered_count": session_data.get("unanswered_count", 0),
            }

            if session_category == "group":
                if normalized_session_id in live_group_sessions:
                    continue

                idle_minutes = int(
                    session_config.get("group_idle_trigger_minutes", 0) or 0
                )
                if idle_minutes <= 0:
                    continue

                group_cards.append(
                    {
                        **common_payload,
                        "timer_kind": "group_silence",
                        "title": "群沉默检测",
                        "timer_kind_label": "群沉默检测",
                        "status": "waiting_message",
                        "window_seconds": idle_minutes * 60,
                        "group_idle_trigger_minutes": idle_minutes,
                        "last_message_time": self.plugin.last_message_times.get(
                            normalized_session_id
                        )
                        or None,
                        "inactive_reason": "等待群聊新消息后开始沉默倒计时",
                        "is_live_group_timer": False,
                    }
                )
                live_group_sessions.add(normalized_session_id)
                continue

            if normalized_session_id in live_auto_sessions:
                continue

            auto_settings = session_config.get("auto_trigger_settings", {})
            if not auto_settings.get("enable_auto_trigger", False):
                continue

            trigger_delay_minutes = int(
                auto_settings.get("auto_trigger_after_minutes", 0) or 0
            )
            if trigger_delay_minutes <= 0:
                continue

            last_message_time = self.plugin.last_message_times.get(
                normalized_session_id
            ) or self.plugin.last_message_times.get(str(session_id), 0)
            auto_cards.append(
                {
                    **common_payload,
                    "timer_kind": "auto_trigger",
                    "title": "自动触发检测",
                    "timer_kind_label": "自动触发检测",
                    "status": "waiting_idle" if last_message_time else "pending_timer",
                    "window_seconds": trigger_delay_minutes * 60,
                    "auto_trigger_after_minutes": trigger_delay_minutes,
                    "last_message_time": last_message_time or None,
                    "inactive_reason": "当前没有运行中的自动触发计时器，等待运行时注册或下一次消息事件",
                }
            )
            live_auto_sessions.add(normalized_session_id)

        auto_cards.sort(
            key=lambda item: (
                item.get("remaining_seconds") is None,
                item.get("remaining_seconds") or 0,
                item["session_id"],
            )
        )
        group_cards.sort(
            key=lambda item: (
                item.get("remaining_seconds") is None,
                item.get("remaining_seconds") or 0,
                item["session_id"],
            )
        )
        return {
            "auto_trigger_cards": auto_cards,
            "group_timer_cards": group_cards,
        }

    def _build_status_payload(self) -> dict[str, Any]:
        now = time.time()
        uptime_sec = max(0, int(now - self.plugin.plugin_start_time))
        timer_cards = self._collect_timer_cards(now)
        visible_jobs_count = len(self._collect_jobs())

        return {
            "running": True,
            # 版本来源按优先级依次回退，保证控制台总能显示一个可读值。
            "version": getattr(self.plugin, "version", None)
            or getattr(self.plugin, "__version__", None)
            or self._metadata_version
            or "未知版本",
            "uptime_seconds": uptime_sec,
            # uptime 使用 datetime 差值字符串，便于直接面向人类展示。
            "uptime": str(
                datetime.fromtimestamp(now)
                - datetime.fromtimestamp(self.plugin.plugin_start_time)
            ),
            "scheduler_running": bool(
                self.plugin.scheduler and self.plugin.scheduler.running
            ),
            "sessions_count": len(self.plugin.session_data),
            "auto_trigger_timers": len(self.plugin.auto_trigger_timers),
            "group_timers": len(self.plugin.group_timers),
            "jobs_count": visible_jobs_count,
            # 计时器总数在前端可直接用于角标和标题，无需再做两次求和。
            "timer_cards_total": len(timer_cards["auto_trigger_cards"])
            + len(timer_cards["group_timer_cards"]),
            "auto_trigger_cards": timer_cards["auto_trigger_cards"],
            "group_timer_cards": timer_cards["group_timer_cards"],
            "ws_connections": len(self._ws_connections),
            # 时间戳用于前端判断数据新鲜度或手动刷新完成时间。
            "timestamp": datetime.now().isoformat(),
        }

    def _build_web_admin_url(self) -> str | None:
        web_admin = self.config.get("web_admin", {})
        if not web_admin.get("enabled", False):
            return None

        host = str(web_admin.get("host", "127.0.0.1") or "127.0.0.1")
        port = int(web_admin.get("port", 4100) or 4100)
        display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        return f"http://{display_host}:{port}/"

    async def build_astrbot_page_payload(self) -> dict[str, Any]:
        """构造 AstrBot 插件卡片 Page 的轻量运行快照。"""
        return {
            "ok": True,
            "status": self._build_status_payload(),
            "jobs": self._collect_jobs(),
            "sessions": self._list_known_session_summaries(),
            "web_admin": {
                "available": bool(self._web_admin_available and self.app),
                "enabled": bool(self.config.get("web_admin", {}).get("enabled", False)),
                "url": self._build_web_admin_url(),
                "auth_required": self._auth_enabled,
            },
            "timestamp": datetime.now().isoformat(),
        }

    def _collect_jobs(self) -> list[dict[str, Any]]:
        jobs = []
        seen_sessions: set[str] = set()

        for job in self.plugin.scheduler.get_jobs() if self.plugin.scheduler else []:
            session_id = str(job.id)
            normalized_session_id = self.plugin._normalize_session_id(session_id)
            seen_sessions.add(normalized_session_id)
            session_data = self.plugin.session_data.get(session_id, {})
            session_config = self.plugin._get_session_config(session_id) or {}
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            unanswered_count = session_data.get("unanswered_count", 0)
            if self.plugin._is_unanswered_limit_reached(
                normalized_session_id, session_config, unanswered_count
            ):
                continue
            jobs.append(
                {
                    "id": session_id,
                    "status": "scheduled",
                    "status_label": "已调度",
                    "session_name": self.plugin._get_session_name(
                        session_id, session_config
                    ),
                    "session_display_name": self.plugin._get_session_display_name(
                        session_id, session_config
                    ),
                    "session_category": self._detect_session_category(session_id),
                    "source_mode": context_settings.get(
                        "source_mode", "conversation_history"
                    ),
                    "max_unanswered_times": self.plugin._get_max_unanswered_count(
                        session_config
                    ),
                    # APScheduler 的 next_run_time 是 datetime，这里统一序列化为 ISO 字符串。
                    "next_run_time": (
                        job.next_run_time.isoformat() if job.next_run_time else None
                    ),
                    "unanswered_count": unanswered_count,
                    "manual_trigger_in_progress": session_id
                    in self.plugin.manual_trigger_sessions,
                    # 以下字段用于前端推导进度条与调度窗口说明。
                    "next_trigger_time": session_data.get("next_trigger_time"),
                    "last_scheduled_at": session_data.get("last_scheduled_at"),
                    "last_schedule_min_interval_seconds": session_data.get(
                        "last_schedule_min_interval_seconds"
                    ),
                    "last_schedule_max_interval_seconds": session_data.get(
                        "last_schedule_max_interval_seconds"
                    ),
                    "last_schedule_random_interval_seconds": session_data.get(
                        "last_schedule_random_interval_seconds"
                    ),
                    "last_schedule_strategy": session_data.get(
                        "last_schedule_strategy"
                    ),
                    "last_schedule_reason": session_data.get("last_schedule_reason"),
                    "last_schedule_rule": session_data.get("last_schedule_rule"),
                    "last_schedule_source": session_data.get("last_schedule_source"),
                    # 透出当前会话配置中的调度区间与免打扰时段，供任务卡片展示。
                    "schedule_min_interval_minutes": schedule_settings.get(
                        "min_interval_minutes"
                    ),
                    "schedule_max_interval_minutes": schedule_settings.get(
                        "max_interval_minutes"
                    ),
                    "quiet_hours": schedule_settings.get("quiet_hours", ""),
                }
            )

        for session_id in self._list_known_sessions():
            normalized_session_id = self.plugin._normalize_session_id(str(session_id))
            if normalized_session_id in seen_sessions:
                continue

            session_config = self.plugin._get_session_config(normalized_session_id)
            if not session_config or not session_config.get("enable", False):
                continue

            session_data = self.plugin.session_data.get(
                normalized_session_id, self.plugin.session_data.get(str(session_id), {})
            )
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            unanswered_count = session_data.get("unanswered_count", 0)
            if self.plugin._is_unanswered_limit_reached(
                normalized_session_id, session_config, unanswered_count
            ):
                continue
            next_trigger_time = session_data.get("next_trigger_time")
            next_run_time = None
            if next_trigger_time:
                try:
                    next_run_time = datetime.fromtimestamp(
                        float(next_trigger_time),
                        tz=getattr(self.plugin, "timezone", None),
                    ).isoformat()
                except Exception:
                    next_run_time = None

            jobs.append(
                {
                    "id": normalized_session_id,
                    "status": "pending_schedule",
                    "status_label": "待调度",
                    "session_name": self.plugin._get_session_name(
                        normalized_session_id, session_config
                    ),
                    "session_display_name": self.plugin._get_session_display_name(
                        normalized_session_id, session_config
                    ),
                    "session_category": self._detect_session_category(
                        normalized_session_id
                    ),
                    "source_mode": context_settings.get(
                        "source_mode", "conversation_history"
                    ),
                    "max_unanswered_times": self.plugin._get_max_unanswered_count(
                        session_config
                    ),
                    "next_run_time": next_run_time,
                    "unanswered_count": unanswered_count,
                    "manual_trigger_in_progress": normalized_session_id
                    in self.plugin.manual_trigger_sessions,
                    "next_trigger_time": next_trigger_time,
                    "last_scheduled_at": session_data.get("last_scheduled_at"),
                    "last_schedule_min_interval_seconds": session_data.get(
                        "last_schedule_min_interval_seconds"
                    ),
                    "last_schedule_max_interval_seconds": session_data.get(
                        "last_schedule_max_interval_seconds"
                    ),
                    "last_schedule_random_interval_seconds": session_data.get(
                        "last_schedule_random_interval_seconds"
                    ),
                    "last_schedule_strategy": session_data.get(
                        "last_schedule_strategy"
                    ),
                    "last_schedule_reason": session_data.get("last_schedule_reason"),
                    "last_schedule_rule": session_data.get("last_schedule_rule"),
                    "last_schedule_source": session_data.get("last_schedule_source"),
                    "schedule_min_interval_minutes": schedule_settings.get(
                        "min_interval_minutes"
                    ),
                    "schedule_max_interval_minutes": schedule_settings.get(
                        "max_interval_minutes"
                    ),
                    "quiet_hours": schedule_settings.get("quiet_hours", ""),
                    "inactive_reason": "当前没有 APScheduler 任务，可能正在等待下一次触发条件或需要重新调度",
                }
            )

        jobs.sort(
            key=lambda item: (
                item.get("next_run_time") is None,
                item.get("next_run_time") or "",
                item.get("id") or "",
            )
        )
        return jobs

    def _list_known_sessions(self) -> list[str]:
        sessions: set[str] = set()

        # 先收集全局配置里显式声明的会话。
        for scope_key in ("friend_settings", "group_settings"):
            cfg = self.config.get(scope_key, {})
            for session in cfg.get("session_list", []):
                if isinstance(session, str) and session:
                    sessions.add(self.plugin._normalize_session_id(session))

        # 再并入运行时数据与会话覆写记录，保证“曾经出现过”的会话也能在管理端看到。
        sessions.update(self.plugin.session_data.keys())
        sessions.update(self.plugin.session_override_manager.list_sessions())
        return sorted(sessions)

    def _list_known_session_summaries(self) -> list[dict[str, Any]]:
        """返回带展示信息的已知会话摘要（供 WS 实时推送使用）。"""
        result: list[dict[str, Any]] = []
        for session in self._list_known_sessions():
            effective = self.plugin._get_session_config(session)
            session_data = self.plugin.session_data.get(session, {})
            schedule_settings = (effective or {}).get("schedule_settings", {})
            auto_trigger_settings = (effective or {}).get("auto_trigger_settings", {})
            result.append(
                {
                    "session": session,
                    "session_name": self.plugin._get_session_name(session, effective),
                    "session_display_name": self.plugin._get_session_display_name(
                        session, effective
                    ),
                    # has_override 让前端在摘要态就能知道这个会话是否存在局部改写。
                    "has_override": bool(
                        self.plugin.session_override_manager.get_override(session)
                    ),
                    "unanswered_count": self.plugin.session_data.get(session, {}).get(
                        "unanswered_count", 0
                    ),
                    "max_unanswered_times": self.plugin._get_max_unanswered_count(
                        effective
                    ),
                    "manual_trigger_in_progress": session
                    in self.plugin.manual_trigger_sessions,
                    "enabled": bool(effective and effective.get("enable", False)),
                    "session_category": self._detect_session_category(session),
                    "next_trigger_time": session_data.get("next_trigger_time"),
                    "last_scheduled_at": session_data.get("last_scheduled_at"),
                    "last_schedule_min_interval_seconds": session_data.get(
                        "last_schedule_min_interval_seconds"
                    ),
                    "last_schedule_max_interval_seconds": session_data.get(
                        "last_schedule_max_interval_seconds"
                    ),
                    "last_schedule_random_interval_seconds": session_data.get(
                        "last_schedule_random_interval_seconds"
                    ),
                    "last_schedule_strategy": session_data.get(
                        "last_schedule_strategy"
                    ),
                    "last_schedule_reason": session_data.get("last_schedule_reason"),
                    "last_schedule_rule": session_data.get("last_schedule_rule"),
                    "last_schedule_source": session_data.get("last_schedule_source"),
                    "schedule_min_interval_minutes": schedule_settings.get(
                        "min_interval_minutes"
                    ),
                    "schedule_max_interval_minutes": schedule_settings.get(
                        "max_interval_minutes"
                    ),
                    "auto_trigger_after_minutes": auto_trigger_settings.get(
                        "auto_trigger_after_minutes"
                    ),
                }
            )
        return result

    async def _build_notification_payload(self) -> dict[str, Any]:
        # 统一封装通知载荷构造，避免 HTTP 路由、首次 WS 快照和增量广播各自重复拼装。
        if not getattr(self.plugin, "notification_center", None):
            return {
                "items": [],
                "meta": {
                    "unread_count": 0,
                    "last_sync_at": None,
                    "total_count": 0,
                },
            }
        return await self.plugin.notification_center.get_payload()

    def _list_markdown_documents(self) -> list[dict[str, Any]]:
        """列出允许浏览的 Markdown 文档摘要。"""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        plugin_root = Path(__file__).resolve().parent.parent.resolve()
        docs_root = (plugin_root / "docs").resolve()

        allowed_paths: list[Path] = []

        # 插件根目录只暴露顶层 Markdown 文档，避免把实现目录中的内部文档一并暴露出来。
        if plugin_root.exists():
            allowed_paths.extend(sorted(plugin_root.glob("*.md")))

        # docs 目录作为显式文档区，允许递归收集其中的所有 Markdown 文件。
        if docs_root.exists():
            allowed_paths.extend(sorted(docs_root.rglob("*.md")))

        for path in allowed_paths:
            if not path.is_file():
                continue

            try:
                # 所有路径统一转为插件工作区相对路径，方便前端展示与请求。
                relative_path = self._to_workspace_relative_path(path)
            except ValueError:
                # 若文件不在工作区内，说明超出允许范围，直接忽略。
                continue

            normalized = relative_path.replace("\\", "/")
            if normalized in seen:
                continue
            seen.add(normalized)

            items.append(
                {
                    "path": normalized,
                    # title 面向展示，filename 更偏向调试或原始文件识别。
                    "title": path.stem,
                    "filename": path.name,
                    # category 便于前端未来按目录做分组；插件根目录下文件统一标为 root。
                    "category": "root"
                    if path.parent.resolve() == plugin_root
                    else path.parent.name,
                }
            )

        # 优先展示根目录文档，再按路径字母序排序，通常更符合 README / CHANGELOG 的阅读优先级。
        items.sort(
            key=lambda item: (
                0 if item["path"].count("/") == 0 else 1,
                item["path"].lower(),
            )
        )
        return items

    def _resolve_markdown_document(self, raw_path: str) -> Path | None:
        """将前端请求的 Markdown 相对路径解析为插件目录中的受信任文件。"""
        normalized = str(raw_path or "").strip().replace("\\", "/")
        if not normalized or not normalized.lower().endswith(".md"):
            return None
        # 明确拒绝绝对路径与上级目录跳转，防止路径穿越访问到插件目录外的文件。
        if (
            normalized.startswith("/")
            or normalized.startswith("../")
            or "/../" in normalized
        ):
            return None

        plugin_root = Path(__file__).resolve().parent.parent.resolve()
        docs_root = (plugin_root / "docs").resolve()
        candidate = (plugin_root / normalized).resolve()

        if not candidate.is_file():
            return None

        try:
            relative_path = candidate.relative_to(plugin_root)
        except ValueError:
            return None

        # 根目录仅允许访问顶层 Markdown；docs 目录允许访问其内部任意层级 Markdown。
        if relative_path.parent == Path("."):
            return candidate

        try:
            candidate.relative_to(docs_root)
            return candidate
        except ValueError:
            return None

    def _to_workspace_relative_path(self, path: Path) -> str:
        """将绝对路径转换为插件工作区内的相对路径。"""
        plugin_root = Path(__file__).resolve().parent.parent.resolve()
        return str(path.resolve().relative_to(plugin_root)).replace("\\", "/")

    async def _broadcast_ws_payload(self, payload: dict[str, Any]) -> None:
        # 广播发送与失活连接清理抽到公共方法，避免多个广播入口重复维护同一逻辑。
        to_remove: list[WebSocket] = []
        for ws in list(self._ws_connections):
            try:
                await ws.send_json(payload)
            except Exception:
                # 某些连接可能已失活，先记录下来，循环结束后统一清理。
                to_remove.append(ws)

        for ws in to_remove:
            if ws in self._ws_connections:
                self._ws_connections.remove(ws)

    async def _broadcast_update(self, reason: str) -> None:
        # 若当前没有任何活跃前端连接，则无需构造完整广播载荷，可直接返回。
        if not self._ws_connections:
            return

        payload = {
            "type": "update",
            # reason 主要供前端调试与按需决定是否额外提示某类更新来源。
            "reason": reason,
            "data": {
                "status": self._build_status_payload(),
                "jobs": self._collect_jobs(),
                "sessions": self._list_known_session_summaries(),
                "notifications": await self._build_notification_payload(),
            },
        }
        await self._broadcast_ws_payload(payload)

    async def _broadcast_notification_meta_update(self, reason: str) -> None:
        # 轻量广播仅同步通知元信息，避免在轮询无内容变更时重复发送完整通知列表。
        if not self._ws_connections:
            return

        if not getattr(self.plugin, "notification_center", None):
            notification_meta = {
                "unread_count": 0,
                "last_sync_at": None,
                "total_count": 0,
            }
        else:
            notification_meta = await self.plugin.notification_center.get_meta()
        payload = {
            "type": "update",
            "reason": reason,
            "data": {
                "notificationsMeta": notification_meta,
            },
        }
        await self._broadcast_ws_payload(payload)

    async def start(self) -> None:
        if not FASTAPI_AVAILABLE:
            logger.error("[主动消息] 无法启动 Web 管理端喵: FastAPI 未安装")
            return

        if not self._web_admin_available or not self.app:
            detail = (
                f" 初始化失败原因: {self._web_admin_init_error}"
                if self._web_admin_init_error
                else ""
            )
            logger.error(
                "[主动消息] 无法启动 Web 管理端喵: 初始化未完成或依赖不兼容，已自动禁用。"
                f"{detail}"
            )
            return

        web_admin = self.config.get("web_admin", {})
        if not web_admin.get("enabled", False):
            logger.info("[主动消息] Web 管理端未启用喵。")
            return

        host = web_admin.get("host", "127.0.0.1")
        port = int(web_admin.get("port", 4100))

        # 采用 Uvicorn 内嵌启动，便于作为插件内部协程任务运行。
        uv_cfg = uvicorn.Config(
            self.app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(uv_cfg)

        async def _serve():
            try:
                await self.server.serve()
            except Exception as e:
                logger.error(f"[主动消息] Web 管理端运行异常喵: {e}")

        self.server_task = asyncio.create_task(_serve())

        async def _cleanup_tokens_loop():
            while True:
                try:
                    await asyncio.sleep(3600)  # 每小时清理一次
                    now = time.time()
                    expired = [k for k, v in self._tokens.items() if now > v]
                    for k in expired:
                        self._tokens.pop(k, None)
                    if expired:
                        logger.debug(f"[主动消息] 已清理 {len(expired)} 个过期令牌喵。")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"[主动消息] 清理过期令牌异常喵: {e}")

        if self._auth_enabled:
            self._token_cleanup_task = asyncio.create_task(_cleanup_tokens_loop())

        # 略等一个事件循环切片，让服务有机会完成绑定后再打印启动日志。
        await asyncio.sleep(0.1)
        logger.info(f"[主动消息] Web 管理端已启动喵: http://{host}:{port}")

    async def stop(self) -> None:
        if self._token_cleanup_task:
            self._token_cleanup_task.cancel()
        if self.server:
            # 通知 Uvicorn 进入优雅退出流程。
            self.server.should_exit = True

        if self.server_task:
            try:
                # 最多等待 5 秒，避免插件卸载时无限阻塞。
                await asyncio.wait_for(self.server_task, timeout=5)
            except Exception:
                pass

        self._ws_connections.clear()
        logger.info("[主动消息] Web 管理端已停止喵。")
