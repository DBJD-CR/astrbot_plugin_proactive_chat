<!-- markdownlint-disable MD024 -->
<!-- markdownlint-disable MD025 -->
<!-- markdownlint-disable MD028 -->
<!-- markdownlint-disable MD033 -->
<!-- markdownlint-disable MD034 -->
<!-- markdownlint-disable MD041 -->
# ChangeLog

# 2026/08/30 v1.2.5

Hi，好久不见。我想说的是我们还没有跑路 :)

本版本中主要更新并迁移了通知与遥测的端点（大概可以解决控制台一直刷警告信息的问题）并简单修订和完善了文档。

我们预计在九月中下旬将带来一波大的新功能和改进，敬请期待！

## 🚀 What's Changed

### 🌐 Data Sources & Network (数据源与网络)

- 迁移遥测与通知中心至新域名 by @DBJD-CR & Aloys233 in #98

### 📚 Documentation (文档)

- 更新适用于 v1.2.5 的 README 文档、贡献指南和更新日志 by @DBJD-CR in #98

---

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.2.4...v1.2.5

<details>
<summary>点击查看历史更新记录 (History)</summary>

# 2026/05/29 v1.2.4

## 🚀 What's Changed

### ✨ New Features (新功能)

- 为 Web UI 的第三方脚本添加自适应 CDN 选择机制，基于浏览器的时区和语言为中国大陆用户提供更好的支持 by @Ayleovelle in #71

### 🐛 Bug Fixes (修复)

- 优化了遥测事件容易触发请求频率超限的问题，并防止在限流或网络连接问题期间丢失遥测数据 by @Ayleovelle in #71
- 优化了 WebUI 加载相关的问题并提升了加载速度 by @DBJD-CR in #72

### 📚 Documentation (文档)

- 修订更新日志 by @DBJD-CR in #72

### 🔧 Chore (杂项)

- 移除了对 `fastapi` 和 `uvicorn` 的依赖版本上限 by @Ayleovelle in #71
- ruff 格式化 by @DBJD-CR in #72
- 更新插件元数据 by @DBJD-CR in #72

---

## ❤️ New Contributors

- @Ayleovelle made their first contribution in #71

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.2.3...v1.2.4

---

# 2026/05/06 v1.2.3

## 🚀 What's Changed

### ✨ New Features (新功能)

- 新增上下文注入来源配置项，支持 `AstrBot LLM 对话历史` (原有设计)、`平台完整聊天流水` 和 `混合模式` by @Sisyphbaous-DT-Project in #59
  - `平台完整聊天流水`模式下支持可配置的主动消息提示词，并预设了新的提示词模板 by @DBJD-CR in #64
  - 支持插件的原有占位符和完整的会话差异覆写支持 by @DBJD-CR in #64
  - 新增占位符：`{{platform_history_lines}}`，代表实际注入的群聊流水正文 by @Sisyphbaous-DT-Project in #59
- 前端中新增 `重新调度` 按钮，用于重新 roll 一次进行主动消息的时间 by @DBJD-CR in #64
- 增强了前端中状态卡片和任务卡片携带的信息内容 by @DBJD-CR in #64
- 增强了前端中的动画效果 by @DBJD-CR in #64
- 自动跳过已达未回复次数上限的会话的自动主动消息触发器设置 by @DBJD-CR in #64
- 优化了部分日志的打印行为 by @DBJD-CR in #64

### 🐛 Bug Fixes (修复)

- 为 FastAPI 初始化失败添加了降级保护，避免插件加载完全失败 by @DBJD-CR in #64

### 📚 Documentation (文档)

- 更新 README 文档中的新配置项与更新日志 by @DBJD-CR in #64
- 澄清了一些表述不清的配置说明 by @DBJD-CR in #64

### 🔧 Chore (杂项)

- 调整群聊默认使用的上下文来源为 `平台完整聊天流水` by @DBJD-CR in #64
- 调整私聊默认的最大主动消息时间间隔为 600 分钟 by @DBJD-CR in #64
- 调整了一些前端中的标题文案和微小的视觉效果 by @DBJD-CR in #64
- 移除了前端卡片右下角中的一个圆形阴影装饰 by @DBJD-CR in #64

---

## ❤️ New Contributors

- @Sisyphbaous-DT-Project made their first contribution in #59

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.2.2...v1.2.3

---

# 2026/04/02 v1.2.2

## 🚀 What's Changed

### 🐛 Bug Fixes (修复)

- 修复了在 Telegram 适配器下，插件启动时可能因内部事件模块被重复扫描/注册而触发“注册指令报错”的问题 by @DBJD-CR in #52
- 修复了配置密码的情况下，Web 端缺失密码填写入口的问题 by @DBJD-CR in #52

---

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.2.1...v1.2.2

---

# 2026/04/01 v1.2.1

## 🚀 What's Changed

### ✨ New Features (新功能)

- 为任务管理页的卡片新增调度间隔与免打扰时段的信息展示 by @DBJD-CR in #50

### 🐛 Bug Fixes (修复)

