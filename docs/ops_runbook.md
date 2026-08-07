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
#   MCP capabilities registered: 8
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
| `smoke-net` | stdout（`PASS/FAIL` 逐项） | 真实网络：主/降级网关可达 + 生产 Router LLM 往返 + mcp.clock 调用（需 systemd 注入密钥，与阻塞 CI 分开） |
| `backup` | `/root/backups/dududa20/dududa20_<时间戳>.tar.gz` | 保留最近 5 份，更旧的自动删除 |

判定口径：
- `health_status.json` 的 `ok == true`：服务 active + 有 Dududa 2.0 banner + 最近备份 < 7 天。
- `supply_chain_manifest.json` 的 `ok == true`：两仓库 commit 可解析 + napcat 镜像 digest 可解析。
- `smoke` 退出码：全部 PASS 为 0，任一 FAIL 为 1。

## 2. 退出门禁 `scripts/exit_gate_check.sh`

部署/发布前逐项核对 P0/P1/P2 证据，只读。完整模式跑门禁相关 pytest（约 1-2 分钟），`--fast` 只做静态检查（约 10 秒）。

```bash
cd /opt/dududa20-prototype
bash scripts/exit_gate_check.sh         # 完整 43 项（含 CP gate）
bash scripts/exit_gate_check.sh --fast  # 静态 38 项
```

覆盖内容：
- P0：forbidden imports / 插件拆分 / 事件契约 / 权限负向 / 提示词注入 / Memory 隔离 / 迁移回滚 / 插件真实加载。
- P1：`DUDUDA_ROUTER`、`DUDUDA_HYBRID_RENDER`、`DUDUDA_LIMITS_ENABLED`、`DUDUDA_MCP_CLIENT` 四个开关 + 429 降级 / 工具链降级重试硬上限 / 无重复回复、重复 Tool、错误 Memory。
- P2：`manage.sh` 有 rollback / `ops.sh` 可执行 / 插件薄壳 < 500 行 / 应用层不引用旧 main / manifest 与 health 实际生成且 `ok`。
- P10：两仓库无 legacy 副本（`*.bak*`/`*.swp`/`main.py.final` 等）、工作区干净、应用层无旧入口路径引用、文档含 rollback/清理清单。

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
bash scripts/exit_gate_check.sh             # 43 项全 PASS
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

## 7. 用户画像（SESSION_STATE / USER_PROFILE，文档 2.4.6）

画像/会话状态按日累积，文件默认 data/profiles.json（插件路径
/root/data/plugins/dududa20/data/profiles.json），可用 DUDUDA_PROFILE_FILE 覆盖。

- 会话状态（SESSION_STATE）：每条消息更新 message_count / last_intent / active_topics（最新在前，最多 8 个）。
- 用户画像（USER_PROFILE）：仅在 engaged（@ / 显式命令 / 回复链）时学习，避免群聊噪音；
  规则提取称呼（"叫我XX"）、偏好（"我喜欢XX"）、事实（"我是XX"），无 LLM 调用。
- 上下文注入：ContextSnapshot.user_preference（称呼/偏好/事实）+ conversation.active_topics；
  生产合成时画像摘要注入 LLM 前缀（"用户希望被称为… / 用户偏好… / 最近话题…"）。
- 隔离：按 platform + bot + actor 隔离用户，按 conversation + actor 隔离会话；
  文件损坏时隔离为 .corrupt-<ts> 并 fail-closed（不参与召回）。

验证：

    grep -n "class ProfileStore" packages/core/profile.py
    python3.12 -m pytest tests/test_profile_session_p0.py -q   # 预期全绿
    # QQ @机器人 发：叫我XX / 我喜欢XX -> 检查 data/profiles.json

## 8. Dashboard 收敛（文档清单第 7 项）

管理面端口 6185（AstrBot Dashboard）、3001/6099（NapCat OneBot / WebUI）仅放行受信来源：
本机回环、私网（10/8、172.16/12、192.168/16）、CGNAT（100.64/10）、运维公网 IP；其余一律 DROP。

- 脚本 /usr/local/sbin/dududa-fw.sh（幂等，可重复执行）+ systemd 单元 dududa-fw.service（开机自启）。
- 关键坑：podman 发布端口由 DNAT 转发且容器重启后 IP 会漂移（10.88.0.2 -> 10.88.0.3），
  INPUT 链看不到 3001/6099 流量；拦截必须放 raw PREROUTING（DNAT 之前，nat 表禁止 DROP），
  FORWARD 纵深防御按 podman 子网 10.88.0.0/16 匹配。
- token 已重置：NapCat WebUI token（webui.json）、OneBot 反向 WS access token
  （onebot11_*.json 与 AstrBot cmd_config.json 双侧一致）；旧配置备份为 .bak-<ts>。
- autoLoginAccount 已设为 3823883634，NapCat 重启后自动登录。

