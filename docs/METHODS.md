# VeriMat 复赛技术设计

版本：v2.0｜预注册：`semifinal-v2-thermo`｜正式结果：`results/semifinal_v2/`

## 1. 设计目标

材料文献中的“提高稳定性”“降低热导率”“提升 ZT”等结论通常依赖组成、制备工艺、温度、
载流子浓度、测量方向和表征尺度。传统 RAG 容易把局部观察外推为普遍规律，也容易因为只检索
支持证据而漏掉反例和失效条件。

VeriMat 的目标不是生成更流畅的综述，而是输出满足以下条件的发现包：

1. 主张可以被一个明确实验否证；
2. 适用边界显式保存；
3. 支持与反证证据均能定位到语料快照原文；
4. 终态由可审计规则裁决，不由模型自报置信度决定；
5. 代码、配置、数据、日志、预测和结果能形成闭合追溯链。

## 2. 端到端流程

```text
研究问题与否证标准
  → Sciverse 语料冻结（2000–2021，SHA-256）
  → 关系抽取（三道准入门）
  → Claim 投影
  → 六方法统一快照验证
  → 2022–2025 时间窗预言机
  → CEDG 终态与统计评分
  → 可证伪发现包 / 拒绝留档
```

生成式模型只参与候选关系、局部证据判断、gap 叙述和最小实验文本的提议。语料范围、原文定位、
关系准入、状态转换、预算、缓存、终态、统计与报告均由确定性代码执行。

## 3. 核心算法

### 3.1 关系准入门

每个模型提议必须同时通过：

- **引文逐字门**：规范化后的 quote 必须是已暴露 passage 的子串；
- **数值回放门**：value / temperature 必须实际出现在 quote 中；
- **封闭词表门**：property、direction、method 等字段必须属于预注册 schema。

正式 v2：354 个提案，258 个准入，96 个拒绝。拒绝分布为 contract violation 4、
quote_not_in_passage 37、temperature_not_in_quote 40、value_not_in_quote 15。

### 3.2 CEDG：Claim–Evidence Decision Graph

图节点为 `CLAIM / QUERY / EVIDENCE / DECISION`，边为
`SUPPORTS / CONTRADICTS / BOUNDS / PRECEDES`。合法终态：

| 终态 | 充分条件 |
|---|---|
| SURVIVED | 反证检索已执行，且当前证据集中没有直接反例 |
| NARROWED | 证据只支持更窄的组成、工艺、温度或测量边界 |
| REFUTED | 至少一条可回放证据直接反驳主张 |
| UNRESOLVED | 证据不足，或查询完成但无法形成确定态 |

模型置信度作为校准特征保存，不参与终态充分条件。这个限制使“模型看见反证但仍坚持原答案”
不再成为系统终点。

### 3.3 反证感知 Pareto-MCTS

搜索状态包含：有边界主张、支持/反证证据、未决条件、数据库状态和物理检查。动作包括：
增加描述符、收窄条件、跨材料家族类比、提出反例查询、请求数据库验证。

七维奖励全部归一化并最大化：

```text
utility, evidence, counterevidence survival, database,
falsifiability, physics, simplicity
```

树内选择使用预注册权重轮换的多目标 PUCT；搜索结束后以严格 Pareto 支配重算非支配档案。
进入档案前必须满足：

```text
主张完整 ∧ 边界明确 ∧ 证据可定位 ∧ 反证已查 ∧ 物理检查已声明
```

正式 v2 中，V3 相对 V2 没有显著增益。因此 MCTS 的结论是“已实现、可复用，但本规模未证明
边际性能价值”，不能把结构复杂度当作实验效果。

## 4. 全自动闭环评测

### 4.1 时间截断

| 窗口 | 年份 | 可见对象 | 作用 |
|---|---:|---|---|
| Discovery | 2000–2021 | 方法与六个变体 | 抽取、检索、状态预测 |
| Validation | 2022–2025 | 预言机 | 自动裁定后续文献是否支持、反驳或收窄 |

方法侧检索函数硬截断 2021，预言机检索函数硬限定 2022–2025，从代码结构上排除“偷看未来”。

### 4.2 预言机

每个 claim 执行四类预注册查询：直接矛盾、失效条件、支持证据、适用边界。模型逐 passage
判断，quote 不逐字命中即作废。固定聚合规则：

```text
contradicted > narrowed > supported > unresolved
```