- 修复并增强了遥测中对于 AstrBot 版本号的获取方式 by @DBJD-CR in #50
- 优化了部分链路中输出日志的判断逻辑，避免误导性日志 by @DBJD-CR in #50
- 尝试修复 release 中编译打包的插件，在部分环境下出现的 WebUI 白屏问题 by @DBJD-CR in #50
- 修复了一些 Issue 模板中错误的语法问题 by @DBJD-CR in #50
- 其他的一些代码稳定性改进 by @DBJD-CR in #50

### 🔧 Chore (杂项)

- 移除了 `aiofiles` 的依赖版本上限 by @DBJD-CR in #50
- 修改发版工作流为本仓库实际情况 by @DBJD-CR in #50

---

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.2.0...v1.2.1

---

# 2026/03/30 v1.2.0

经历了大半个月的高强度开发，新版本的主动消息插件终于和大家见面了！
首先非常感谢大家对本插件的喜爱，插件的 Star 数一个月就翻了将近一倍，让我非常的惊喜，也敦促着我去进一步完善这个插件。
那么现在，你可以通过插件原生的 WebUI 对主动消息插件进行高效的配置与日常使用，新版本的个性化配置请通过插件自带的 Web 端进行配置。
在正式使用新版本插件前，请仔细阅读以下说明：

> [!IMPORTANT]
>
> 由于新版本的重构需要，更新后您需要以 `UMO` 的格式，重新配置需要接收主动消息的会话列表。
>
> 完整的 UMO 可通过 `/sid` 指令快捷获取。格式: `{platform_name}:{message_type}:{session_id}`
>
> `{platform_name}` 就是你在 WebUI 中的 `机器人` 一栏中自定义的机器人名称。`{message_type}` 可选参数为 `GroupMessage` 和 `FriendMessage`
>
> 例如：如果你之前在**私聊**列表中填写的是 `123456789`，自定义的机器人名称 (平台名称) 为 `default`，请改为 `default:FriendMessage:123456789`。
> 如果你之前在**群聊**列表中填写的是 `987654321`，自定义的机器人名称 (平台名称) 为 `default`，请改为 `default:GroupMessage:987654321`。
>
> 使用 QQ 官机时，注意不要像个人号那样填写 QQ 号，应填写 UID，可使用指令 `/sid` 获取，格式类似 `4C011A2B3D4C5E6F9F8E7D6C5B4A3210`。
>
> 使用个人微信需要升级到最新的手机微信版本：`iOS >= 8.0.70`，`Android >= 8.0.69`，并确保微信中包含 `ClawBot` 插件
>
> 如果更新到新版本发现自己的某些配置项或自定义提示词被重置，可以参考插件数据文件夹下的配置快照与提示词备份来辅助恢复。
>
> 如遇到无法正常使用的问题，欢迎通过 Issue 等方式向开发者反馈。

以下是较为详细的主要更新内容：

## 🚀 What's Changed

### ✨ New Features (新功能)

- 完整的 WebUI 支持，提供 运行状态 / 任务管理 / 通知中心 / 文档浏览  /配置管理 五大页面视图 by @DBJD-CR & @Aloys233 in #34 #36 #37 #39 #43 #46 #47
  - 其中 通知中心 与 文档浏览 支持完整的原生 MarkDown 浏览体验
  - 更多内容等待您去探索！
- 新增通知系统相关的配置项 by @DBJD-CR in #39 #40 #47
- 新增遥测系统相关的配置项 by @DBJD-CR in #46
- 新增 `内容过滤正则表达式` 配置项，默认关闭。仅建议 AstrBot v4.20.1+ 开启 by @DBJD-CR in #46

### ♻️ Refactor (重构)

- 将原来的 `main.py` 全家桶架构彻底拆分为多文件的模块化架构并保持原有核心功能不变 by @DBJD-CR in #34
- 将原来又臭又长的配置文件结构大幅简化 (并且现在在 AstrBot WebUI 中进行浏览时再也不会卡顿了)，个性化配置功能解耦至插件独立 Web by @DBJD-CR in #34 #36
- **破坏性变更**：暂时移除了配置与提示词备份相关功能，以及需要重新填写的 UMO 格式 by @DBJD-CR in #34

### 🐛 Bug Fixes (修复)

- 或许没有？

### 📚 Documentation (文档)

- 全面重写和增强了 README 文档，并同步更新多语言文档 (AI 翻译) by @DBJD-CR & Alaye-Dong in #47
- 更新适用于 v1.2.0 版本的贡献指南、更新日志 by @DBJD-CR in #39 #47
- 新增了一些 Issue 模板以便更好的分类 Issue 类型 by @DBJD-CR in #39
- 澄清了一些表述不清的配置说明 by @DBJD-CR in #34

### 🔧 Chore (杂项)

- 为屎山代码工作流添加了 SHA256 校验 by @DBJD-CR in #47
- SVG 与插件元数据更新 by @DBJD-CR in #47

---

## ❤️ New Contributors

