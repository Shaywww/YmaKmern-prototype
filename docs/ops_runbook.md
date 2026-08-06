# Dududa 2.0 运维手册（Ops Runbook）

对应文档章节：2.5.9（运行时限流与预算）、2.5.10（运维收口）、2.5.11（退出门禁）。
服务器路径：`/opt/dududa20-prototype`（原型仓库）、`/root/data/plugins/dududa20`（插件薄壳）。
服务名：`astrbot`（systemd）。

## 0. 常用服务命令

```bash
systemctl status astrbot                # 服务状态
systemctl restart astrbot && sleep 15   # 重启并等待加载
systemctl is-active astrbot             # active / inactive
journalctl -u astrbot --no-pager -n 200 # 最近 200 行日志
# 启动成功标志（必须出现）：
#   Dududa 2.0 | renderer=OK | memory=JSON | vision=... | security=ON
#   MCP capabilities registered: 7
```

## 1. 运维脚本 `scripts/ops.sh`

只读为主（`backup` 除外），输出目录默认 `/root/data/ops`，可用环境变量 `OPS_OUT` 覆盖（便于测试/临时快照）。

```bash
cd /opt/dududa20-prototype
bash scripts/ops.sh health              # 健康快照
bash scripts/ops.sh manifest            # 供应链 manifest v2
bash scripts/ops.sh smoke               # 完整 smoke（含关键 pytest）
bash scripts/ops.sh smoke --fast        # 快速 smoke（跳过 pytest）
bash scripts/ops.sh backup              # 先写 health 快照，再委托 /root/manage.sh backup
```

| 子命令 | 产物 | 关键字段 |
| --- | --- | --- |
| `health` | `$OPS_OUT/health_status.json` | `service.active`、`banner`、`mcp_capabilities`、`recent_errors`、`backup.age_seconds`、`ok` |
| `manifest` | `$OPS_OUT/supply_chain_manifest.json` | `schema_version: 2`、`repos.prototype.commit`、`repos.plugin.commit`、`working_tree_clean`、`containers.napcat.image_digest`、`ok` |
| `smoke [--fast]` | stdout（`PASS/FAIL` 逐项） | bash 语法、py_compile、插件 import、服务 active；非 fast 追加两组关键 pytest |
| `backup` | `/root/backups/dududa20/dududa20_<时间戳>.tar.gz` | 保留最近 5 份，更旧的自动删除 |

判定口径：
- `health_status.json` 的 `ok == true`：服务 active + 有 Dududa 2.0 banner + 最近备份 < 7 天。
- `supply_chain_manifest.json` 的 `ok == true`：两仓库 commit 可解析 + napcat 镜像 digest 可解析。
- `smoke` 退出码：全部 PASS 为 0，任一 FAIL 为 1。

## 2. 退出门禁 `scripts/exit_gate_check.sh`

部署/发布前逐项核对 P0/P1/P2 证据，只读。完整模式跑门禁相关 pytest（约 1-2 分钟），`--fast` 只做静态检查（约 10 秒）。

```bash
cd /opt/dududa20-prototype
bash scripts/exit_gate_check.sh         # 完整 18 项
bash scripts/exit_gate_check.sh --fast  # 静态 16 项
```

覆盖内容：
- P0：forbidden imports / 插件拆分 / 事件契约 / 权限负向 / 提示词注入 / Memory 隔离 / 迁移回滚 / 插件真实加载。
- P1：`DUDUDA_ROUTER`、`DUDUDA_HYBRID_RENDER`、`DUDUDA_LIMITS_ENABLED`、`DUDUDA_MCP_CLIENT` 四个开关 + 429 降级 / 工具链降级重试硬上限 / 无重复回复、重复 Tool、错误 Memory。
- P2：`manage.sh` 有 rollback / `ops.sh` 可执行 / 插件薄壳 < 500 行 / 应用层不引用旧 main / manifest 与 health 实际生成且 `ok`。

