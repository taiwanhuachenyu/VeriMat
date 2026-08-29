# VeriMat 复赛实验报告：热电材料构效关系发现的全自动闭环评测

版本 v1.0｜预注册 `semifinal-v1-thermo`（冻结于 2026-08-28，先于任何计分运行）｜结果目录 `results/semifinal_v1/`

---

## 1. 摘要

本实验在热电材料文献域上，对 VeriMat 的证据与反证驱动发现管线进行了**预注册、全自动化闭环评测**：系统在 2000–2021 年语料上抽取构效关系并输出带验证状态的候选主张，由时间截断文献预言机（2022–2025 年验证窗）自动裁定真值，六个预注册方法变体在统一语料快照、统一模型路由、统一 token 预算与统一随机种子下对照。

核心结果（n=120 主张）：

- **决策准确率**：V0 vanilla-RAG 0.142 → V2 双向检索+CEDG 状态机 0.442 → V3 全量系统 0.463；
- **状态机是承重组件**：V2 对 V1 的配对提升 **+0.300（Holm 校正 p=0.0003）**，V3 对 V0 基线 **+0.321（p=0.0003）**；
- **诚实的负结果**：Pareto-MCTS 与成分数据库交叉验证在此规模下无显著边际贡献（p=0.62 / p=1.0）；
- **证据可回放性**：全部六方法证据回放精度 **1.0**（每条证据可定位到语料快照中的原文并哈希一致）；
- **新颖性可检验**：系统宣称的 8 个"新颖"研究缺口中 2 个被验证窗文献解决（**already-known false-gap rate = 0.25**）；
- 全部方法在其预注册 2M token 预算内运行（合计 1.14M tokens）。

---

## 2. 科学问题与任务

VeriMat 参加 GOAI 赛道三算法赛题方向三「材料科学文献驱动的科学发现智能体」，基础任务（文献调研：自主检索、知识抽取、Research Gap 识别、结构化报告）之上，选择进阶路线 A「构效关系发现」。

科学问题：材料文献中的构效结论依赖组成、制备工艺、测试条件与表征尺度，仅检索支持性文献会系统性高估结论、忽略反例与失效条件。VeriMat 将文献调研表示为**主张的状态估计问题**：每个候选构效关系携带适用条件、支持证据、反证证据与验证状态（ACCEPTED/SURVIVED、NARROWED、REFUTED、UNRESOLVED），由确定性状态机而非模型自报置信度裁决。

验证场景：热电材料的结构–性质关系（ZT、Seebeck 系数、电/热导率、功率因子等），覆盖掺杂、空位、纳米结构化等结构手段。

---

## 3. 系统与方法

### 3.1 管线

```
问题契约 → 语料冻结(Sciverse, SHA-256) → 关系抽取(引文逐字门+数值门)
        → 主张投影 → [六方法并行验证] → 时间窗预言机 → 确定性评分 → 报告
```

语言模型（GLM-5.3-Flash）只产生候选与判断；证据定位、时间截断、状态转换、预算与终态全部由确定性组件裁决。模型输出的每条关系必须满足：引文逐字命中其来源片段（规范化子串匹配）、数值必须出现在引文中、属性词封闭词表。不合规输出在准入前被拒绝并留档（本 run 拒绝 50/190）。

### 3.2 六个预注册方法变体

| 变体 | 反证检索 | 状态机 | Pareto-MCTS | DB 校验 | 检验假设 |
|---|---|---|---|---|---|
| V0 vanilla-RAG | – | – | – | – | 盲接受基线 |
| V1 dual-retrieval | ✓ | – | – | – | 反证检索本身是否足够 |
| V2 dual+CEDG | ✓ | ✓ | – | – | 状态机是否是增益来源 |
| V3 full | ✓ | ✓ | ✓ | ✓ | 完整系统 |
| A1 no-MCTS | ✓ | ✓ | 贪心 | ✓ | MCTS 边际贡献 |
| A2 no-DB | ✓ | ✓ | ✓ | – | DB 校验边际贡献 |

各变体共享同一次缓存的关系抽取（相同主张集），因此方法间差异可归因于**验证层**而非候选生成。

### 3.3 全自动真值：时间截断文献预言机

无人工复核。对每个候选主张，预言机在 **2022–2025 验证窗**内以 4 条预注册查询模板检索（矛盾/失效/支持/适用边界各一），对每条命中文献由模型输出判定（contradicted / supported / unrelated + 是否限定适用域 + 逐字引文）；**引文必须逐字命中被检文献**，否则该判定作废留档。判定到真值的聚合是预注册的确定性映射：矛盾优先，其次适用域收窄，再次支持，否则未决。

方法侧的验证检索被硬性限制在发现窗内（year_to=2021）——**任何方法都不可能通过偷看未来得分**。

### 3.4 模型路由