- @dependabot[bot] made their first contribution in #34
- @openai-codex[bot] made their first contribution in #34
- @roomote made their first contribution in #36
- @kilo-code-bot[bot] made their first contribution in #36
- @gemini-code-assist[bot] made their first contribution in #36
- @claude made their first contribution in #37
- @Aloys233 made their first contribution in #40
- @codex made their first contribution in #43
- @Alaye-Dong made their first contribution in #47

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.1.5...v1.2.0

---

# 2026/02/17 v1.1.5

感谢您使用本插件，首先在这祝大家新年快乐！
非常感谢大家对本插件的喜爱，最近也是成功突破了 80 Star，可喜可贺喵！
目前重构计划已经提上了日程，等我另一个插件的 WebUI 和配置管理也写成熟了就可以回来升级和重构主动消息插件了。
另外官方最近在 AstrBot v4.14.0+ 推出了“未来任务”功能，因此本插件原定在 v1.2.0 版本中更新的内容就被重构计划替代了喵，下次更新版号或许也可能直接跳到 2.0.0 了（）

## What's Changed

### 新增 & 优化 (Feat & Opt)

- **配置自动备份**: 每次插件重载时，会自动将当前生效的完整用户配置备份为 `user_config_snapshot.json`，防止配置丢失
- **Prompt 汇总导出**: 新增 Prompt 提取功能，自动将所有私聊、群聊的全局及个性化 Prompt 汇总导出为 `prompts_collection.md`，极大提升了 Prompt 的可读性和管理便捷性
- **数据持久化增强**: 所有备份数据均存储在插件专属数据目录下，为后续重构做好数据安全准备
- 新增了一些基础的 GitHub 工作流

### 修复

- 修复了一个错误的导入名

### 文档 & 杂项

- 修订 README 文档与更新日志
- Issue/PR 模板微调

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.1.4...v1.1.5

# 2026/01/22 v1.1.4

## What's Changed

### 新增 & 修复 (Feat & Fix)

- 为配置文件新增了滑块组件（仅 AstrBot v4.9.2+ 支持）by @DBJD-CR
- 优化了插件在多实例情况下的行为表现 by @DBJD-CR

---

> [!TIP]
> 如果遇到你期望之外的 Bot 给你发送主动消息，并且插件无法自行更正，请尝试删除插件生成的持久化数据（不影响你在插件中的配置项），然后和你想要发送主动消息的那个 Bot 重新聊一下。

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.1.3...v1.1.4

---

# 2026/01/11 v1.1.3

## What's Changed

### 新增 & 修复 (Feat & Fix)

- 在 `main.py` 新增了相关逻辑，增强对 ApiNotAvailable 报错的处理 Fix #19 by @DBJD-CR
- 在 `main.py` 新增了相关逻辑，防止 LLM “发对象” by @DBJD-CR

### 文档与杂项 (Docs & Chore)

- 调整了部分日志的打印级别
- 更新文档 by @dosu-ai @sourcery-ai

---

## New Contributors

- @dosu-ai made their first contribution in #19

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.1.2...v1.1.3

---

# 2026/01/07 v1.1.2

## What's Changed

### 新增 & 修复 (Feat & Fix)

- 在 `main.py` 新增了相关逻辑，让插件伪造一个 AstrBot 消息事件，使生成的主动消息经过 `on_decorating_result` 钩子，让其他插件能对主动消息的内容进行修改，提升插件兼容性 Fix #15

### 文档与杂项 (Docs & Chore)

- 新增 DeepWiki 与 Zread 第三方文档
- 新增英文与日文 README.md 文档
- 更新贡献指南
- 更新 PR 模板

---

## New Contributors

- @NickWoluff made their first contribution in #16
- @sourcery-ai made their first contribution in #17

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.1.0...v1.1.2

---

# 2025/12/23 v1.1.0

## What's Changed

### 新增

- 在 `main.py` 和 `_conf_schema.json` 中完整实现了分段回复功能（支持正则/词表分段、随机/对数间隔、配置热重载）

### 修复

- 解决了配置优先级覆盖问题，确保个性化配置可以正确禁用会话是否启用

---

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.0.2...v1.1.0

---

# 2025/12/21 v1.0.2

# What's Changed

## 新增

- 在 `ProactiveChatPlugin` 类中新增了辅助方法 `_sanitize_history_content`，用于将结构化的消息列表转换为纯文本字符串

## 修复

- 修复 `_send_proactive_message` 中因非标准 Session ID 导致的 TTS Provider 获取失败问题
- 修改了核心调度函数 `check_and_chat` 。现在，它会先尝试使用原始数据调用 LLM。只有当捕获到包含 "validation error" 和 "valid string" 的特定异常时，才会触发清洗逻辑，使用转换为纯文本的历史记录进行重试

---

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.0.1...v1.0.2

---

# 2025/12/18 v1.0.1

> [!WARNING]
> 以下内容由 AI 生成，我只做了简单润色，请仔细甄别

## 🚀 Release v1.0.1

### 🐛 Bug 修复

