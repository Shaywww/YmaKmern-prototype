# Dududa 2.0 Agent Runtime 原型

基于文档 https://docs.mmdustc.top/dududa107/ 开发的 2.0 独立原型。

## 一键安装（不需要装 Python）

双击 setup.bat，全自动：下载 Python → 安装依赖 → 跑测试验证。

## 手动安装

`ash
pip install -r requirements.txt
`

## 运行

`ash
# 启动 Web 控制台
python -c "from packages.control_plane import run_server; run_server()"
# 浏览器打开 http://127.0.0.1:8000
`

## 运行测试

`ash
python -m pytest tests/ -q
`

## 项目结构

`
packages/dududa-agent/src/dududa/
├── core/             # 领域模型（消息/状态/记忆/人格/渲染）
│   └── persona/      # OC 人格系统（4 套预设）
├── runtime/          # 控制中枢（13 阶段 Pipeline）
├── router/           # 模型路由
├── safeguards/       # 安全校验（身份/隐私/预算）
├── observability/    # Trace 与事件
├── mcp/              # 校园 MCP 服务（课表/考试/日历/二课/通知）
├── planner/          # 多步骤工具编排
├── control_plane/    # Web 控制台（FastAPI + 仪表盘）
└── adapters/astrbot/ # QQ 机器人适配层

ops/                  # 运维脚本（exit_gate / ops / eval_gate / smoke_net）
tests/                # 分层测试：unit / contracts / integration / evals / fixtures / smoke
`

## 对接 QQ

详见 packages/dududa-agent/src/dududa/adapters/astrbot/plugin.py，需额外安装 AstrBot：

`ash
pip install astrbot
`

## 状态

238 核心测试 + 26 适配器测试 = 264 tests，0 失败。