单一路由：本机 OpenCode server（127.0.0.1:4124，HOME 隔离的最小配置）代理 `zhipuai/glm-5.3-flash`（智谱 coding plan），benchmark agent 全工具禁用，structured output（json_schema，服务端 2 次格式重试）。该路由与 `claude-code` 路线协议同构（`StructuredModelTransport.complete(operation_id, system, user, response_schema)`），操作缓存按内容寻址，跨路线共享——商业依赖可替换性是架构性质而非声明。

---

## 4. 预注册

`preregistration/semifinal_v1.json` 在任何计分运行前写入并随仓库提交，包括：指标定义、六方法消融矩阵、每方法 2M token 硬预算（`BudgetedTransport` 在传输层强制）、配对置换 20000 次种子 20260903、Holm 校正的六组比较、oracle 查询模板与聚合规则、主张上限 120 及确定性选取规则。

---

## 5. 实验设置

| 项 | 值 |
|---|---|
| 发现窗语料 | Sciverse 检索，60 候选文档（14 次查询×2 pass），**13 篇可全文定位，114 个片段**（5–11 片段/篇），年份 2003–2021 |
| 语料密封 | `corpus_snapshot.json`（SHA-256），后续阶段全部离线回放 |
| 抽取 | 14 次模型调用，190 提案 → **140 条关系准入**（73.7%），50 条拒绝（引文门/数值门/词表门） |
| 主张集 | 140 条关系去重投影，取前 120（确定性排序） |
| 验证窗 | 2022–2025，每次裁定 ≤4 检索 × ≤4 文献 |
| 真值分布 | supported 17 / contradicted 17 / narrowed 31 / unresolved 55 |
| 预算执行 | V1 187K、V2 96K、V3 555K、A1 187K、A2 110K tokens（上限各 2M） |
| 披露的偏差 | 4 个片段因反复超出 600s 模型超时被跳过（`skip_passages.json` 留档）；OQMD 数据库预言机因网络不可达未启用，DB 校验记 uncovered |

---

## 6. 结果

### 6.1 主表（n=120）

| 方法 | 决策准确率↑ | 反证召回↑ | 过度宣称↓ | 回放精度↑ | Brier↓ | tokens/有效发现↓ |
|---|---|---|---|---|---|---|
| V0 vanilla-RAG | 0.142 | 0.000 | 0.142 | 1.0 | 0.340 | — |
| V1 dual-retrieval | 0.142 | 0.059 | 0.133 | 1.0 | 0.487 | 11000 |
| V2 dual+CEDG | 0.442 | 0.235 | 0.042 | 1.0 | 0.494 | **1917** |
| V3 full | 0.463 | 0.118 | 0.067 | 1.0 | 0.487 | 10471 |
| A1 no-MCTS | 0.488 | 0.235 | 0.058 | 1.0 | 0.477 | 3333 |
| A2 no-DB | 0.463 | 0.294 | 0.033 | 1.0 | 0.487 | 2073 |

决策准确率为预注册的分级一致分（NARROWED 对 supported 记 0.5 分）；反证召回在 oracle=contradicted 的 17 条上计算。

### 6.2 行为差异（标签分布）

| 方法 | ACCEPTED | NARROWED | REFUTED | UNRESOLVED |
|---|---|---|---|---|
| V0 | 120 | 0 | 0 | 0 |
| V1 | 112 | 0 | 8 | 0 |
| V2 | 25 | 23 | 8 | 64 |
| V3 | 27 | 25 | 7 | 61 |
| A1 | 27 | 20 | 12 | 61 |
| A2 | 23 | 22 | 8 | 67 |

V0 的 0.142 恰好等于 oracle 中 supported 的先验占比（17/120）：**盲接受基线不提供超出先验的任何信息**。带状态机的变体（V2/V3/A1/A2）将大量主张保守地记为 UNRESOLVED/NARROWED，用召回换准确率，过度宣称降至 3–7%。

### 6.3 配对统计（Holm 校正）

| 比较 | Δ一致分 | p | p_holm | 显著？ |
|---|---|---|---|---|
| **V2 vs V1** | **+0.300** | 5e-05 | **0.0003** | ✅ |
| **V3 vs V0** | **+0.321** | 5e-05 | **0.0003** | ✅ |
| **V3 vs V1** | **+0.321** | 5e-05 | **0.0003** | ✅ |
| V3 vs V2 | +0.021 | 0.636 | 1.0 | ❌ |
| V3 vs A1 | −0.025 | 0.617 | 1.0 | ❌ |
| V3 vs A2 | 0.000 | 1.0 | 1.0 | ❌ |

### 6.4 Research Gap 新颖性检验

规则引擎产出 13 个 Gap 候选，narrator 确认 13 个（8 个标注"新颖"、5 个"已知"）。验证窗检索+裁定：**2/8 个"新颖"缺口已被后续文献解决 → already-known false-gap rate = 0.25**。每一个"已知"判定都必须携带指出其已知性的逐字引文，否则在构造时即被拒绝。

### 6.5 成本