- **修复 Satori 等平台兼容性问题**: 解决了部分平台（如 Satori）生成的非标准会话 ID (例如 4 段式 ID) 导致 AstrBot 核心解析错误 (`ValueError`) 的问题。插件现在能智能识别并处理这些特殊格式，确保跨平台稳定性
- **修复新会话初始化失败**: 解决了首次使用插件或新用户接入时，因缺少对话历史而无法正确创建主动消息任务的逻辑死锁问题。现在插件会自动为新会话初始化对话 ID

### ⚡ 优化

- **增强异常处理**: 优化了内部任务调度逻辑的异常捕获机制，提升了插件在边缘情况下的健壮性

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v1.0.0...v1.0.1

---

# 2025/12/18 v1.0.0

# v1.0.0 正式版

## What's Changed

### **新增**

- 自动主动消息功能，仅在插件加载完成后生效一次，用于在没有用户输入的情况下创建主动消息任务，收到任何消息后将取消
- 完整的多会话支持，支持最多5个私聊+5个群聊的完全个性化配置。更多会话将应用全局配置
- 全新的，`又臭又长` 的 WebUI 界面
- 全面优化的日志打印🐾与代码注释清理
- 内存泄漏防护：新增定期清理机制，自动清理超过5分钟的过期会话状态，防止状态数据永久残留
- 全面适配 AstrBot v4.5.2+ 的各类新 API

### **修复**

- 添加补丁，尝试解析部分平台非标准 `session_id` 导致的解包错误
- 优化了判断会话类型的方法
- 并发竞态条件修复：新增状态一致性检查，避免在极少数状况下，LLM生成期间用户发送消息导致的"插嘴"问题

---

目前就这么多了 - 感谢所有报告问题并帮助我们不断改进插件的用户！

保持警觉，不放过未来任何新版本更新的相关资讯！

**Full Changelog**: https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat/compare/v0.9.97...v1.0.0

---

# 2025/12/06 v1.0.0-beta.7

> [!WARNING]
> 以下内容由 AI 生成，我只做了简单润色，请仔细甄别

# 🚀 v1.0.0-beta.7 多会话架构版

## 🎯 核心突破

从单一会话限制升级为完整多会话支持，支持同时为多个私聊和群聊提供主动消息服务，每个会话完全独立管理

## 🔧 主要更新

### ✅ 多会话架构

- **个性化配置槽位**：新增5个私聊 + 5个群聊独立配置槽位，支持完全个性化设置
- **全局配置系统**：通过`session_list`管理更多会话，使用全局配置作为后备方案
- **会话完全隔离**：每个会话拥有独立状态、计数器、触发器，避免相互干扰

### 🐛 Bug修复

- **会话ID匹配**：改进原有逻辑以适配多会话架构
- **自动触发器重载失效**：修复插件重载后自动触发计时器未被正确清理的历史遗留bug
- **日志重复问题**：合并部分重复日志，优化日志职责

### 🏗️ 架构重构

- **统一配置入口**：`_get_session_config()`智能支持多层级配置优先级
- **多触发器协调**：自动触发系统与消息监听系统的完美配合
- **状态隔离管理**：独立的`last_message_times`、`auto_trigger_timers`等状态管理

## 📊 配置变更

- 新增`private_sessions`和`group_sessions`个性化配置块
- 新增`session_list`全局会话列表配置
- 配置优先级：个性化配置 > 全局配置 > 默认配置

## ⚡ 性能优化

- 内存管理：定期清理过期会话状态，防止内存泄漏
- 并发安全：完善异步锁机制，确保数据操作原子性
- 日志优化：使用会话备注显示，提高可读性

---

# 2025/11/30 v1.0.0-beta.6

> [!WARNING]
> 以下内容由 AI 生成，我只做了简单润色，请仔细甄别。

# 🚀 v1.0.0-beta.6 代码质量优化版

## 🎯 核心改进

基于AI审查建议进行了部分代码质量优化，修复多个潜在缺陷和逻辑问题

## 🔧 修复内容

### ✅ 逻辑正确性

- **会话类型判断优化**：从模糊的 `"group" in umo.lower()` 改为精确解析 `platform:type:id` 格式，避免误判风险
- **函数副作用消除**：`_is_chat_allowed` 改为纯查询函数，移除隐式重新调度逻辑，符合"命令查询分离"原则

### 🛡️ 潜在缺陷修复

- **内存泄漏防护**：新增定期清理机制，自动清理超过5分钟的过期会话状态，防止状态数据永久残留
- **数据覆盖保护**：从直接赋值覆盖改为安全字段更新，保留其他现有数据，为未来功能扩展预留空间

### 🏗️ 代码结构优化

- **重复代码消除**：提取 `_setup_auto_trigger_for_session_type` 公共函数，消除私聊/群聊逻辑的重复代码
- **并发竞态条件修复**：新增状态一致性检查，避免在极少数状况下，LLM生成期间用户发送消息导致的"插嘴"问题

---

# 2025/11/25 v1.0.0-beta.4

> [!WARNING]
> 以下内容由 AI 生成，我只做了简单润色，请仔细甄别。

