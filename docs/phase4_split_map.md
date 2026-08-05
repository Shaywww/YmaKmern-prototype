# Phase 4 —— 生产 main.py 拆分映射（P0）

目标形态（文档 2.4.3 / 2.5.11 Phase 4）：main.py 只保留薄 Adapter
（事件转换、命令注册、平台结果适配），感知/决策/记忆/工具/渲染进入
可独立测试的 core 包。本文件给出生产 `/root/data/plugins/dududa20/main.py`
现有职责到原型模块的映射，服务器恢复访问后按此表机械拆分。

## 映射表

| 生产 main.py 职责 | 原型目标模块 | 说明 |
| --- | --- | --- |
| `_make_scope` / 事件身份 | `packages/adapters/astrbot/input_adapter.py` + `core/envelope.py` | `to_preprocessed()` 产出 `PreprocessedEnvelope`；`Actor(role/deny_flags)` 承载权限 |
| `_store_memory` / `_read_memory` | `core/memory.py` | `WriteGate` 评审 `MemoryCandidate` 后 `MemoryRepository.write`；跨类型召回改用 `ScopeSelector` |
| `_is_self_message` / `_get_bot_id` | `input_adapter.py`（`Actor.bot_id`）+ `core/envelope.py` | 每事件动态 bot_id，多机器人隔离 |
| `_social_decision` | `core/decision.py` `SocialDecisionEngine` | 六动作 + `DecisionReason` 稳定 reason code |
| `_perceive` | `core/perception.py` | `PerceptionResult` 注入系统提示词 |
| `_call_llm` / `_call_vision` | `router/router.py` + `router/openai_provider.py` | `ModelRouter` 八角色路由；Provider 为基础设施适配器 |
| `_render_response` / `_persona_to_oc` | `core/renderer.py` + `core/persona/` | `DraftResponse -> OCRenderer -> FinalResponse` |
| `_handle_text` / `_handle_media` / `_handle_image` | `runtime/orchestrator.py` | `RuntimeOrchestrator.run(preprocessed)` 统一编排 |
| 命令（persona/off/on/forget/health） | `deploy/astrbot/plugin_prod.py` `@command` | 命令注册留在薄层 |
| Capability 注册 | `core/capability.py` + `mcp/registry.py` | `CapabilityRegistry` + `register_all_mcp_services` |
| 输出与投递 | `core/delivery.py` + `adapters/astrbot/output_adapter.py` | `DeliveryManager.deliver -> DeliveryReceipt` 两段式完成 |

## 拆分步骤（服务器恢复后执行）

1. `cp main.py main.py.bak_p4`，把原型包同步到服务器 `PYTHONPATH`（或 pip 安装 `dududa-agent`）。
2. `main.py` 改为继承 `StarPlugin` 的薄壳：`on_message` 只做
   `to_preprocessed -> orchestrator.run -> plain_result`（参考
   `deploy/astrbot/plugin_prod.py`，约 60 行）。
3. 命令保留在薄壳 `@command`，内部调用 core 服务。
4. 记忆读取改造：`_read_memory(include_episodic=True)` 改为
   `repo.query_selector(ScopeSelector(platform, bot_id, conversation_id, actor_id, memory_type=None))`
   —— 注意：Repository 已改为**精确 Scope** 语义，跨类型召回必须显式
   用 Selector，不能再依赖旧的宽松匹配（宽松匹配是跨 Bot 泄露隐患，
   见 `test_memory_isolation_p0.py::TestIsolationMatrix::test_bot_isolation`）。
5. 逐命令回归：/dududa /persona /off/on /forget /health + 私聊/群聊/图片/文件。

## 回归清单（对应文档 2.5.10 盲区）

- [ ] 插件导入、注册、优先级、yield/send、stop_event
- [ ] 命令结果、权限、确认、审计、脱敏
- [ ] Memory 跨群/用户/私聊/Bot/Persona 隔离
- [ ] 图片/文件处理链（QQ 官方 Bot 本地路径）
- [ ] 模型 fallback（MHCoding 直连降级）
- [ ] 分片、@、引用、合并转发