正式真值分布：supported 38、contradicted 41、narrowed 35、unresolved 144。

### 4.3 六方法消融

| 方法 | 双向检索 | CEDG | Pareto-MCTS | 数据库 |
|---|---:|---:|---:|---:|
| V0 vanilla-RAG | – | – | – | – |
| V1 dual-retrieval | ✓ | – | – | – |
| V2 dual-CEDG | ✓ | ✓ | – | – |
| V3 full | ✓ | ✓ | ✓ | ✓ |
| A1 no-MCTS | ✓ | ✓ | – | ✓ |
| A2 no-DB | ✓ | ✓ | ✓ | – |

所有方法共享同一 claim 集、检索快照、模型路由、预算与统计随机种子。主要比较和 Holm 校正
在计分前冻结。

## 5. 实证结果与解释

| 方法 | 决策准确率 | 反证召回 | 过度宣称 | 回放精度 |
|---|---:|---:|---:|---:|
| V0 vanilla-RAG | 0.1473 | 0.0000 | 0.1589 | 1.0000 |
| V1 dual-retrieval | 0.1589 | 0.1220 | 0.1395 | 1.0000 |
| V2 dual-CEDG | **0.4826** | **0.3902** | **0.0426** | 1.0000 |
| V3 full | 0.4612 | 0.3171 | 0.0465 | 1.0000 |
| A1 no-MCTS | 0.4612 | 0.3415 | 0.0388 | 1.0000 |
| A2 no-DB | 0.4477 | 0.2683 | 0.0465 | 1.0000 |

V2 相对 V1 增加 0.3236（Holm p=0.0003），说明**把反证展示给模型并不够，必须把反证接入
确定性状态转换**。V3 相对 V2 为 -0.0213（Holm p=1.0），说明本规模下 MCTS/DB 没有可证明
的边际贡献。

## 6. 可用产物

系统产出 44 个发现包，每个包含：

- `falsifiable_statement`
- `boundary`
- `evidence[]`（doc_id、passage_id、quote、content_sha256）
- `counterevidence_considered`
- `minimal_verification_experiment`
- `observable` 与 `expected_result_if_true`

例如 `claim-17b5a937aa4a97ca` 把“(Bi,Sb)2Te3 纳米片厚度下降”收窄为
“Seebeck 系数下降可能抵消低晶格热导收益”，并给出同批次纳米片按 AFM 厚度分组，联合测量
`S / σ / κ / zT` 的最小验证实验。研究者可以直接接续，而不必重新猜测模型结论的适用范围。

## 7. 可信执行与工程优化

| 组件 | 机制 | 防护对象 |
|---|---|---|
| Durable Job Control | 幂等键、租约、检查点、乐观版本、硬预算 | 重启、重复执行、超预算 |
| Operation Ledger | 稳定 operation_id、SQLite 台账、PENDING/fail-closed | 不确定付费调用、重复计费 |
| Evidence Ledger | 版本事件、fsync、SHA-256 哈希链 | 篡改、删除、重复、重排 |
| Artifact Store | 内容寻址、租户隔离、读取时重验哈希 | 跨任务污染、字节漂移 |
| Shared Retrieval Cache | 规范请求哈希、文件锁、限流与退避 | 方法间证据不公平、重复网络调用 |
| Model Transport | Claude Code / OpenCode 同一协议、工具全禁用 | 路由耦合、提示注入扩大权限 |
| Release Gate | 白名单、SBOM、密钥扫描、SHA256SUMS | 非许可文件、秘密和构建垃圾 |

当前代码为 15,563 行 `src/` Python、5,627 行测试；本地全量测试 449 passed、1 skipped。
CI 矩阵覆盖 Ubuntu / Windows / macOS 与 Python 3.11–3.13，并单独比较跨平台离线输出哈希。

## 8. 复现与边界

```bash
bash scripts/smoke_test.sh
bash scripts/reproduce_core.sh
```

正式路由：OpenCode 1.18.21，`127.0.0.1:4124`，
`zhipuai/glm-5.3-flash`，agent=`build`，请求层禁用全部工具。统计 seed=20260903。

局限：预言机不是专家金标；144/258 主张 unresolved；单一热电领域和单一正式路由；OQMD 因
网络不可达未启用。运行阶段 Agent 会话完整提交；构筑阶段跨两台机器，原始 TUI 轨迹未能完整
导出，以 Harness 声明和 git 历史替代并明确披露。