---

## 🆕 v1.0.0-beta.4 更新日志

**发布日期**: 2025年11月25日

### 🔧 修复内容

- **修复竞态条件问题**: 解决了多群聊并发场景下Bot消息检测的竞态条件问题。将会话相关的全局状态改为以`session_id`为键的字典隔离存储，确保每个群聊会话的状态独立，避免状态互相干扰
- **优化日志过滤**: 改进了日志记录逻辑，现在只针对配置中启用的会话记录相关日志，避免非目标群聊产生多余日志信息

### 🎯 影响

- ✅ 多群聊并发场景下Bot消息检测更加准确可靠
- ✅ 减少了非目标会话的日志噪音，提升调试体验
- ✅ 插件在高并发环境下的稳定性显著提升

这是一个简单的的稳定性修复版本，你可以自主选择是否更新。

---

# 2025/11/20 v1.0.0-beta.3

> [!WARNING]
> 以下内容由 AI 生成，我只做了简单润色，请仔细甄别。

## 🆕 v1.0.0-beta.3 更新日志

**发布日期**: 2025年11月20日

### 🔧 修复内容

修复了新会话无法创建主动消息的问题。当用户第一次使用插件时，系统现在会自动为新会话创建初始数据，而不是错误地跳过处理（#10）

**问题**:

```log
[主动消息] 群聊 xxx 的会话数据不存在，跳过主动消息创建喵。
```

**修复**: 修改了 `main.py` L1145-L1149 的会话数据检查逻辑，新会话会自动初始化数据

### 🎯 影响

- ✅ 新用户首次使用插件现在可以正常工作
- ✅ 现有用户数据不受影响
- ✅ 解决了"无法发送主动消息"的反馈问题

这是一个热修复版本，建议所有用户升级。

---

# 2025/11/19 v1.0.0-beta.2

# v1.0.0-beta.2 更新日志

## ✨ 新增功能

### 🤖 自动主动消息

- 插件启动后可检测无消息会话，自动触发主动消息任务
- 支持私聊和群聊场景，可配置触发等待时间
- 智能避免与现有任务冲突，收到消息自动取消触发

## 🔧 优化改进

### LLM调用优化

- 采用 `context.llm_generate()` 新API作为主要调用方式
- 新API失败时自动回退到传统API，保持兼容性
- 改进错误处理，提供更详细的调试信息

### 人格管理优化

- 使用 `context.persona_manager` 新API简化人格获取逻辑
- 优先使用会话绑定人格，其次使用默认人格
- 代码结构更清晰，遵循最新开发文档规范

### 其他改进

- 增强配置验证，初始化时检查设置合理性
- 优化日志输出，避免信息过载
- 改进异步锁使用，提升性能

如果在使用过程中遇到任何问题，**建议先更新 AstrBot 版本到 v4.5.7 或更新的版本。**

---

# 2025/11/17 v1.0.0-beta.1

> [!WARNING]
> 以下内容由 AI 生成，我只做了简单润色，请仔细甄别。

## **Release v1.0.0-beta.1 更新日志**

**版本描述:**
这是一个重要的架构升级版本，首次引入对**单群聊**的完整主动消息支持。我们彻底重构了配置系统和数据存储架构，为未来的多用户、多群聊功能奠定了坚实基础。

**⚠️ 重要提醒：**
由于配置结构的完全重构，从 `v0.9.x` 版本升级的用户，插件将**无法**继承旧的配置。如果您已经精心设置了个性化的提示词，请务必提前备份。

---

### **🚀 新增功能 (New Features)**

- **【核心】新增单群聊支持:**
  - 现在，除了原有的私聊功能，您还可以配置插件对一个指定的群聊发起主动消息
  - 群聊使用全新的"沉默倒计时"机制，只有当群聊连续沉默指定时间后才会触发主动消息

- **【架构】全新的多会话配置系统:**
  - 彻底重构了 `_conf_schema.json` 文件，将所有配置项清晰地划分为 `private_settings` 和 `group_settings` 两大独立区域
  - 私聊和群聊的**所有配置**（启用开关、目标ID、动机Prompt、调度时间、TTS设置等）完全隔离，可独立精细化配置
  - 为未来支持多个、可动态添加的会话配置打下坚实基础

- **【技术】AstrBot 4.5.7+ 兼容性:**
  - 新增对 `context.llm_generate()` 新API的支持，同时保持对传统 `provider.text_chat()` API的向后兼容
  - 自动检测API可用性，优先使用新API，确保在最新版本AstrBot上的最佳性能

- **【优化】日志打印:**
  - 增强了部分场景下的日志打印以便于调试
  - **现在所有的日志打印都会喵喵叫喵~**

### **🐛 Bug 修复与健壮性提升 (Bug Fixes & Robustness)**

