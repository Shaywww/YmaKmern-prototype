# ADR-0001：Control Plane 正式立项

- 状态：已采纳（Accepted）
- 日期：2026-08-07
- 决策者：项目负责人（Codex 会话按文档 2.5.10「规划中（附件提出）」条款决议）
- 关联：文档 2.5.10（Tracing、Eval、WebUI）、2.5.11（Phase 10 兼容清理第 9 节）、docs/ops_runbook.md

## 1. 背景

文档 2.5.10 将独立 WebUI / Control Plane 列为「规划中（附件提出，尚未纳入 Phase 1 正式合同）」，
并明确：**若后续通过 ADR 采纳 Control Plane，其权限与 Policy 门禁必须另行进入正式计划；
当前不能把它算作 Phase 8–10 的既定退出条件。**

本仓库已存在一个最小控制台原型（`packages/dududa-agent/src/dududa/control_plane/app.py`，FastAPI，约 330 行），
提供 `/health`、`/personas`（CRUD + overrides + activate）、`/mcp/services`（health/query）、
`/traces`、`/runtime/state` 与 HTML Dashboard，并有 20+ 测试（`tests/test_control_plane.py`）。
它目前**没有鉴权、权限、审计、脱敏与 Scope 过滤**，且 `run_server` 未接生产 systemd/ops 链路。

本 ADR 决定：将 Control Plane 作为独立正式合同（Phase CP）采纳，并补齐安全与治理基线。

## 2. 决策

1. **独立合同（Phase CP）**：Control Plane 正式立项，独立于 Phase 0–10 既有退出条件；
   其完成度由本文第 6 节门禁单独衡量。
2. **不进同步消息主链**：CP 是独立管理面（独立 FastAPI 进程/单元），不参与 QQ 消息
   Connector→Runtime→Delivery 同步链路，不修改消息时延与幂等语义。
3. **强制复用既有治理组件，禁止旁路**：
   - 权限：所有写操作与敏感读操作必须经 `PermissionEngine`（owner/admin/trusted 动作级授权）；
   - Scope：任何按用户/群/会话维度的读取必须构造 `MemoryScope`/具名 ScopeSelector 过滤；
   - 脱敏：所有出参（含 Trace metadata、日志、Eval 报告）统一经 `Redactor` 处理；
   - 审计：写操作与敏感读操作写入 JSONL 审计（与生产 `audit.jsonl` 同一通道）；
   - Repository：Memory/Profile/Style/GroupPolicy 读写必须经对应 Repository，不得直接碰文件。
4. **网络与鉴权**：默认绑定 `127.0.0.1`；对外暴露必须落在 `dududa-fw.sh` 受信来源白名单内；
   请求级鉴权使用管理 token（env `DUDUDA_CP_TOKEN`，缺省拒绝），高安全场景可升级 mTLS。
5. **MCP 调用不直连服务**：`/mcp/services/{id}/query` 等入口必须经 `CapabilityRegistry`
   的权限/风险/access 策略过滤（与 Agent Runtime 同一路径），禁止直接 `getattr(service, action)`。
6. **分阶段实施**：
   - CP-P0 安全基线：鉴权、权限、审计、脱敏、Scope、Repository 约束 + 负向测试；
   - CP-P1 只读面板：Trace Viewer、Memory Explorer、MCP Health、Eval 报告（全部只读）；
   - CP-P2 高级能力：Agent Playground（沙箱内）、成本/性能面板、自动故障告警、日志检索。
7. **写操作边界**：CP 可管理 Persona/配置，但不得绕过写门禁（WriteGate）写 Memory，
   不得触发有副作用工具，不得修改权限/审计自身。

## 3. 现状差距表（`packages/dududa-agent/src/dududa/control_plane/app.py` → 目标）

| 维度 | 现状 | CP-P0 目标 |
| --- | --- | --- |
| 鉴权 | 无（任意本地调用者） | `DUDUDA_CP_TOKEN` 请求级鉴权，缺省拒绝 |
| 权限 | 无（可改 persona、activate、删 persona） | PermissionEngine 动作级授权（owner 可写，其余只读） |
| 审计 | 无 | 写操作 + 敏感读操作入 audit.jsonl |
| 脱敏 | `/traces` metadata 原样返回 | Redactor 统一脱敏（含 msg 文本、token、URL query） |
| Scope | 无 | 按 viewer 构造 Scope 过滤 Memory/Trace 数据 |
| Repository | persona 走内存 registry；无 Memory 面 | Memory/Profile/Style/GroupPolicy 全部经 Repository |
| MCP query | `getattr(svc, action)` 直连，绕过权限/风险/熔断 | 经 CapabilityRegistry + access 策略 + 熔断 |
| 部署 | `run_server` 未接生产 | systemd 单元 + ops.sh 子命令 + dududa-fw.sh 白名单 |

## 4. 非目标

- 不替代 AstrBot Dashboard（6185）与 NapCat WebUI（3001/6099），三者并存、职责分离。
- Agent Playground 不获得自动写权限；所有沙箱动作仍需确认与审计。
- CP 不进入 Phase 8–10 退出条件；不阻塞现有 P0/P1/P2 门禁。

## 5. 后果

正面：可观测性（Trace/Memory/MCP Health/Eval 集中查看）、运维效率、故障定位成本下降。
负面：新增攻击面与维护成本；token 泄露可导致配置被改。
缓解：loopback 默认 + 防火墙白名单 + token 缺省拒绝 + 写操作审计 + 负向测试门禁。

## 6. 实施清单与退出门禁（Phase CP）

CP-P0 交付物：
- `docs/adr/0001_control_plane.md`（本文档）；
- CP 安全基线实现（鉴权中间件、PermissionEngine 接线、Redactor 出参、Scope 过滤、审计通道）；
- `tests/test_control_plane_cp_p0.py`：负向鉴权（无/错 token 401）、非 owner 写操作 403、
  脱敏不变量（Trace metadata 不含明文 msg/token）、Scope 过滤、审计完整性（写操作必有审计行）、
  MCP query 绕过权限被拒。

CP 退出门禁：
1. CP-P0 全部负向测试通过；`exit_gate_check.sh` 新增 CP 段全 PASS；
2. `/mcp/services/{id}/query` 不再直连 service（经 CapabilityRegistry）；
3. 所有 CP 出参经 Redactor；所有写操作有审计行；
4. CP 服务仅监听受信来源；`ops.sh cp` 子命令可启停/健康检查。

## 7. 验证

```bash
cd /opt/dududa20-prototype
python3.12 -m pytest tests/test_control_plane_cp_p0.py -q   # CP-P0 门禁测试全绿
bash ops/exit_gate_check.sh 2>&1 | tail -1              # 含 CP 段全 PASS
```

## 8. 参考

- 文档 2.5.10「规划中（附件提出）」段落；
- 文档 2.5.11 Phase 10「若后续通过 ADR 采纳 Control Plane…」条款；
- docs/ops_runbook.md 第 8 节（管理面收敛与 dududa-fw.sh）。