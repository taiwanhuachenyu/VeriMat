# 复赛提交清单（材料方向 / 算法赛题）

队伍：可以的 ｜ 作品：VeriMat ｜ 方向代码：MAT

## 对应提交包

- 【非代码材料】`AI4R_MAT_可以的_VeriMat_非代码材料.zip`
  - 报告：`docs/semifinal_report.md`（导出 PDF 同名）
  - PPT：`AI4R_MAT_可以的_VeriMat_复赛PPT.pptx`
  - 研究数据与证据包：语料快照、检索审计链、claims/relations、gap 清单、oracle 裁定明细
  - 运行与评测包：六方法 predictions、操作台账、token 台账、异常/拒绝留档、supervisor 日志
- 【代码材料】`AI4R_MAT_可以的_VeriMat_代码材料.zip`
  - 本仓库（对应 tag `semifinal-v2`，commit 见下方）
  - Prompt 披露：`docs/PROMPTS.md`
  - 指标与分析代码：`src/experiments/scoring.py` + `experiments/run_semifinal_v1.py --stage score`
  - README + 一键命令：`scripts/smoke_test.sh`、`scripts/reproduce_core.sh`
  - 环境变量模板：`config/verimat.env.example`（无密钥）

## 可追溯性

报告/summary 中每一个数字对应链路：
`git tag semifinal-v2` → `preregistration/semifinal_v2.json` →
`results/semifinal_v2/retrieval_cache.jsonl`（统一检索快照）→
`results/semifinal_v2/*_model_operations.sqlite`（逐操作 token/响应台账）→
`results/semifinal_v2/{method}/predictions.jsonl` → `results/semifinal_v2/summary.json`。

## 运行次数披露

- v1（120 主张，Windows/Claude Code Opus 5 部分 + Linux/GLM 部分）：开发性运行，验证管线
- v2（258 主张，Linux/opencode/GLM-5.3-Flash）：**正式结果，单次完整运行，无筛选**
- 两次运行的全部产物均随仓库公开

## Agent Harness 声明

- 构筑：opencode（Linux）+ Claude Code（Opus 5，Windows v1 阶段）
- 运行：opencode headless（GLM-5.3-Flash）
- 轨迹：`submission/agent_traces/`（运行阶段 10,063 sessions 全量；构筑阶段说明见其中 HARNESS.md）