- **修复了"日志重复打印"问题：** 解决了因重复定义和逻辑错误导致的日志多次重复打印问题
- **修复了"监听器失效"问题：** Bot消息现在能被正确监听和识别，解决了群聊消息流监听失效的问题
- **修复了"插件停用残留"问题：** 彻底重构了 `terminate` 函数，所有正在运行的定时器和计时器都会被正确清理，防止插件停用后仍有逻辑运转
- **修复了"计数器错误"问题：** 修正了未回复计数器的错误累加逻辑，确保计数准确
- **修复了"时序问题"：** 优化了Bot消息捕获的时序，确保消息检测的准确性
- **修复了"会话共享"问题：** 私聊与群聊现在使用独立的计数器和处理逻辑，互不干扰

### **📝 代码质量提升**

- **详细中文注释：** 为所有核心函数添加了详细的中文注释，便于社区开发者理解和维护
- **完善异常处理：** 增强了错误处理机制，提高插件的稳定性和健壮性
- **异步安全存储：** 使用异步文件I/O和并发锁保护数据存储，确保数据一致性

### **🎯 核心架构变化**

| 方面 | v0.9.97 | v1.0.0-beta.1 |
| ----- | --------- | --------------- |
| **支持模式** | 仅单用户私聊 | 单私聊 + 单群聊 |
| **架构设计** | 单会话模式 | 多会话架构 |
| **群聊机制** | ❌ 不支持 | ✅ 沉默倒计时 |
| **Bot检测** | ❌ 无 | ✅ 三重验证 |
| **配置管理** | 集中缓存 | 按需获取 |
| **API兼容** | 传统API | 双API支持 |

### **🎉 总结**

v1.0.0-beta.1是一次从架构到功能的全面升级，首次实现了完整的群聊支持，并为未来的多会话扩展奠定了坚实基础。虽然需要重新配置，但全新的架构将带来更稳定、更强大的用户体验！

**感谢社区的支持，让我们一起打造更好的 AstrBot 生态！** 🚀

---

# 2025/11/12 v0.9.97

> [!WARNING]
> 以下内容由 AI 生成，我只做了简单润色，请仔细甄别。

## **v0.9.97 (代码质量重构版) - 更新日志**

这是一个专注于采纳社区反馈、修复潜在 Bug、并对代码进行全面重构的“小更新”版本。本次更新没有引入任何新功能，其核心目标是偿还所有已知的“技术债”，提升插件的健壮性、可维护性和代码质量，为未来的 `v1.0.0` 正式版做好准备。

---

### **🐛 Bug 修复 (Bug Fixes)**

- 修复了因“对方正在输入”状态事件导致计数器被错误重置的问题。 现在插件会正确过滤掉没有实际内容的消息事件，防止 `unanswered_count` 被意外清零
- 修复了在所有定时任务创建时未正确使用配置时区，可能导致任务执行时间不准确的 Bug。现在所有时间转换都将严格遵循用户在 `AstrBot` 中配置的时区
- 修复了在部分情况下（如未配置目标用户或初次使用时），插件会创建一个无法使用的“None 对话”的问题。移除了不必要的兜底逻辑，避免了垃圾数据的产生。你现在可以将会话管理中的“None 对话”删除

### **🚀 性能与健壮性优化 (Performance & Robustness)**

- 为重构后的主函数设计了新的并发处理逻辑，确保所有对会话数据的读写操作都在异步锁的保护下进行，解决了潜在的数据竞争风险
- 使用更现代的异步处理方式，用 `asyncio.to_thread` 替代了 `loop.run_in_executor` 
- 在时区配置无效并回退到系统时区时，增加了一条明确的警告日志，方便用户排查潜在的配置问题

### **🎨 代码质量与规范 (Code Quality & Style)**

- 对核心函数 `check_and_chat` 进行了重构，将其拆分为多个职责单一的辅助函数，大幅提升了代码的可读性和可维护性
- 重构了配置管理，现在所有配置项都在插件初始化时一次性读取，实现了集中化管理
- 移除了已废弃的 `last_msg_time` 变量及相关逻辑的死代码
- 遵循 AstrBot 插件开发规范和 AI 审查建议，对代码进行了全面的风格优化

---

# 2025/11/06 v0.9.9

> [!IMPORTANT]
> 要使用该版本的插件，请确保你的 AstrBot 版本大于等于 **4.5.2** ，否则无法正常导入插件。#7

> [!WARNING]
> 以下内容由 AI 生成，我只做了简单润色，请仔细甄别。

## **v0.9.9 (社区优化版) - 更新日志**

这是一个专注于采纳社区反馈、提升代码质量和框架适应性的“小更新”版本。我们根据“AI 锐评”和 #5 中提出的宝贵建议，对插件的内部实现进行了多项优化和修复，使其更健壮、更专业。

### **✨ 核心优化与修复 (Core Optimizations & Fixes)**

- **【API 适配】存档逻辑升级 (feat: Adopt `add_message_pair` API)**
  - 我们已将插件的“记忆存档”逻辑，从“手动拼凑字典”的旧方式，全面升级为使用 `AstrBot v4.5.2+` 提供的官方 `conversation_manager.add_message_pair` API
  - **收益：** 这使得我们的代码在处理对话历史时，变得更简洁、更健壮，并能完美兼容未来 `AstrBot` 对消息结构的任何更新，彻底杜绝了因格式不匹配而导致存档失败的风险