退出码：全部 PASS 为 0；任一 FAIL 为 1。末尾输出 `summary: 门禁检查 N 项, PASS=... FAIL=...`。

## 3. 全量门禁 `scripts/eval_gate.sh`

Phase 9 Eval 门禁：语法编译（全部 packages/tests）→ 全量 pytest → 版本化 Eval，全绿才输出 `ALL GATES PASS`。

```bash
cd /opt/dududa20-prototype
bash scripts/eval_gate.sh
```

## 4. 备份/恢复 `manage.sh`

位于 `/root/manage.sh`，供 `ops.sh backup` 委托，也支持独立使用：

```bash
/root/manage.sh backup                      # 打包插件+packages+systemd 单元
/root/manage.sh restore <tar.gz>            # 停服解包恢复再启动
/root/manage.sh rollback                    # 恢复到最近一份备份
/root/manage.sh status                      # 服务状态 + 最近日志关键行
/root/manage.sh restart                     # 重启 + 3s + status
/root/manage.sh logs [n]                    # 最近 n 行日志（默认 30）
```

备份位置：`/root/backups/dududa20/`，保留最近 5 份。

## 5. 发布后验收清单（对照文档 2.5.11）

```bash
cd /opt/dududa20-prototype
git log --oneline -3                        # 期望的提交链
git status --short                          # 工作区干净（无输出）
bash scripts/exit_gate_check.sh             # 18 项全 PASS
bash scripts/eval_gate.sh                   # ALL GATES PASS
bash scripts/ops.sh health && bash scripts/ops.sh manifest   # 两 JSON 的 ok 均为 true
systemctl is-active astrbot                 # active
```

真机行为抽查：QQ 发图 + `@机器人` 追问，`journalctl -u astrbot -n 50 | grep -E "Flow start|Run end"` 应出现 `run_id/trace_id` 配对的 `Flow start → Run end → Flow end`；`data/traces/2026-08-06.jsonl`（按日滚动）记录 `model_request/model_response/render_result/memory_gate/run_end/flow_end` 等事件。
感知结果按日入库 `data/perceptions/YYYY-MM-DD.jsonl`（目录可用 `DUDUDA_PERCEPTION_DIR` 覆盖），记录每条消息的 speech_acts/topics/entities/candidate_intents/needs_tools/confidence 与 run/trace 绑定。

## 6. iCourse MCP 按群/按人切换 + 服务熔断（文档 2.5.6）

策略文件默认 data/mcp_access.json（首次启动由插件自动种下：default deny + DUDUDA_OWNER_IDS 放行），
可用环境变量 DUDUDA_MCP_ACCESS 覆盖路径。修改配置即时生效（按 mtime 热加载），无需重启。

    {
      "default_policy": "deny",
      "groups": {"allow": ["群号1"], "deny": []},
      "users":  {"allow": ["QQ号"], "deny": []}
    }

- 约束范围：六个 iCourse 服务 course_schedule / exam_schedule / academic_calendar /
  training_program / second_classroom / campus_notice；mcp.clock 等非 iCourse 能力恒允许。
- 判定优先级：用户 deny → 用户 allow（个人放行优先于群）→ 群 deny → 群 allow → default_policy（默认 deny，fail closed）。
- 群号兼容 group_123 与裸 123 两种写法。

服务熔断（Server Registry）：每服务连续失败 >= DUDUDA_MCP_BREAKER_THRESHOLD（默认 3）次
自动 open（快速失败，不再触碰 service）；冷却 DUDUDA_MCP_BREAKER_RESET（默认 30s）后放行
一个 half-open 探针，成功即恢复，失败立即重新 open。open 期间该能力从候选/健康列表剔除，
不会进入规划。

验证：

    grep -n "ICOURSE_SERVICE_IDS" packages/mcp/access.py
    grep -n "ServerCircuitBreaker" packages/mcp/registry.py
    python3.12 -m pytest tests/test_mcp_access_breaker.py -q   # 预期全绿
    # QQ 发 /dududa_mcp 查看访问策略与熔断状态
