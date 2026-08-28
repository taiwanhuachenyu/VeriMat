# VeriMat 复赛技术方案：热电材料构效关系发现的全自动闭环评测

版本：v1.0（2026-08-28 冻结）｜运行环境：Linux + OpenCode server + GLM-5.3-Flash（zhipu coding plan）

## 0. 设计原则

1. **零人工评测**：不用专家盲审。真值信号全部来自两个自动预言机——时间截断文献预言机与公开计算数据库预言机。
2. **一切可回放**：语料快照、证据定位、模型调用、评分过程全部哈希入库，评审可复现到字节。
3. **预算公平**：所有方法与消融共享同一语料快照、同一 token 硬预算、同一模型路由与随机种子。
4. **预注册**：指标、预算、消融矩阵、截断日期在跑实验前冻结进 `preregistration/semifinal_v1.json`，跑完不改。

## 1. 科学问题

固态电解质（SSE）构效结论依赖组成、工艺、测试条件与表征尺度，文献中支持性结论易被高估、
反证与失效条件易被忽略。VeriMat 将文献调研表示为**主张的状态估计**：每个候选构效关系
（claim）携带适用条件、支持证据、反证证据与验证状态
（PROPOSED / SURVIVED / NARROWED / REFUTED / UNRESOLVED），由确定性状态机（CEDG）裁决。

验证场景三族：块体热电（Bi2Te3、PbTe、Half-Heusler）、氧化物热电、低维/纳米结构热电，
对应 H1（纳米结构化–晶格热导率下降–ZT 提升）、H2（掺杂/空位–载流子浓度与功率因子）、
H3（能带简并/合金无序–Seebeck 与热导的解耦）。

## 2. 系统与模型路由

- 管线：问题契约 → Sciverse 语料冻结（SHA-256）→ 关系抽取 → CEDG 投影 → 反证感知
  Pareto-MCTS → 数据库交叉验证 → 报告（复用 `src/survey`、`src/discovery`、`src/evidence`、
  `src/materials`）。
- 模型路由：`opencode` 路线（本机 OpenCode server 127.0.0.1:4123 → zhipuai/glm-5.3-flash，
  工具全禁用、structured output、operation cache 幂等）。与 `claude-code` 路线协议同构，
  作为商业依赖可替换性的实证；主实验单一路由，路由披露写入 manifest。
- 语言模型只产出候选与检索动作；证据定位、时间截断、状态转换、预算、终态由确定性组件裁决。

## 3. 全自动评测设计（本方案核心）

### 3.1 时间截断文献预言机

| 窗口 | 年份 | 语料 | 用途 |
|---|---|---|---|
| Discovery | 2000–2021 | Sciverse 全文可得（`require_full_text`）SSE 构效文献快照，≤16 篇 | 系统输入：抽取关系、生成 claim 与 gap 判断 |
| Validation | 2022–2025 | 同口径快照，≤14 篇 | 评分器输入：自动检索每个 claim 的后窗支持/反证 |

评分器（`src/experiments/time_split_oracle.py`）对每个候选 claim 自动执行：
1. 在 Validation 窗检索反证查询（直接反例、失效条件、相反趋势）与支持查询；
2. 依据命中与抽取出的关系极性，裁定 oracle 状态 ∈ {supported, contradicted, unresolved}；
3. 与系统 CEDG 状态对照计分。

由此全自动导出的指标：
- **决策准确率**：系统状态与 oracle 一致的比例（SURVIVED↔supported，REFUTED↔contradicted，
  NARROWED 记部分分，UNRESOLVED 与 oracle=unresolved 一致记分、与确定态冲突记 0）。
- **反证召回**：oracle=contradicted 的 claim 中被系统标记为 REFUTED/NARROWED 的比例。
- **already-known false-gap rate**：系统在 Discovery 窗判定为"研究空白"的候选中，
  Validation 窗已给出解决/直接反驳的比例（自动检索裁定）——衡量新颖性宣称的可信度。
- **过度宣称率**：系统 SURVIVED 但 oracle=contradicted 的比例。

### 3.2 数据库预言机（稳定性类主张）

- 首选 **OQMD** REST（免 key）；**Materials Project** 为可选（有 key 时启用）。
- 对候选 claim 中出现的组成–性质对，凡属形成能 / energy-above-hull / 带隙，自动换算组成查询
  数据库，一致性 ∈ {consistent, inconsistent, uncovered}；inconsistent 计入反证证据并参与 CEDG。
- 未覆盖的性质（如离子电导率）按初赛约定记 `pending validation` 并保留原因，不影响计分。

### 3.3 确定性评分（无模型参与）

- **证据回放精度**：每条关系的证据 locator+SHA-256 对快照回放，能定位且极性一致的比例。
- **CEDG 状态合法性**：状态转换满足状态机充分条件的比例。
- **预算合规**：各方法实际 token 消耗 ≤ 预算上限。

### 3.4 校准与成本

- 系统对每个 claim 输出置信度；对确定态结果计算 **Brier 分数与 ECE**。
- **单位有效发现成本** = 总 tokens /（oracle 判定为 supported 且系统判对的 claim 数）。

## 4. 消融矩阵（统一预算 1.5M tokens/方法）

| 方法 | 检索 | 状态机 | 搜索 | DB 验证 | 假设 |
|---|---|---|---|---|---|
| V0 vanilla-RAG | 仅支持性 | – | – | – | 基线：单轮摘要式调研 |
| V1 dual-retrieval | 支持+反证 | – | – | – | 反证检索本身带来收益 |
| V2 dual+CEDG | 支持+反证 | ✓ | – | – | 状态机过滤噪声候选 |
| V3 full（VeriMat） | 支持+反证 | ✓ | Pareto-MCTS | ✓ | 完整系统最优 |
| A1 = V3 − MCTS | 支持+反证 | ✓ | 贪心单遍 | ✓ | MCTS 的边际贡献 |
| A2 = V3 − DB | 支持+反证 | ✓ | Pareto-MCTS | – | 数据库交叉验证的边际贡献 |

统计：按泄漏隔离组做配对置换检验 + Holm 校正；cluster bootstrap 置信区间。

## 5. 预注册与产物

- `preregistration/semifinal_v1.json`：指标定义、预算、种子（20260903）、截断日期、消融矩阵，
  在任何计分运行前写入并哈希。
- `experiments/run_sse_v1.py`：阶段化总入口（freeze → extract → discover → crossval → score
  → report），断点续跑复用 operation cache。
- 产物：每方法 `predictions.jsonl` / `per_claim.jsonl` / `summary.json`；跨方法统计与图；
  LaTeX 报告 + 复现清单（运行入口、依赖、配置、快照哈希）。
- 全部落 `results/sse_v1_<date>/`，manifest 声明 `scientific_result` 语义与路由披露。

## 6. 风险与对策

- 语料规模 vs 预算：Discovery 抽取按相关性分层采样，预算熔断由 transport 层硬保证。
- Oracle 自动裁定误差：反证/支持查询模板预注册固定；裁定规则为确定性映射，抽查样本入附录。
- 速率限制：flash 模型并发 1、失败退避；Sciverse 调用全部入审计链。