- **【性能优化】JSON 解析异步化 (perf: Asynchronous JSON parsing)**
  - 采纳了“AI 锐评”的建议，我们将插件中所有同步的、CPU 密集型的 `json.loads` 和 `json.dumps` 操作，全部通过 `asyncio.run_in_executor` 放入了独立的线程中执行
  - **收益：** 这避免了在处理超长对话历史或保存会话数据时，可能导致的 `AstrBot` 核心事件循环被阻塞的问题，极大地提升了插件在极限情况下的性能和响应能力

- **【Bug 修复】修复了时区未配置时的崩溃问题 (fix: Handle `ValueError` on invalid timezone)**
  - 根据 #5 中 `@victical` 的反馈，我们在 `initialize` 函数中，为时区加载逻辑，增加了对 `ValueError` 的捕获
  - **收益：** 这彻底解决了在新安装的、未配置全局时区的 `AstrBot` 环境中，插件会因无法解析空的 `timezone` 配置而加载失败的 Bug

### **🎨 代码质量与规范性提升 (Code Quality & Style)**

- **【代码质量】路径拼接现代化：** 我们将 `os.path.join` 的用法，更新为了 `pathlib` 推荐的 `/` 操作符，使代码更简洁、更符合现代 Python 风格
- **【框架适应性】装饰器格式统一：** 我们将 `@star.register` 装饰器，从“关键字参数”的写法，修改为了社区推荐的“位置参数”格式，以更好地遵循框架的编码规范
- **【代码整洁】移除了未使用的导入：** 通过 `ruff` 工具，我们清理了代码中所有冗余和未被使用的 `import` 语句，使代码更加干净
- **【编码规范】异常类型现代化：** 将代码中所有 `IOError` 的捕获，更新为了更现代、更基础的 `OSError`

---

## New Contributors

- @victical made their first contribution in #5

---

# 2025/11/02 v0.9.8

> [!CAUTION]
> 请确保你已经在 AstrBot 的 WebUI 中，在 `配置文件` → `系统配置` 中正确配置了时区，不然会导致 `ValueError` 报错。我们会在下个版本的更新中添加补丁以修复这个问题。（#5）

> [!WARNING]
> 以下内容由 AI 生成，我只做了简单润色，请仔细甄别。

这是 `proactive_chat` 插件发布以来的第一次更新。我与 Gemini-2.5-Pro 并肩作战，攻克了数个难题，将插件的稳定性、健壮性和功能性，提升到了一个新的高度

## **✨ 核心修复与新功能 (Core Fixes & New Features)**

- **【重要修复】彻底解决了“重启失忆”问题 (Fix: Persistent Session Failure) 🔥**
  - 通过引入全新的、基于“事务性日志”思想的异步 I/O 架构，我们确保了每一次定时任务的安排，都会被**立刻、原子地**写入硬盘
  - 现在，无论您是“重启核心”还是“重载插件”，`proactive_chat` 都将能够完美地、无可辩驳地，从文件中恢复所有未执行的主动消息任务。**持久化会话，现在，真正地实现了**

- **【修复】修复了潜在的并发数据竞争问题 (Fix: Concurrent Data Race)**
  - 通过引入 `asyncio.Lock` 机制，我们为插件所有对 `session_data` 文件的读写操作，都增加了“原子锁”
  - 这从根本上杜绝了在高并发场景下（例如，用户消息和定时任务同时触发），可能导致的数据损坏或状态错乱问题

- **【重要修复】修复了“记忆黑洞”问题 (Fix: Context-aware Failure)**
  - 我们为“手动存档”逻辑，增加了最终的“容错补丁”。现在，即使在全新的、没有任何历史记录的会话中，插件也能**主动地、正确地**，为该会话创建新的对话历史，并**同时**将“模拟的用户消息”和“AI 的回复”，作为一个完整的对话对，存入历史记录
  - **现在，每一次主动消息，都将成为“永不磨灭的记忆”**

- **【新增功能】未回复次数上限 (Feature: Max Unanswered Times)**
  - 为了避免对不活跃的用户造成骚扰，我们在 `schedule_settings` 中，新增了 `max_unanswered_times` 选项（默认为 4）
  - 当插件连续发送了指定次数的主动消息，但用户仍未回复时，插件将**自动暂停**对该用户的主动聊天，直到用户下一次主动发来消息

## **🛠️ 其他修复与增强**

- **【修复】上下文感知与其他日志优化：**`使用 update_conversation` 手动存档对话记录。未来新 API 加入后可进一步优化（#2）
- **【新增】增加了插件级 TTS 开关：** 在 `tts_settings` 中，新增了 `enable_tts` 选项（默认为 `true`），允许您一键开启或关闭所有由本插件引发的 TTS 功能（#2）
- **【新增】增加了 `{{current_time}}` 时间占位符：** 您现在可以在您的 `proactive_prompt` 中，使用 `{{current_time}}` 这个占位符，它将被自动替换为格式化的、带有正确时区的当前时间（例如 `2025年11月02日 18:30`）（#2）