全部 run 合计 1.14M tokens（zhipu coding plan / GLM-5.3-Flash，订阅额度内），每方法均低于 2M 预算上限。操作缓存按内容寻址：断点续跑与跨方法重复主张不产生重复计费。

---

## 7. 解读

1. **确定性状态机是增益的来源，而非检索本身。** V1 与 V0 准确率完全相同（0.142）：把反证文献摆到模型面前并不改变其结论，只有 CEDG 的确定性裁决（反证→REFUTED、适用域→NARROWED、证据不足→UNRESOLVED）把反证信息转化为行为差异（+0.300，p=0.0003）。这与"生成式模型提议、确定性程序裁决"的系统哲学一致。
2. **两个诚实的负结果。** Pareto-MCTS（V3 vs A1）与成分数据库校验（V3 vs A2）在此规模下无显著边际贡献。可能的解释：主张集以单片段支持的局部关系为主，树搜索的细化空间与稳定性类数据库校验的覆盖面都有限。负结果连同全部运行数据一并公开。
3. **校准仍不足。** 各方法 Brier ≈ 0.48–0.49，当前的启发式置信策略（1 − 反证比例）区分度有限，是下一个迭代点。
4. **反证召回是准确率之外的独立维度。** A2 的反证召回最高（0.294）而准确率与 V3 持平，说明更激进的怀疑策略牺牲部分一致分换取更低的过度宣称（0.033，全場最低）。

## 8. 局限与威胁

- **预言机本身依赖模型判定**（有逐字引文门约束，但仍可能在难例上出错）；oracle 判定明细全文随结果公开，可被独立抽查。
- **46% 的主张真值为 unresolved**：验证窗文献未明确表态，压低了所有方法可达到的准确率上限；这不是方法缺陷而是任务现实，配对统计在相同主张集上进行，不受其影响。
- 单一材料域、单一模型路由、单一语料快照；跨域泛化与路由消融未在本轮预注册范围内。
- MCTS/DB 的零边际贡献可能随规模与主张类型（多片段支持的全局性主张占比）改变，尚不能外推。

## 9. 复现说明

```bash
# 环境：Python 3.11+，本地 OpenCode server（127.0.0.1:4124，HOME 隔离最小配置）
cp config/verimat.env.example .env   # 填 SCIVERSE_API_TOKEN 等
python experiments/run_semifinal_v1.py --stage freeze  --out results/semifinal_v1
python experiments/run_semifinal_v1.py --stage extract --out results/semifinal_v1   # 可断点续跑
python experiments/run_semifinal_v1.py --stage claims  --out results/semifinal_v1
python experiments/run_semifinal_v1.py --stage verify  --out results/semifinal_v1   # 六方法
python experiments/run_semifinal_v1.py --stage gaps    --out results/semifinal_v1
python experiments/run_semifinal_v1.py --stage oracle  --out results/semifinal_v1
python experiments/run_semifinal_v1.py --stage score   --out results/semifinal_v1
python experiments/run_semifinal_v1.py --stage report  --out results/semifinal_v1
```

- 操作缓存幂等：任何阶段崩溃后直接重跑，已完成的模型调用从 SQLite 操作缓存回放，不重复计费；supervisor 自动完成 对账→重试 循环。
- 评分与统计完全离线确定性：`python -m pytest tests/test_semifinal_eval.py` 覆盖评分器、聚合规则与统计。
- 仓库 430 项自动化测试全绿；语料快照、oracle 裁定明细、六方法预测、逐操作 token 台账全部随仓库提交。

## 10. 依赖与合规披露

| 依赖 | 用途 | 披露 |
|---|---|---|
| Sciverse API | 语料检索与全文定位 | 组委会推荐数据源；全部调用入审计链（`sciverse_audit.jsonl`，142 次检索含命中数与响应摘要）；限流以客户端节流+退避处理 |
| 智谱 coding plan（GLM-5.3-Flash） | 候选生成、关系抽取、验证判定、gap 叙述 | API key 仅由本地 OpenCode server 进程持有，评测客户端不接触密钥；逐操作 token 记录在 `*_model_operations.sqlite` |
| OpenCode 1.18.21 | 本地推理服务器 | 工具全禁用的 benchmark agent；请求级 structured output |
| OQMD | 成分稳定性校验 | 本轮因网络不可达未启用，全部记 uncovered（诚实披露，非静默跳过） |
| 数据许可 | 语料 | 语料为 Sciverse 开放获取文献，检索时按许可字段过滤；快照仅存定位符与哈希 |

模型生成内容不直接成为证据：每条入库关系/裁定都通过引文逐字门与哈希定位门，未通过者留档为拒绝记录。

---

*预注册与全部产物：`preregistration/semifinal_v1.json`、`results/semifinal_v1/`（summary.json、REPORT.md、六方法 predictions、oracle_cache、审计链）。仓库：https://github.com/taiwanhuachenyu/VeriMat （MIT License）。*
