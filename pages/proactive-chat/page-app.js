(function () {
    "use strict";

    var PLUGIN_REPO = "https://github.com/DBJD-CR/astrbot_plugin_proactive_chat";
    var state = {
        view: "status",
        bridge: null,
        bridgeReady: false,
        error: "",
        status: {},
        jobs: [],
        sessions: [],
        config: null,
        configSchema: null,
        configMode: "global",
        expandedKeys: [],
        saveFeedback: "",
        saveFeedbackType: "",
        sessionConfigState: { baseAvailable: true, message: "" },
        selectedSession: "",
        sessionDetail: null,
        theme: safeStorageGet("theme") || "light",
        busy: {},
        realtimeTimer: null,
        loadingSnapshot: false,
        lastRealtimeAt: 0
    };

    var viewMeta = {
        status: { label: "运行状态", icon: "📊", subtitle: "服务状态、调度概览与会话计时器" },
        tasks: { label: "任务管理", icon: "📋", subtitle: "查看、立即触发、重新调度或取消会话任务" },
        config: { label: "配置管理", icon: "⚙️", subtitle: "编辑全局配置与会话差异配置" }
    };

    function safeStorageGet(key) {
        try {
            return localStorage.getItem(key);
        } catch (e) {
            return "";
        }
    }

    function safeStorageSet(key, value) {
        try {
            localStorage.setItem(key, value);
        } catch (e) {}
    }

    function $(id) {
        return document.getElementById(id);
    }

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function text(value, fallback) {
        if (value === null || value === undefined || value === "") return fallback || "--";
        return String(value);
    }

    function asArray(value) {
        return Array.isArray(value) ? value : [];
    }

    function objectKeys(value) {
        return value && typeof value === "object" ? Object.keys(value) : [];
    }

    function closest(node, selector) {
        while (node && node !== document) {
            if (node.matches && node.matches(selector)) return node;
            node = node.parentNode;
        }
        return null;
    }

    function parsePath(path) {
        return String(path || "").split(".").filter(Boolean);
    }

    function getByPath(source, path) {
        var parts = parsePath(path);
        var current = source;
        for (var i = 0; i < parts.length; i += 1) {
            if (!current || typeof current !== "object") return undefined;
            current = current[parts[i]];
        }
        return current;
    }

    function setByPath(source, path, value) {
        var parts = parsePath(path);
        var current = source;
        for (var i = 0; i < parts.length - 1; i += 1) {
            var key = parts[i];
            if (!current[key] || typeof current[key] !== "object" || Array.isArray(current[key])) {
                current[key] = {};
            }
            current = current[key];
        }
        if (parts.length) current[parts[parts.length - 1]] = value;
    }

    function stripLeadingEmoji(value) {
        return String(value || "").replace(/^[\s\u2600-\u27BF\uD800-\uDBFF][\s\uFE0F\u200D\uDC00-\uDFFF]*/g, "").trim() || String(value || "");
    }

    function setBusy(key, value) {
        state.busy[key] = Boolean(value);
        render();
    }

    function setError(message) {
        state.error = message || "";
        render();
    }

    function formatDuration(totalSeconds) {
        var seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
        var days = Math.floor(seconds / 86400);
        var hours = Math.floor((seconds % 86400) / 3600);
        var minutes = Math.floor((seconds % 3600) / 60);
        var secs = seconds % 60;
        var parts = [];
        if (days) parts.push(days + "天");
        if (hours) parts.push(hours + "小时");
        if (minutes) parts.push(minutes + "分");
        if (secs || !parts.length) parts.push(secs + "秒");
        return parts.slice(0, 3).join("");
    }

    function formatDate(value) {
        if (!value) return "--";
        var date = new Date(value);
        if (isNaN(date.getTime())) return text(value);
        return date.toLocaleString("zh-CN", {
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit"
        });
    }

    function timestampMs(value) {
        if (value === null || value === undefined || value === "") return null;
        if (typeof value === "number") {
            return value > 100000000000 ? value : value * 1000;
        }
        var raw = String(value);
        if (/^\d+(\.\d+)?$/.test(raw)) {
            var numeric = Number(raw);
            return numeric > 100000000000 ? numeric : numeric * 1000;
        }
        var date = new Date(raw);
        return isNaN(date.getTime()) ? null : date.getTime();
    }

    function clampPercent(value) {
        var number = Math.round(Number(value) || 0);
        return Math.max(0, Math.min(100, number));
    }

    function stableJson(value) {
        if (Array.isArray(value)) {
            return "[" + value.map(stableJson).join(",") + "]";
        }
        if (value && typeof value === "object") {
            var keys = Object.keys(value).sort();
            var parts = [];
            for (var i = 0; i < keys.length; i += 1) {
                parts.push(JSON.stringify(keys[i]) + ":" + stableJson(value[keys[i]]));
            }
            return "{" + parts.join(",") + "}";
        }
        return JSON.stringify(value);
    }

    function sameConfigValue(left, right) {
        return stableJson(left || {}) === stableJson(right || {});
    }

    function normalizePayload(payload) {
        if (!payload || typeof payload !== "object") return payload || {};
        if (payload.error) {
            throw new Error(typeof payload.error === "string" ? payload.error : payload.error.message || "请求失败");
        }
        if (payload.ok === false || payload.success === false) {
            throw new Error(payload.message || "请求失败");
        }
        if (typeof payload.code === "number" && payload.code !== 0) {
            throw new Error(payload.message || payload.msg || "请求失败 (" + payload.code + ")");
        }
        if ((payload.ok === true || payload.success === true || payload.code === 0) && Object.prototype.hasOwnProperty.call(payload, "data")) {
            return normalizePayload(payload.data || {});
        }
        return payload;
    }

    function waitBridge(timeoutMs) {
        return new Promise(function (resolve) {
            var started = Date.now();
            if (window.AstrBotPluginPage) {
                resolve(window.AstrBotPluginPage);
                return;
            }
            var timer = setInterval(function () {
                if (window.AstrBotPluginPage) {
                    clearInterval(timer);
                    resolve(window.AstrBotPluginPage);
                    return;
                }
                if (Date.now() - started > timeoutMs) {
                    clearInterval(timer);
                    resolve(null);
                }
            }, 60);
        });
    }

    function apiGet(endpoint) {
        if (!state.bridge || typeof state.bridge.apiGet !== "function") {
            return Promise.reject(new Error("AstrBot Pages bridge 未注入，请从 AstrBot WebUI 的插件页面重新打开。"));
        }
        return Promise.resolve(state.bridge.apiGet(endpoint)).then(normalizePayload);
    }

    function apiPost(endpoint, body) {
        if (!state.bridge || typeof state.bridge.apiPost !== "function") {
            return Promise.reject(new Error("AstrBot Pages bridge 未注入，请从 AstrBot WebUI 的插件页面重新打开。"));
        }
        return Promise.resolve(state.bridge.apiPost(endpoint, body || {})).then(normalizePayload);
    }

    function directBridgePost(endpoint, body) {
        if (!state.bridge || typeof state.bridge.apiPost !== "function") {
            return Promise.reject(new Error("AstrBot Pages bridge 未注入，请从 AstrBot WebUI 的插件页面重新打开。"));
        }
        // 保存配置走裸 endpoint，用 received 标记确认命中了本插件的新 Pages 后端。
        return Promise.resolve(state.bridge.apiPost(endpoint, body || {})).then(normalizePayload);
    }

    function route(endpoint) {
        return endpoint;
    }

    function hideBoot() {
        var boot = $("loading-skeleton");
        if (!boot) return;
        boot.classList.add("is-exiting");
        setTimeout(function () {
            if (boot.parentNode) boot.parentNode.removeChild(boot);
        }, 220);
    }

    function initTheme() {
        document.body.classList.toggle("dark-theme", state.theme === "dark");
        document.documentElement.classList.toggle("theme-dark", state.theme === "dark");
    }

    function toggleTheme() {
        state.theme = state.theme === "dark" ? "light" : "dark";
        safeStorageSet("theme", state.theme);
        initTheme();
        renderHeader();
    }

    function navButton(key) {
        var meta = viewMeta[key];
        return [
            '<button class="pc-nav-button', state.view === key ? " is-active" : "", '" data-view="', key, '">',
            '<span class="pc-nav-icon">', meta.icon, '</span>',
            '<span>', meta.label, '</span>',
            '</button>'
        ].join("");
    }

    function shellHtml() {
        return [
            '<div class="pc-app">',
            '<aside class="pc-sidebar">',
            '<div class="pc-brand">',
            '<div class="pc-logo-mark" aria-label="主动消息"><span>主</span></div>',
            '<div><div class="pc-brand-title">主动消息</div><div class="pc-brand-subtitle">Admin Console</div></div>',
            '</div>',
            '<nav class="pc-nav">', navButton("status"), navButton("tasks"), navButton("config"), '</nav>',
            '<a class="pc-github-card" href="', PLUGIN_REPO, '" target="_blank" rel="noopener noreferrer">',
            '<div class="pc-github-author">@DBJD-CR</div>',
            '<div class="pc-github-title">🔧 (主动消息) ... 点个 Star 吧~ ⭐</div>',
            '</a>',
            '</aside>',
            '<main class="pc-main">',
            '<header class="pc-topbar" id="pc-topbar"></header>',
            '<div class="pc-content">',
            '<div id="pc-error"></div>',
            '<section id="view-status" class="pc-section"></section>',
            '<section id="view-tasks" class="pc-section"></section>',
            '<section id="view-config" class="pc-section"></section>',
            '</div>',
            '</main>',
            '</div>'
        ].join("");
    }

    function renderShell() {
        $("root").innerHTML = shellHtml();
        bindShellEvents();
        render();
    }

    function bindShellEvents() {
        document.addEventListener("click", function (event) {
            var viewBtn = closest(event.target, "[data-view]");
            if (viewBtn) {
                switchView(viewBtn.getAttribute("data-view"));
                return;
            }
            var action = closest(event.target, "[data-action]");
            if (!action) return;
            handleAction(action.getAttribute("data-action"), action);
        });
        document.addEventListener("change", function (event) {
            if (event.target && event.target.id === "session-select") {
                state.selectedSession = event.target.value;
                state.expandedKeys = [];
                loadSessionDetail(state.selectedSession);
            }
            if (event.target && event.target.getAttribute("data-config-path")) {
                updateConfigFromControl(event.target, true);
            }
        });
        document.addEventListener("input", function (event) {
            if (event.target && event.target.getAttribute("data-config-path")) {
                updateConfigFromControl(event.target, false);
            }
        });
    }

    function handleAction(action, node) {
        if (action === "refresh") loadCurrentView();
        if (action === "theme") toggleTheme();
        if (action === "trigger-job") triggerJob(node.getAttribute("data-id"));
        if (action === "reschedule-job") rescheduleJob(node.getAttribute("data-id"));
        if (action === "cancel-job") cancelJob(node.getAttribute("data-id"));
        if (action === "save-config") saveConfig();
        if (action === "load-config") loadConfig();
        if (action === "save-session") saveSessionConfig();
        if (action === "reset-session") resetSessionConfig();
        if (action === "config-mode") switchConfigMode(node.getAttribute("data-mode"));
        if (action === "toggle-config") toggleConfigGroup(node.getAttribute("data-path"));
        if (action === "toggle-all-config") toggleAllConfigGroups();
        if (action === "reset-config-defaults") resetConfigDefaults();
        if (action === "discard-config") loadConfig();
    }

    function switchView(view) {
        if (!viewMeta[view]) return;
        state.view = view;
        render();
        loadCurrentView();
    }

    function loadCurrentView() {
        if (state.view === "status") loadDashboard();
        if (state.view === "tasks") loadJobs();
        if (state.view === "config") loadConfig();
    }

    function renderHeader() {
        var meta = viewMeta[state.view];
        var status = state.status || {};
        var connected = state.bridgeReady;
        $("pc-topbar").innerHTML = [
            '<div><div class="pc-title">', escapeHtml(meta.label), '</div>',
            '<div class="pc-subtitle">', escapeHtml(meta.subtitle), '</div></div>',
            '<div class="pc-topbar-actions">',
            '<span class="pc-chip">🕒 <span id="pc-clock">', escapeHtml(formatDate(new Date())), '</span></span>',
            '<span class="pc-chip ', connected ? "is-ok" : "is-warn", '">', connected ? "已连接 Pages bridge" : "未连接 Pages bridge", '</span>',
            '<span class="pc-chip">同步 ', state.lastRealtimeAt ? escapeHtml(formatDate(state.lastRealtimeAt)) : '--', '</span>',
            '<span class="pc-chip">WebSocket ', Number(status.ws_connections || 0), ' 个</span>',
            '<button class="pc-icon-button" data-action="refresh" title="刷新">↻</button>',
            '<button class="pc-icon-button" data-action="theme" title="切换主题">', state.theme === "dark" ? "☀" : "🌙", '</button>',
            '</div>'
        ].join("");
    }

    function renderError() {
        $("pc-error").innerHTML = state.error ? '<div class="pc-error">' + escapeHtml(state.error) + '</div>' : "";
    }

    function render() {
        if (!$("pc-topbar")) return;
        initTheme();
        renderHeader();
        renderError();
        var keys = ["status", "tasks", "config"];
        for (var i = 0; i < keys.length; i += 1) {
            var section = $("view-" + keys[i]);
            if (section) section.classList.toggle("is-active", state.view === keys[i]);
        }
        var navs = document.querySelectorAll("[data-view]");
        for (var n = 0; n < navs.length; n += 1) {
            navs[n].classList.toggle("is-active", navs[n].getAttribute("data-view") === state.view);
        }
        renderStatus();
        renderTasks();
        renderConfig();
    }

    function metric(label, value, hint) {
        return [
            '<article class="pc-card pc-metric">',
            '<div><div class="pc-metric-label">', escapeHtml(label), '</div>',
            '<div class="pc-metric-value">', escapeHtml(value), '</div></div>',
            '<div class="pc-card-subtitle">', escapeHtml(hint || ""), '</div>',
            '</article>'
        ].join("");
    }

    function renderStatus() {
        var status = state.status || {};
        var timerCards = collectTimerCards(status);
        var autoCards = timerCards.auto;
        var groupCards = timerCards.group;
        var fallbackCards = timerCards.fallback;
        var scheduledAutoCards = fallbackCards.filter(function (card) {
            return detectSessionType(card.session_id || card.session || card.id) !== "group";
        });
        var scheduledGroupCards = fallbackCards.filter(function (card) {
            return detectSessionType(card.session_id || card.session || card.id) === "group";
        });
        var visibleAutoTriggerCount = autoCards.length + scheduledAutoCards.length;
        var visibleGroupTimerCount = groupCards.length + scheduledGroupCards.length;
        var autoTriggerSummary = visibleAutoTriggerCount + " 个";
        if (scheduledAutoCards.length) autoTriggerSummary += "（实时 " + autoCards.length + " / 调度 " + scheduledAutoCards.length + "）";
        var groupTimerSummary = visibleGroupTimerCount + " 个";
        if (scheduledGroupCards.length) groupTimerSummary += "（实时 " + groupCards.length + " / 调度 " + scheduledGroupCards.length + "）";
        var allCards = groupCards.concat(autoCards).concat(fallbackCards);
        var realJobsCount = Number(status.jobs_count || 0);
        var visibleJobsCount = Math.max(realJobsCount, asArray(state.jobs).length);
        var pendingJobsCount = Math.max(0, visibleJobsCount - realJobsCount);
        $("view-status").innerHTML = [
            '<div class="pc-grid metrics">',
            metric("插件状态", status.running ? "运行中" : "已停止", "版本 " + text(status.version, "...")),
            metric("运行时长", formatDuration(status.uptime_seconds), "启动后持续运行时间"),
            metric("调度任务", text(visibleJobsCount, "0"), (status.scheduler_running ? "调度器运行中" : "调度器未启动") + " · 已调度 " + realJobsCount + " / 待调度 " + pendingJobsCount),
            metric("会话数据", text(status.sessions_count, "0"), "自动/会话触发 " + visibleAutoTriggerCount + " / 群沉默 " + visibleGroupTimerCount),
            '</div>',
            '<div class="pc-grid two">',
            '<div class="pc-card"><div class="pc-card-header"><div><div class="pc-card-title">会话计时器可视化</div><div class="pc-card-subtitle">实时展示自动触发检测与群沉默检测的倒计时、进度和会话状态。</div></div></div>',
            renderTimerList(allCards), '</div>',
            '<div class="pc-card"><div class="pc-card-title">调度概览</div>',
            '<div class="pc-list" style="margin-top:14px">',
            infoRow("调度器", status.scheduler_running ? "运行中" : "未启动"),
            infoRow("当前任务总数", visibleJobsCount + " 个"),
            pendingJobsCount ? infoRow("待调度会话", pendingJobsCount + " 个") : "",
            infoRow("自动/会话触发计时器", autoTriggerSummary),
            infoRow("群沉默计时器", groupTimerSummary),
            fallbackCards.length ? infoRow("其中会话调度倒计时", fallbackCards.length + " 个") : "",
            infoRow("数据时间", formatDate(status.timestamp)),
            '</div></div></div>'
        ].join("");
    }

    function infoRow(label, value) {
        return '<div class="pc-row"><div class="pc-row-title">' + escapeHtml(label) + '</div><div class="pc-row-meta">' + escapeHtml(value) + '</div></div>';
    }

    function collectTimerCards(status) {
        var autoCards = asArray(status.auto_trigger_cards);
        var groupCards = asArray(status.group_timer_cards);
        var seen = {};
        var seenSessions = {};
        var fallbackSessions = {};
        var fallback = [];
        var jobs = asArray(state.jobs);

        function cardSessionId(card) {
            return String(card && (card.session_id || card.session || card.id) || "");
        }

        function cardHasTarget(card) {
            return !!(card && (card.target_time || card.next_trigger_time || card.next_run_time || card.remaining_seconds !== null && card.remaining_seconds !== undefined));
        }

        function mark(card) {
            var sessionId = cardSessionId(card);
            var key = String(card.timer_kind || "timer") + ":" + sessionId;
            if (key !== "timer:") seen[key] = true;
            if (sessionId && cardHasTarget(card)) seenSessions[sessionId] = true;
        }
        for (var a = 0; a < autoCards.length; a += 1) mark(autoCards[a] || {});
        for (var g = 0; g < groupCards.length; g += 1) mark(groupCards[g] || {});

        function pushFallback(source, kind) {
            source = source || {};
            var id = source.session || source.id || source.session_id || "";
            var targetRaw = source.next_trigger_time || source.next_run_time;
            var target = timestampMs(targetRaw);
            if (!id || !target) return;
            var key = kind + ":" + id;
            if (seen[key] || seenSessions[id]) return;
            var remaining = Math.max(0, Math.ceil((target - Date.now()) / 1000));
            var windowSeconds = Number(source.last_schedule_random_interval_seconds || source.last_schedule_max_interval_seconds || 0);
            if (!windowSeconds && source.schedule_max_interval_minutes) windowSeconds = Number(source.schedule_max_interval_minutes) * 60;
            var started = timestampMs(source.last_scheduled_at);
            var progress = 0;
            if (started && target > started) {
                progress = clampPercent(((Date.now() - started) / (target - started)) * 100);
            } else if (windowSeconds > 0) {
                progress = clampPercent(((windowSeconds - remaining) / windowSeconds) * 100);
            }
            fallback.push({
                session_id: id,
                session: id,
                session_name: source.session_name,
                session_display_name: source.session_display_name,
                session_category: source.session_category,
                timer_kind: kind,
                timer_kind_label: kind === "scheduled_job" ? "调度任务" : "下次触发",
                status: remaining <= 0 ? "expired" : "running",
                remaining_seconds: remaining,
                target_time: target / 1000,
                window_seconds: windowSeconds,
                progress_percent: progress,
                unanswered_count: source.unanswered_count,
                max_unanswered_times: source.max_unanswered_times,
                last_schedule_strategy: source.last_schedule_strategy,
                last_schedule_reason: source.last_schedule_reason,
                last_schedule_rule: source.last_schedule_rule,
                last_schedule_source: source.last_schedule_source
            });
            seen[key] = true;
            seenSessions[id] = true;
            fallbackSessions[id] = true;
        }

        for (var j = 0; j < jobs.length; j += 1) pushFallback(jobs[j], "scheduled_job");
        fallback.sort(function (left, right) {
            return Number(left.remaining_seconds || 0) - Number(right.remaining_seconds || 0);
        });
        function keepPrimaryCard(card) {
            var id = cardSessionId(card);
            return !id || !fallbackSessions[id] || cardHasTarget(card);
        }
        return { auto: autoCards.filter(keepPrimaryCard), group: groupCards.filter(keepPrimaryCard), fallback: fallback };
    }

    function timerStatusLabel(item) {
        if (item.status === "waiting_message") return "等待消息";
        if (item.status === "waiting_idle") return "等待空闲";
        if (item.status === "pending_timer") return "未挂起";
        if (item.status === "unknown") return "待确认";
        if (item.remaining_seconds === null || item.remaining_seconds === undefined) return "未启动";
        if (Number(item.remaining_seconds || 0) <= 0) return "待刷新";
        if (Number(item.remaining_seconds || 0) <= 300) return "即将触发";
        return "计时中";
    }

    function scheduleStrategyLabel(item) {
        var strategy = String(item && item.last_schedule_strategy || "").toLowerCase();
        var source = String(item && item.last_schedule_source || "").toLowerCase();
        if (strategy === "contextual" || source === "recent_context") return "语境预测";
        if (strategy === "random" || source === "random_interval") return "随机区间";
        return "";
    }

    function scheduleReasonLabel(item) {
        var rule = String(item && item.last_schedule_rule || "");
        var reason = String(item && item.last_schedule_reason || "");
        var labels = {
            explicit_delay: "明确延后",
            tomorrow: "明天再聊",
            do_not_disturb: "暂不打扰",
            sleep_night: "睡眠休息",
            movie: "观影追剧",
            meeting_or_class: "会议课程",
            commute: "通勤路上",
            meal: "用餐时间",
            shower: "短时离开",
            game: "游戏中",
            short_later: "稍后再聊"
        };
        if (labels[rule]) return labels[rule];
        if (reason.indexOf("context:explicit_delay:") === 0) {
            return "明确延后 " + reason.replace("context:explicit_delay:", "");
        }
        return reason;
    }

    function renderTimerList(items) {
        if (!items.length) return '<div class="pc-empty">🫧 暂无运行中的会话计时器</div>';
        var html = ['<div class="pc-timer-list">'];
        for (var i = 0; i < items.length; i += 1) {
            var item = items[i] || {};
            var progress = clampPercent(item.progress_percent);
            var hasCountdown = item.remaining_seconds !== null && item.remaining_seconds !== undefined;
            var remaining = hasCountdown ? formatDuration(item.remaining_seconds) : (item.status === "waiting_message" ? "等待消息" : "--");
            var countdownSuffix = hasCountdown ? " 后触发" : (item.status === "waiting_message" ? " 后开始" : " 待启动");
            var targetText = item.target_time ? formatDate(Number(item.target_time) * 1000) : "--";
            var chipClass = hasCountdown && Number(item.remaining_seconds || 0) > 300 ? "is-ok" : "is-warn";
            var detailText = (item.timer_kind_label || item.title || item.timer_kind || "计时器") + " · 目标时间 " + targetText + " · 未回复 " + text(item.unanswered_count, "0") + "/" + text(item.max_unanswered_times, "0");
            var strategyText = scheduleStrategyLabel(item);
            var reasonText = scheduleReasonLabel(item);
            if (strategyText) detailText += " · 调度 " + strategyText;
            if (reasonText && strategyText === "语境预测") detailText += " · " + reasonText;
            if (item.inactive_reason) detailText += " · " + item.inactive_reason;
            html.push('<div class="pc-timer-card ', hasCountdown ? "" : "is-pending", '">');
            html.push('<div class="pc-timer-top"><div><div class="pc-row-title">', escapeHtml(item.session_display_name || item.session_name || item.session || item.session_id || item.id || "会话"), '</div>');
            html.push('<div class="pc-row-meta">', escapeHtml(item.session_id || item.session || item.id || ""), '</div></div>');
            html.push('<span class="pc-chip ', chipClass, '">', escapeHtml(timerStatusLabel(item)), '</span></div>');
            html.push('<div class="pc-timer-countdown">', escapeHtml(remaining), '<span>', escapeHtml(countdownSuffix), '</span></div>');
            html.push('<div class="pc-row-meta">', escapeHtml(detailText), '</div>');
            html.push('<div class="pc-progress"><div style="width:', progress, '%"></div></div>');
            html.push('<div class="pc-row-meta">进度 ', progress, '%', item.window_seconds ? ' · 窗口 ' + escapeHtml(formatDuration(item.window_seconds)) : '', '</div>');
            html.push('</div>');
        }
        html.push('</div>');
        return html.join("");
    }

    function renderTasks() {
        var jobs = state.jobs || [];
        if (!jobs.length) {
            $("view-tasks").innerHTML = '<div class="pc-card"><div class="pc-card-title">任务列表</div><div class="pc-empty">暂无调度任务或已配置会话</div></div>';
            return;
        }
        var html = ['<div class="pc-card"><div class="pc-card-header"><div><div class="pc-card-title">任务列表</div><div class="pc-card-subtitle">当前共 ', jobs.length, ' 个任务</div></div></div><div class="pc-list">'];
        for (var i = 0; i < jobs.length; i += 1) {
            var job = jobs[i] || {};
            var id = job.id || job.session || "";
            var isPending = job.status === "pending_schedule";
            var statusLabel = job.status_label || (isPending ? "待调度" : "已调度");
            var nextTime = job.next_run_time || job.next_trigger_time;
            var detailText = "下次运行: " + formatDate(timestampMs(nextTime) || nextTime) + " · 来源: " + text(job.source_mode, "--") + " · 未回复: " + text(job.unanswered_count, "0") + "/" + text(job.max_unanswered_times, "0");
            var strategyText = scheduleStrategyLabel(job);
            var reasonText = scheduleReasonLabel(job);
            if (strategyText) detailText += " · 调度: " + strategyText;
            if (reasonText && strategyText === "语境预测") detailText += " · " + reasonText;
            if (job.inactive_reason) detailText += " · " + job.inactive_reason;
            html.push('<div class="pc-row">');
            html.push('<div class="pc-row-title">', escapeHtml(job.session_display_name || job.session_name || id || "任务"), ' <span class="pc-inline-chip ', isPending ? "is-warn" : "is-ok", '">', escapeHtml(statusLabel), '</span></div>');
            html.push('<div class="pc-row-meta">UMO: ', escapeHtml(id), '</div>');
            html.push('<div class="pc-row-meta">', escapeHtml(detailText), '</div>');
            html.push('<div class="pc-row-actions">');
            html.push('<button class="pc-button" data-action="trigger-job" data-id="', escapeHtml(id), '">立即触发</button>');
            html.push('<button class="pc-button secondary" data-action="reschedule-job" data-id="', escapeHtml(id), '">重新调度</button>');
            if (!isPending) html.push('<button class="pc-button ghost" data-action="cancel-job" data-id="', escapeHtml(id), '">取消任务</button>');
            html.push('</div></div>');
        }
        html.push('</div></div>');
        $("view-tasks").innerHTML = html.join("");
    }

    function detectSessionType(sessionId) {
        var raw = String(sessionId || "");
        if (raw.indexOf(":GroupMessage:") >= 0 || raw.indexOf(":GuildMessage:") >= 0) return "group";
        return "friend";
    }

    function getSessionSchemaEntries(schema, sessionType) {
        var rootKey = sessionType === "group" ? "group_settings" : "friend_settings";
        var rootItems = schema && schema[rootKey] && schema[rootKey].items || {};
        var orderedKeys = [
            "auto_trigger_settings",
            "group_idle_trigger_minutes",
            "proactive_prompt",
            "context_settings",
            "schedule_settings",
            "tts_settings",
            "segmented_reply_settings"
        ];
        var entries = [];
        entries.push(["session_name", rootItems.session_name || {
            type: "string",
            default: "",
            description: "会话备注名",
            hint: "用于日志和管理端展示。为空时将回退显示 UMO。"
        }]);
        for (var i = 0; i < orderedKeys.length; i += 1) {
            if (rootItems[orderedKeys[i]]) entries.push([orderedKeys[i], rootItems[orderedKeys[i]]]);
        }
        return entries;
    }

    function getConfigEntries() {
        var schema = state.configSchema || {};
        if (state.configMode === "session") {
            return getSessionSchemaEntries(schema, detectSessionType(state.selectedSession));
        }
        var keys = objectKeys(schema);
        var entries = [];
        for (var i = 0; i < keys.length; i += 1) entries.push([keys[i], schema[keys[i]]]);
        return entries;
    }

    function getAllExpandablePathsFromEntries(entries, prefix) {
        var paths = [];
        for (var i = 0; i < entries.length; i += 1) {
            var key = entries[i][0];
            var schema = entries[i][1] || {};
            var path = prefix ? prefix + "." + key : key;
            if (schema.type === "object" && schema.items) {
                paths.push(path);
                var childEntries = [];
                var childKeys = objectKeys(schema.items);
                for (var j = 0; j < childKeys.length; j += 1) childEntries.push([childKeys[j], schema.items[childKeys[j]]]);
                paths = paths.concat(getAllExpandablePathsFromEntries(childEntries, path));
            }
        }
        return paths;
    }

    function schemaTitle(key, schema) {
        return stripLeadingEmoji(schema && schema.description || key);
    }

    function schemaDefault(schema) {
        if (!schema) return "";
        if (schema.default !== undefined) return schema.default;
        if (schema.type === "bool" || schema.type === "boolean") return false;
        if (schema.type === "int" || schema.type === "integer" || schema.type === "number" || schema.type === "float" || schema.type === "double") return 0;
        if (schema.type === "list" || schema.type === "array") return [];
        if (schema.type === "object") return {};
        return "";
    }

    function fieldValue(path, schema) {
        var value = getByPath(state.config, path);
        return value === undefined ? schemaDefault(schema) : value;
    }

    function conditionMatches(path, schema) {
        if (!schema || !schema.condition) return true;
        var parts = parsePath(path);
        parts.pop();
        var parent = getByPath(state.config, parts.join(".")) || {};
        var keys = objectKeys(schema.condition);
        for (var i = 0; i < keys.length; i += 1) {
            if (parent[keys[i]] !== schema.condition[keys[i]]) return false;
        }
        return true;
    }

    function controlAttrs(path, type) {
        return ' data-config-path="' + escapeHtml(path) + '" data-config-type="' + escapeHtml(type || "string") + '"';
    }

    function renderConfigField(key, schema, path, depth) {
        schema = schema || {};
        if (schema.hidden || !conditionMatches(path, schema)) return "";
        var value = fieldValue(path, schema);
        var title = schemaTitle(key, schema);
        var hint = schema.hint ? '<div class="pc-field-hint">' + escapeHtml(schema.hint) + '</div>' : "";
        var type = schema.type || "string";

        if (type === "object" && schema.items) {
            var expanded = state.expandedKeys.indexOf(path) >= 0;
            var childKeys = objectKeys(schema.items);
            var children = [];
            if (expanded) {
                for (var i = 0; i < childKeys.length; i += 1) {
                    var childKey = childKeys[i];
                    children.push(renderConfigField(childKey, schema.items[childKey], path + "." + childKey, depth + 1));
                }
            }
            return [
                '<div class="pc-config-group depth-', depth, '">',
                '<button class="pc-config-summary" data-action="toggle-config" data-path="', escapeHtml(path), '">',
                '<span class="pc-config-arrow">', expanded ? "▾" : "▸", '</span>',
                '<span><span class="pc-config-title">', escapeHtml(title), '</span>',
                schema.hint ? '<span class="pc-config-hint">' + escapeHtml(schema.hint) + '</span>' : "",
                '</span>',
                '<span class="pc-config-count">', childKeys.length, ' 项</span>',
                '</button>',
                expanded ? '<div class="pc-config-children">' + children.join("") + '</div>' : "",
                '</div>'
            ].join("");
        }

        var input = "";
        if (type === "bool" || type === "boolean") {
            input = '<label class="pc-switch"><input type="checkbox"' + controlAttrs(path, "bool") + (value ? " checked" : "") + '><span></span></label>';
        } else if (type === "int" || type === "integer" || type === "number" || type === "float" || type === "double") {
            var slider = schema.slider || {};
            var min = slider.min !== undefined ? slider.min : schema.minimum;
            var max = slider.max !== undefined ? slider.max : schema.maximum;
            var step = slider.step !== undefined ? slider.step : (type === "int" || type === "integer" ? 1 : 0.1);
            var rangeAttrs = "";
            if (min !== undefined) rangeAttrs += ' min="' + escapeHtml(min) + '"';
            if (max !== undefined) rangeAttrs += ' max="' + escapeHtml(max) + '"';
            if (step !== undefined) rangeAttrs += ' step="' + escapeHtml(step) + '"';
            input = [
                '<div class="pc-number-field">',
                min !== undefined && max !== undefined ? '<input class="pc-range" type="range"' + controlAttrs(path, type) + rangeAttrs + ' value="' + escapeHtml(value) + '">' : "",
                '<input class="pc-input" type="number"', controlAttrs(path, type), rangeAttrs, ' value="', escapeHtml(value), '">',
                '</div>'
            ].join("");
        } else if ((schema.options && Array.isArray(schema.options))) {
            var labels = Array.isArray(schema.labels) ? schema.labels : [];
            var options = [];
            for (var o = 0; o < schema.options.length; o += 1) {
                options.push('<option value="' + escapeHtml(schema.options[o]) + '"' + (schema.options[o] === value ? " selected" : "") + '>' + escapeHtml(labels[o] || schema.options[o]) + '</option>');
            }
            input = '<select class="pc-select"' + controlAttrs(path, "select") + '>' + options.join("") + '</select>';
        } else if (type === "list" || type === "array") {
            input = '<textarea class="pc-textarea compact"' + controlAttrs(path, "list") + ' spellcheck="false" placeholder="每行一项">' + escapeHtml(Array.isArray(value) ? value.join("\n") : "") + '</textarea>';
        } else if (type === "text") {
            input = '<textarea class="pc-textarea prompt"' + controlAttrs(path, "text") + ' spellcheck="false">' + escapeHtml(value) + '</textarea>';
        } else {
            input = '<input class="pc-input"' + controlAttrs(path, "string") + ' value="' + escapeHtml(value) + '">';
        }

        return [
            '<div class="pc-config-field depth-', depth, '">',
            '<div class="pc-field-copy"><div class="pc-field-title">', escapeHtml(title), '</div>', hint, '</div>',
            '<div class="pc-field-control">', input, '</div>',
            '</div>'
        ].join("");
    }

    function renderConfig() {
        var schema = state.configSchema || {};
        var entries = getConfigEntries();
        var sessionOptions = ['<option value="">选择会话...</option>'];
        for (var i = 0; i < state.sessions.length; i += 1) {
            var s = state.sessions[i] || {};
            var value = s.session || s;
            var display = s.session_display_name || s.session_name || value;
            if (s.session_name && s.session_name !== display) display += " (" + value + ")";
            sessionOptions.push('<option value="' + escapeHtml(value) + '"' + (value === state.selectedSession ? " selected" : "") + '>' + escapeHtml(display + (s.has_override ? " · 已覆写" : "")) + '</option>');
        }
        var fields = [];
        for (var e = 0; e < entries.length; e += 1) {
            fields.push(renderConfigField(entries[e][0], entries[e][1], entries[e][0], 0));
        }
        var selectedMeta = null;
        for (var m = 0; m < state.sessions.length; m += 1) {
            if ((state.sessions[m].session || state.sessions[m]) === state.selectedSession) selectedMeta = state.sessions[m];
        }
        var canSaveSession = state.configMode !== "session" || state.sessionConfigState.baseAvailable;
        var saveBusy = state.configMode === "session" ? state.busy.sessionSave : state.busy.configSave;
        var saveAction = state.configMode === "session" ? "save-session" : "save-config";
        var saveText = state.configMode === "session" ? "保存会话配置" : "保存配置";
        if (saveBusy) saveText = "保存中...";
        $("view-config").innerHTML = [
            '<div class="pc-card pc-config-card">',
            '<div class="pc-config-toolbar">',
            '<div class="pc-segmented">',
            '<button class="', state.configMode === "global" ? "is-active" : "", '" data-action="config-mode" data-mode="global">全局配置</button>',
            '<button class="', state.configMode === "session" ? "is-active" : "", '" data-action="config-mode" data-mode="session">会话差异配置</button>',
            '</div>',
            '<div class="pc-config-actions">',
            state.configMode === "session" ? '<select class="pc-select session-picker" id="session-select">' + sessionOptions.join("") + '</select>' : "",
            '<button class="pc-button secondary" data-action="load-config">刷新</button>',
            '</div>',
            '</div>',
            state.configMode === "session" && selectedMeta ? '<div class="pc-footer-note">当前会话: ' + escapeHtml(selectedMeta.session_display_name || selectedMeta.session_name || selectedMeta.session || selectedMeta) + ' ｜ 未回复次数: ' + escapeHtml(text(selectedMeta.unanswered_count, "0")) + '</div>' : "",
            state.configMode === "session" && state.config ? renderSessionEnableCard() : "",
            state.sessionConfigState.message ? '<div class="pc-warning">' + escapeHtml(state.sessionConfigState.message) + '</div>' : "",
            state.saveFeedback ? '<div class="pc-feedback ' + escapeHtml(state.saveFeedbackType) + '">' + escapeHtml(state.saveFeedback) + '</div>' : "",
            objectKeys(schema).length ? '<div class="pc-config-form">' + fields.join("") + '</div>' : '<div class="pc-empty">暂无配置 Schema</div>',
            '<div class="pc-config-footer">',
            '<div class="pc-footer-note">', entries.length, ' 个配置组 · ', state.expandedKeys.length ? "已展开 " + state.expandedKeys.length + " 项" : "当前全部收起", '</div>',
            '<div class="pc-row-actions">',
            '<button class="pc-button secondary" data-action="toggle-all-config">', state.expandedKeys.length ? "全部收起" : "全部展开", '</button>',
            '<button class="pc-button ghost" data-action="reset-config-defaults">恢复默认</button>',
            '<button class="pc-button ghost" data-action="discard-config">撤销更改</button>',
            state.configMode === "session" ? '<button class="pc-button ghost" data-action="reset-session"' + (!state.selectedSession ? " disabled" : "") + '>清空会话覆写</button>' : "",
            '<button id="btn-save" class="pc-button" data-action="', saveAction, '" type="button"', canSaveSession && !saveBusy ? "" : " disabled", '>', saveText, '</button>',
            '</div></div>',
            '</div>'
        ].join("");
    }

    function renderSessionEnableCard() {
        var enabled = Boolean(state.config && state.config.enable);
        var label = detectSessionType(state.selectedSession) === "group" ? "群聊会话启用状态" : "私聊会话启用状态";
        return [
            '<div class="pc-session-enable">',
            '<div><div class="pc-field-title">', label, '</div>',
            '<div class="pc-field-hint">这是当前会话的独立开关。关闭后，该会话会暂停主动消息，但不影响同类型全局配置与其他会话。</div></div>',
            '<div class="pc-row-actions"><span class="pc-chip ', enabled ? "is-ok" : "is-warn", '">', enabled ? "已启用" : "已暂停", '</span>',
            '<label class="pc-switch"><input type="checkbox"', controlAttrs("enable", "bool"), enabled ? " checked" : "", '><span></span></label></div>',
            '</div>'
        ].join("");
    }

    function setFeedback(type, message) {
        state.saveFeedbackType = type || "";
        state.saveFeedback = message || "";
    }

    function switchConfigMode(mode) {
        if (mode !== "global" && mode !== "session") return;
        state.configMode = mode;
        state.expandedKeys = [];
        setFeedback("", "");
        if (mode === "session" && !state.selectedSession && state.sessions.length) {
            state.selectedSession = state.sessions[0].session || state.sessions[0];
        }
        loadConfig();
    }

    function toggleConfigGroup(path) {
        var index = state.expandedKeys.indexOf(path);
        if (index >= 0) {
            state.expandedKeys.splice(index, 1);
        } else {
            state.expandedKeys.push(path);
        }
        render();
    }

    function toggleAllConfigGroups() {
        if (state.expandedKeys.length) {
            state.expandedKeys = [];
        } else {
            state.expandedKeys = getAllExpandablePathsFromEntries(getConfigEntries(), "");
        }
        render();
    }

    function generateDefaults(entries) {
        var result = {};
        for (var i = 0; i < entries.length; i += 1) {
            var key = entries[i][0];
            var schema = entries[i][1] || {};
            if (schema.type === "object" && schema.items) {
                var childEntries = [];
                var childKeys = objectKeys(schema.items);
                for (var j = 0; j < childKeys.length; j += 1) childEntries.push([childKeys[j], schema.items[childKeys[j]]]);
                result[key] = generateDefaults(childEntries);
            } else {
                result[key] = schemaDefault(schema);
            }
        }
        return result;
    }

    function resetConfigDefaults() {
        if (!window.confirm("确定要恢复默认值吗？\n\n这会覆盖当前编辑区内容，需要点击保存后才会写入。")) return;
        state.config = generateDefaults(getConfigEntries());
        if (state.configMode === "session" && state.config.enable === undefined) {
            state.config.enable = true;
        }
        setFeedback("warn", "已恢复为默认值，点击保存后生效。");
        render();
    }

    function updateConfigFromControl(node, shouldRender, suppressRender) {
        if (!state.config || typeof state.config !== "object") state.config = {};
        var path = node.getAttribute("data-config-path");
        var type = node.getAttribute("data-config-type") || "string";
        var value = node.value;
        if (type === "bool") {
            value = Boolean(node.checked);
            shouldRender = true;
        } else if (type === "list") {
            value = String(value || "").split(/\r?\n/);
        } else if (type === "int" || type === "integer") {
            if (value === "" || value === "-") return;
            value = parseInt(value, 10);
            if (isNaN(value)) value = 0;
        } else if (type === "number" || type === "float" || type === "double") {
            if (value === "" || value === "-") return;
            value = parseFloat(value);
            if (isNaN(value)) value = 0;
        }
        setByPath(state.config, path, value);
        setFeedback("", "");
        if (node.type === "range") {
            var pair = document.querySelector('input[type="number"][data-config-path="' + path.replace(/"/g, '\\"') + '"]');
            if (pair) pair.value = node.value;
        }
        if (!suppressRender && (shouldRender || type === "select")) render();
    }

    function syncConfigFromVisibleControls() {
        if (!state.config || typeof state.config !== "object") state.config = {};
        var root = $("view-config") || document;
        var nodes = root.querySelectorAll("[data-config-path]");
        for (var i = 0; i < nodes.length; i += 1) {
            updateConfigFromControl(nodes[i], false, true);
        }
    }

    function cleanConfig(obj) {
        if (Array.isArray(obj)) {
            var list = [];
            for (var i = 0; i < obj.length; i += 1) {
                var item = typeof obj[i] === "string" ? obj[i].trim() : obj[i];
                if (item !== "") list.push(item);
            }
            return list;
        }
        if (obj && typeof obj === "object") {
            var next = {};
            var keys = objectKeys(obj);
            for (var k = 0; k < keys.length; k += 1) next[keys[k]] = cleanConfig(obj[keys[k]]);
            return next;
        }
        return obj;
    }

    function stripSessionRuntimeKeys(config) {
        var cleaned = cleanConfig(config || {});
        if (!cleaned || typeof cleaned !== "object" || Array.isArray(cleaned)) return {};
        // 会话 effective 配置会带运行时元信息，保存和回读校验都只比较真实配置字段。
        var copy = {};
        var keys = objectKeys(cleaned);
        for (var i = 0; i < keys.length; i += 1) {
            if (keys[i].charAt(0) === "_") continue;
            copy[keys[i]] = cleaned[keys[i]];
        }
        return copy;
    }

    function loadDashboard() {
        if (!state.bridgeReady) {
            setError("AstrBot Pages bridge 未注入，当前只能显示静态页面。");
            return;
        }
        apiGet(route("dashboard")).then(function (data) {
            applyDashboardPayload(data);
            setError("");
            render();
        }).catch(function () {
            Promise.all([apiGet(route("status")), apiGet(route("jobs")), apiGet(route("session-config/sessions"))]).then(function (parts) {
                state.status = parts[0] || {};
                state.jobs = asArray(parts[1].jobs || parts[1]);
                state.sessions = asArray(parts[2].sessions || parts[2]);
                setError("");
                render();
            }).catch(function (err) {
                setError(err.message || "加载状态失败");
            });
        });
    }

    function applyDashboardPayload(data) {
        data = data || {};
        state.status = data.status || state.status || {};
        state.jobs = asArray(data.jobs);
        state.sessions = asArray(data.sessions);
        state.lastRealtimeAt = Date.now();
    }

    function loadRealtimeSnapshot() {
        if (!state.bridgeReady || !state.bridge) return;
        if (state.loadingSnapshot) return;
        if (document.visibilityState === "hidden") return;
        if (state.busy.configSave || state.busy.sessionSave) return;
        if (state.view === "config") return;
        state.loadingSnapshot = true;
        apiGet(route("dashboard")).then(function (data) {
            applyDashboardPayload(data);
            if (state.view === "status" || state.view === "tasks") render();
        }).catch(function () {
            return Promise.all([apiGet(route("status")), apiGet(route("jobs")), apiGet(route("session-config/sessions"))]).then(function (parts) {
                state.status = parts[0] || state.status || {};
                state.jobs = asArray(parts[1].jobs || parts[1]);
                state.sessions = asArray(parts[2].sessions || parts[2]);
                state.lastRealtimeAt = Date.now();
                if (state.view === "status" || state.view === "tasks") render();
            }).catch(function () {});
        }).finally(function () {
            state.loadingSnapshot = false;
        });
    }

    function initRealtimeSync() {
        if (state.realtimeTimer) return;
        state.realtimeTimer = setInterval(loadRealtimeSnapshot, 1000);
    }

    function loadJobs() {
        apiGet(route("jobs")).then(function (data) {
            state.jobs = asArray(data.jobs || data);
            setError("");
            render();
        }).catch(function (err) {
            setError(err.message || "加载任务失败");
        });
    }

    function triggerJob(id) {
        if (!id) return;
        apiPost(route("jobs/" + encodeURIComponent(id) + "/trigger"), {}).then(loadJobs).catch(function (err) { setError(err.message); });
    }

    function rescheduleJob(id) {
        if (!id) return;
        apiPost(route("jobs/" + encodeURIComponent(id) + "/reschedule"), {}).then(loadJobs).catch(function (err) { setError(err.message); });
    }

    function cancelJob(id) {
        if (!id) return;
        apiPost(route("jobs-cancel/" + encodeURIComponent(id)), {}).then(loadJobs).catch(function (err) { setError(err.message); });
    }

    function loadConfig() {
        Promise.all([
            apiGet(route("get_config")),
            apiGet(route("config-schema")),
            apiGet(route("session-config/sessions"))
        ]).then(function (parts) {
            state.configSchema = parts[1] && (parts[1].schema || parts[1]) || {};
            state.sessions = asArray(parts[2].sessions || parts[2]);
            if (state.configMode === "session") {
                if (!state.selectedSession && state.sessions.length) {
                    state.selectedSession = state.sessions[0].session || state.sessions[0];
                }
                if (state.selectedSession) {
                    loadSessionDetail(state.selectedSession);
                    return;
                }
                state.config = null;
                state.sessionConfigState = { baseAvailable: false, message: "请先选择一个会话。" };
            } else {
                state.config = parts[0] || {};
                state.sessionConfigState = { baseAvailable: true, message: "" };
            }
            if (!state.expandedKeys.length) {
                state.expandedKeys = getAllExpandablePathsFromEntries(getConfigEntries(), "");
            }
            setError("");
            render();
        }).catch(function (err) {
            setError(err.message || "加载配置失败");
        });
    }

    function saveConfig() {
        try {
            syncConfigFromVisibleControls();
            var cleaned = cleanConfig(state.config || {});
            state.config = cleaned;
            var payload = {
                friend_settings: cleaned.friend_settings,
                group_settings: cleaned.group_settings,
                web_admin: cleaned.web_admin,
                notification_settings: cleaned.notification_settings
            };
            setBusy("configSave", true);
            // 保存后立即回读，避免只更新前端状态而后端实际没有持久化。
            directBridgePost("save_config", payload).then(function (data) {
                if (!data || data.received !== true) {
                    throw new Error("保存请求没有命中新后端 save_config 接口，请重载插件后再试");
                }
                return apiGet(route("get_config")).then(function (serverConfig) {
                    var mismatched = [];
                    var keys = ["friend_settings", "group_settings", "web_admin", "notification_settings"];
                    for (var i = 0; i < keys.length; i += 1) {
                        if (!sameConfigValue(serverConfig && serverConfig[keys[i]], payload[keys[i]])) {
                            mismatched.push(keys[i]);
                        }
                    }
                    if (mismatched.length) {
                        throw new Error("保存请求已到后端，但配置回读不一致: " + mismatched.join(", "));
                    }
                    state.config = serverConfig || cleaned;
                    setFeedback("success", "全局配置已保存，后端回读校验通过。");
                    setError("");
                    render();
                });
            }).catch(function (err) {
                setFeedback("error", err.message || "配置保存失败");
                setError(err.message);
            }).finally(function () {
                setBusy("configSave", false);
            });
        } catch (e) {
            setError("JSON 格式错误: " + e.message);
        }
    }

    function loadSessionDetail(session) {
        if (!session) {
            state.sessionDetail = null;
            render();
            return;
        }
        apiGet(route("session-config/" + encodeURIComponent(session))).then(function (data) {
            state.sessionDetail = data || {};
            state.config = state.sessionDetail.effective || state.sessionDetail.override || {};
            state.sessionConfigState = state.sessionDetail.base ? { baseAvailable: true, message: "" } : {
                baseAvailable: false,
                message: "该会话尚未命中对应类型的全局 session_list，暂时无法保存会话差异配置。请先在对应全局配置中加入该会话。"
            };
            if (!state.expandedKeys.length) {
                state.expandedKeys = getAllExpandablePathsFromEntries(getConfigEntries(), "");
            }
            setError("");
            render();
        }).catch(function (err) {
            setError(err.message || "加载会话配置失败");
        });
    }

    function saveSessionConfig() {
        if (!state.selectedSession) {
            setError("请先选择会话");
            return;
        }
        try {
            syncConfigFromVisibleControls();
            var effective = stripSessionRuntimeKeys(state.config || {});
            var payload = { mode: "effective", effective: effective };
            setBusy("sessionSave", true);
            apiPost(route("session-config-save/" + encodeURIComponent(state.selectedSession)), payload).then(function (data) {
                return apiGet(route("session-config/" + encodeURIComponent(state.selectedSession))).then(function (serverDetail) {
                    var serverEffective = stripSessionRuntimeKeys(serverDetail && serverDetail.effective);
                    if (!sameConfigValue(serverEffective, payload.effective)) {
                        throw new Error("会话配置保存请求已到后端，但配置回读不一致");
                    }
                    state.sessionDetail = serverDetail || data || {};
                    state.config = state.sessionDetail.effective || payload.effective;
                    setFeedback("success", "会话差异配置已保存，后端回读校验通过。");
                    setError("");
                    render();
                });
            }).catch(function (err) {
                setFeedback("error", err.message || "会话配置保存失败");
                setError(err.message);
            }).finally(function () {
                setBusy("sessionSave", false);
            });
        } catch (e) {
            setError("JSON 格式错误: " + e.message);
        }
    }

    function resetSessionConfig() {
        if (!state.selectedSession) {
            setError("请先选择会话");
            return;
        }
        if (!window.confirm("确定要清空该会话的差异配置吗？\n\n清空后将完全继承全局默认配置。")) return;
        apiPost(route("session-config-delete/" + encodeURIComponent(state.selectedSession)), {}).then(function (data) {
            state.sessionDetail = data || {};
            state.config = state.sessionDetail.effective || {};
            setFeedback("success", "会话差异配置已清空。");
            loadConfig();
        }).catch(function (err) { setError(err.message); });
    }

    function initClock() {
        setInterval(function () {
            var node = $("pc-clock");
            if (node) node.textContent = formatDate(new Date());
            if (state.view === "status" && $("view-status")) renderStatus();
        }, 1000);
    }

    function init() {
        initTheme();
        renderShell();
        hideBoot();
        initClock();
        waitBridge(5000).then(function (bridge) {
            state.bridge = bridge;
            state.bridgeReady = !!bridge;
            if (!bridge) {
                setError("AstrBot Pages bridge 未注入，当前页面无法读取插件数据。");
                return;
            }
            Promise.resolve(typeof bridge.ready === "function" ? bridge.ready() : null).then(function () {
                state.bridgeReady = true;
                loadDashboard();
                initRealtimeSync();
            }).catch(function (err) {
                state.bridgeReady = false;
                setError(err.message || "AstrBot Pages bridge 初始化失败");
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