## **🔨 工程优化**

- **代码重构：** 全面升级了插件的异步 I/O 模型，使用 `aiofiles` 彻底替代了所有同步阻塞式的文件操作，解决了可能导致 `AstrBot` 核心事件循环被卡死的致命问题
- **注释完善：** 为 `v0.9.8` 版本的所有核心改动，增加了详尽的中文注释，详细解释了每一次修复背后的“为什么”与“怎么做”

---

## New Contributors

- @TheFurina made their first contribution in #3

---

# 2025/10/27 v0.9.7

## v0.9.7 - 最终稳定版 (Final Stable Release)

> [!WARNING]
> 以下内容由 AI 生成，我仅做了简单润色。内容仅供参考，请仔细甄别。

> [!NOTE]
> 这是 `astrbot_plugin_proactive_chat` 插件的第一个公开稳定版本。它凝聚了我们近百个版本的迭代、无数次的失败与重构，以及最终在 Prompt Engineering 上的灵光一闪。它现在不仅功能运转良好，而且拥有一个健壮、优雅、且经受住了考验的核心架构。

---

### 🚀 相比于历史版本 (v0.9.5) 的主要改进

- **【核心】通用 TTS 支持:** 彻底移除了内置的、仅针对日语的“前置过滤器”。现在，插件会“勇敢地”将所有语言的文本都尝试进行 TTS，并将最终的成功与否，完全交由用户自己配置的 TTS 服务（或其适配器）来决定。这使得插件能够完美地、无差别地支持 OpenAI TTS、Edge TTS 等多语言云服务
- **【核心】配置 UI 现代化:** 全面重构了 `_conf_schema.json` 文件，严格遵循 `AstrBot v4.5.0+` 的最新官方规范。所有配置项都被归入了逻辑清晰的分组（如“核心设置”、“动机设置”、“时间设置”等），极大地提升了用户在 WebUI 中的配置体验
- **【核心】代码与配置同步:** 修正了因配置分组而导致 `main.py` 无法正确读取参数的致命错误，确保了 WebUI 上的所有修改都能精确生效
- **【规范性】同步字段:** 修正了所有没有及时更新到 `astrbot_plugin_proactive_chat` 的字段和相关调用函数
- **【规范性】ruff:** 使用 `ruff` 对代码进行了全面格式化，提升了代码的可读性与规范性，并移除了未使用的导入
- **【健壮性】命名空间净化:** 修正了持久化数据文件名 (`.json`)、插件内部注册名 (`@star.register`) 与插件文件夹名 (`astrbot_plugin_proactive_chat`) 不统一的问题。这遵循了高质量开源插件的最佳实践，从根源上杜绝了未来与其他插件可能发生的命名冲突
- **【开发者体验】日志全面汉化:** 将插件的所有日志输出都修改为了清晰、易懂的中文，并增加了关键的“未回复次数”日志，极大地便利了社区其他开发者进行调试或二次开发

### ✨ 主要功能

- **定时触发:** 基于用户沉默时间，在设定的随机时间范围内自动触发
- **上下文感知:** 能够回顾历史对话，并根据你配置的“动机” Prompt，生成与之前话题相关的回复，而不是生硬的问候
- **完整人格支持:** 加载并应用你在 AstrBot 中为 Bot 设置的完整人格（System Prompt），确保每一次主动消息都符合人设
- **动态情绪:** 内置“未回复计数器”，你可以利用它在 Prompt 中设计不同的情绪表达（例如，第一次主动消息是自然的关心，多次未回复后可以表现出失落或困惑）
- **免打扰时段:** 可以自由设定一个时间段（如午夜），在此期间 Bot 不会主动打扰用户
- **健壮的 TTS 集成:** 支持调用你配置的任何 TTS 服务生成语音，并能优雅地处理 TTS 失败的情况
- **高度可配置:** 所有核心参数，包括最重要的“主动消息动机”，都可以在一个美观、分组化的 WebUI 界面中轻松配置

### ⚠️ 已知限制与未来展望

- **持久化会话:** 经测试，在 `AstrBot` 重启后，插件无法恢复之前的定时任务。该问题已被列入“未来开发路线”，目前正在计划解决该问题
- **分段回复:** 当前版本未适配 AstrBot 提供的分段回复功能 (#2)
- **上下文感知不完整:** 插件主动生成并发送的内容未包含在 AstrBot 的存储范围内，导致主动消息内容效果不理想(#2)
- **时间感知:** 主动消息缺少时间感知能力（#2）
- **单目标:** 目前的主动消息仅支持单个私聊对象。多用户、多群聊的支持是未来的核心开发方向
- **Prompt 依赖:** 主动消息的效果，高度依赖于用户在“动机 Prompt”中提供的创造力和引导。一个好的 Prompt 是插件灵魂的关键

---

我们相信，在社区的共同努力下，`astrbot_plugin_proactive_chat` 会变得越来越好。感谢您的使用！

</details>