验证：

    bash /usr/local/sbin/dududa-fw.sh && iptables -t raw -L DUDUDA-PRE -n   # 白名单 RETURN + DROP
    systemctl is-enabled dududa-fw                                        # enabled
    curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:6099/         # 301（白名单可达）
    # 负向验证（raw 表不支持 REJECT，用 DROP）：
    iptables -t raw -I DUDUDA-PRE 1 -p tcp -m tcp --dport 6099 -s <本机IP> -j DROP
    curl -s -m 6 http://<公网IP>:6099/   # 超时/000
    iptables -t raw -D DUDUDA-PRE -p tcp -m tcp --dport 6099 -s <本机IP> -j DROP
    grep -F autoLogin /opt/napcat/config/webui.json                       # 3823883634


## 9. Phase 10 兼容清理（legacy 清零）

目标（文档 2.5.11 Phase 10）：生产入口、测试、文档、旧 import/path 消费者与 rollback 清单全部满足；legacy 移除清单为零。

清理口径（`scripts/exit_gate_check.sh` P10 段静态检查，`--fast` 也执行）：
- 原型仓库与插件仓库工作区必须干净（`git status --porcelain` 为空）。
- 两仓库不得存在 legacy 代码副本：`*.bak*`、`*.swp`/`*.swo`、`main.py.final`/`main.py.stable*`/`main.py.v2.final`。
  缓存（`__pycache__`、`.pytest_cache`）与运行数据（`data/`、`deploy/.env*`）不在清单内。
- `packages/` 不得引用插件旧入口路径 `/root/data/plugins/dududa20/main.py`；测试与运维脚本加载薄壳是预期行为，不算 legacy。
- 生产入口 = 插件薄壳 `main.py`（< 500 行），业务逻辑在 `/opt/dududa20-prototype/packages/`。

回滚路径（rollback 清单）：
- 代码回滚：两仓库均以 git 为唯一事实源，历史提交即回滚证据；按需 `git revert` 或 `git checkout <rev>`。
- 数据回滚：`/root/manage.sh rollback`（自动取最新 `dududa20_*.tar.gz` 恢复）；`/root/manage.sh restore <file>` 指定备份。
- 发布前必须全绿：`bash scripts/exit_gate_check.sh`（P0/P1/P2/CP/P10）+ `bash scripts/eval_gate.sh` + `bash scripts/ops.sh smoke-net`。

验证：

    bash scripts/exit_gate_check.sh        # summary 43 项 PASS=43 FAIL=0
    find /opt/dududa20-prototype -name '*.bak*' -not -path '*/.git/*' | wc -l   # 0
    find /root/data/plugins/dududa20 -name '*.bak*' | wc -l                    # 0
    cd /opt/dududa20-prototype && git status --porcelain                       # 空



## 10. Control Plane 运维（ADR-0001）

独立管理面（FastAPI，默认 127.0.0.1:8000），不参与 QQ 消息同步链路。

- CP-P0 安全基线：Bearer/X-CP-Token 鉴权（缺省拒绝）、写操作经 PermissionEngine（owner 可写）、
  JSONL 审计、Redactor 出参脱敏、Trace Scope 过滤、MCP query 经 CapabilityRegistry + access 策略。
- CP-P1 只读面板：Trace Viewer（/traces[/{trace_id}|/runs/{run_id}]）、Memory Explorer（/memory，
  经 JSONMemoryRepository，RESTRICTED 永不召回 / PRIVATE 仅本人）、Eval 报告（/eval/reports）、
  MCP Health（/mcp/services）。全部只读，无写路由。
- CP-P2 高级能力：Agent Playground（POST /playground/run，沙箱内：独立 Memory/Trace/NoOp 投递、
  DANGEROUS 能力移除、无 LLM key 时离线占位，仅 owner）、成本/性能面板（/metrics/costs、
  /metrics/performance，聚合 data/traces JSONL）、自动告警（/alerts：模型降级率/连续错误/MCP 健康/
  Memory gate 压力，10 分钟窗口）、日志检索（/logs?level=&query=&source=traces|audit|all，脱敏出参）。

部署与日常：

    bash /opt/dududa20-prototype/deploy/control_plane/install_cp.sh   # 安装 systemd 单元 + cp.env（随机 token）+ 防火墙白名单
    systemctl status dududa-cp                                        # 服务状态
    bash /opt/dududa20-prototype/scripts/ops.sh cp status             # token 配置/监听/审计行数/单元状态
    bash /opt/dududa20-prototype/scripts/ops.sh cp restart            # 重启
    curl -s http://127.0.0.1:8000/health                              # 存活探针（无 token 豁免）
    curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/personas

安全要点：
- token 在 /root/data/cp.env（systemd EnvironmentFile），仅 root 可读；ops.sh cp status 只显示是否已配置，不打印明文。
- 防火墙 dududa-fw.sh 已将 8000 纳入受信来源白名单（纵深防御；默认只绑 loopback）。
- 运维坑：历史上 8000 曾被旧原型进程（run_server 无鉴权版）长期占用导致 systemd 单元起不来，
  若 `systemctl is-active dududa-cp` 反复 restart 且日志报 address already in use，
  用 `ss -ltnp | grep ':8000 '` 定位旧进程并清理后再 restart。
- CP 备份已纳入 /root/manage.sh backup（dududa-cp.service + cp.env + packages）。
