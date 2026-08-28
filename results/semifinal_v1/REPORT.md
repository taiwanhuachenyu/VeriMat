# SSE v1 — 闭环自动评测结果

预注册：semifinal-v1-thermo｜语料：固态电解质 2000–2021，验证窗 2022–2025

| 方法 | 决策准确率 | 反证召回 | 过度宣称 | 回放精度 | Brier | tokens/有效 |
|---|---|---|---|---|---|---|
| A1-no-mcts | 0.625 | None | 0.0 | 1.0 | None | 848.6 |
| A2-no-db | 0.5 | None | 0.0 | 1.0 | None | 724.5 |
| V0-vanilla-rag | 0.0 | None | 0.0 | 1.0 | None | None |
| V1-dual-retrieval | 0.0 | None | 0.0 | 1.0 | None | None |
| V2-dual-cedg | 0.625 | None | 0.0 | 1.0 | None | 724.0 |
| V3-full | 0.5 | None | 0.0 | 1.0 | None | 8013.0 |

**already-known false-gap rate**: 0.25 (novel gaps: 8)

## 配对比较（Holm 校正）

- V2-dual-cedg__vs__V1-dual-retrieval: Δ=0.625 p=0.0638 p_holm=0.3828 (n=8)
- V3-full__vs__A1-no-mcts: Δ=-0.125 p=1.0 p_holm=1.0 (n=8)
- V3-full__vs__A2-no-db: Δ=0.0 p=1.0 p_holm=1.0 (n=8)
- V3-full__vs__V0-vanilla-rag: Δ=0.5 p=0.12519 p_holm=0.62595 (n=8)
- V3-full__vs__V1-dual-retrieval: Δ=0.5 p=0.12519 p_holm=0.62595 (n=8)
- V3-full__vs__V2-dual-cedg: Δ=-0.125 p=1.0 p_holm=1.0 (n=8)
