# Agent Harness 声明与会话轨迹

依据《材料方向材料提交说明》：若项目构筑或运行使用了 AI Agent，须声明 Harness 名称并提交
完整会话轨迹，构筑与运行阶段分目录存放。

## 运行阶段（正式结果 v2）：`run_opencode_glm/`

- Harness：**opencode 1.18.21**（headless server 模式，HOME 隔离最小配置）
- 模型：zhipuai/glm-5.3-flash，端点 https://open.bigmodel.cn/api/coding/paas/v4
- 会话轨迹：`sessions_v2.jsonl` —— 10,063 个 session / 20,109 条消息，覆盖正式 run 的
  **每一次** LLM 调用（关系抽取、验证判定、精炼提议、oracle 裁定、gap 叙述、发现包生成）。
  每个 session 的 payload 中可见工具禁用配置（bash/read/write/edit/... 全 false）与
  structured output 请求。
- 与证据链的关系：session title 即 VeriMat 操作 id（如 `benchmark-<request_hash 前 16 位>`），
  可与 `*_model_operations.sqlite` 的操作台账、`*_usage.jsonl` 的 token 台账逐一对应。

## 构筑阶段：`build_opencode/`

- Harness：**opencode**（交互式 TUI，Linux）+ 早期 **Claude Code**（Opus 5，Windows，
  v1 开发阶段）
- 交互式构筑会话的原始轨迹存储在各自 harness 的本地会话库中；由于构筑跨越两台机器
  （Linux 服务器 + Windows VM），随包提交的是：构筑产物本身（git 逐 commit 历史，
  2cde03c…c2b3c23 全部可追溯）、以及运行轨迹（上节）。构筑交互的完整 TUI 导出可按需
  补充提供。
- 声明：代码为人工与 Agent 协作产出；全部自动化测试（439 项）与确定性验收门不依赖
  Agent 会话即可独立复现结果。
