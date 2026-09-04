# ADR-0002：实验治理与确定性宪法

- 状态：Accepted
- 日期：2026-09-04
- 范围：高杠杆功能的灰度、人工门、紧急停止与不可越权规则

## 决策

所有会放大机器人自主权的功能，都必须先进入版本化实验注册表。状态固定为：

```text
DRAFT → SHADOW → CANARY → PROMOTED
                  ↓
                PAUSED
```

只有已认证的人类 owner 能向右推进、恢复或扩大 rollout。系统与 kill switch
只能暂停、缩量或关闭。任何新实验从 `DRAFT` 开始，不能跳级。

分桶使用：

```text
HMAC-SHA256(deployment_salt, experiment_id | subject_id) mod 100
```

`strategy_version` 不参与分桶，因此代码升级不会移动实验对象。不同
`experiment_id` 的分桶相互独立。原始群号/用户号不进入持久实验状态与 Trace；
只保存 HMAC 摘要。缺少部署 salt 时，实验不能进入 live stage。

## Kill switch

注册表文件通过原子替换写入；消息热路径在每次实验决策前检查文件签名并同步
刷新。全局 kill 将实验置为 `PAUSED`，单群 kill 使用不可逆推出原 ID 的 HMAC
键。kill 只降低权限，不需要二次确认；恢复必须重新经过人工门。

主动参与的 kill 不影响明确 @、命令或回复链，也不消费这些路径的回复预算。

## 宪法层级

授权顺序固定为：

```text
Constitution → Permission/RBAC → Model
```

宪法是确定性代码而非 prompt。规则分为：

- `HARD`：owner 和运行时维护覆盖均不能绕过；包括跨用户私密记忆访问、跨
  Scope 敏感记忆搬运和 secret/credential 导出。
- `OPERATIONAL`：扩大实验、恢复实验、修改宪法等必须有人类确认；未来只有
  经离线验证器验签的限时维护覆盖才可能临时跳过。

宪法清单公开 `version + digest + rule metadata`。digest 包含规则元数据与
predicate 源码摘要，便于审计代码变化。

## 第一批上线边界

第一批只上线注册表、审计、kill 热路径和宪法骨架。内置主动参与实验为
`SHADOW + rollout=0`，不会因为部署而自动开启任何新群。进入 live canary
必须由后续单独的 go/no-go 决策完成。

## 验收不变量

1. 分桶跨进程、跨重启、跨策略版本稳定。
2. 缺少 salt、状态损坏或解析异常时不能获得 live 权限。
3. `DRAFT/SHADOW` 绝不改变用户可见行为。
4. kill 在一个消息决策周期内生效，且不影响明确 @。
5. owner 导出其他用户私密记忆仍被 `HARD` 规则拒绝并留审计。
6. `HARD` 规则不能被运行时 break-glass 覆盖。
7. rollout 扩大、恢复和晋级必须记录人类操作者、原因与版本。
