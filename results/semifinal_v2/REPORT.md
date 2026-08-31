# 热电构效 v1 — 闭环自动评测结果

预注册：semifinal-v2-thermo｜语料：热电材料 2000–2021，验证窗 2022–2025

| 方法 | 决策准确率 | 反证召回 | 过度宣称 | 回放精度 | Brier | tokens/有效 |
|---|---|---|---|---|---|---|
| A1-no-mcts | 0.4612 | 0.3415 | 0.0388 | 1.0 | 0.474 | 2392.2 |
| A2-no-db | 0.4477 | 0.2683 | 0.0465 | 1.0 | 0.4895 | 1015.5 |
| V0-vanilla-rag | 0.1473 | 0.0 | 0.1589 | 1.0 | 0.3514 | 0.0 |
| V1-dual-retrieval | 0.1589 | 0.122 | 0.1395 | 1.0 | 0.4811 | 7662.8 |
| V2-dual-cedg | 0.4826 | 0.3902 | 0.0426 | 1.0 | 0.4586 | 1746.7 |
| V3-full | 0.4612 | 0.3171 | 0.0465 | 1.0 | 0.4783 | 3327.2 |

**already-known false-gap rate**: 0.1667 (novel gaps: 6)

## 配对比较（Holm 校正）

- V2-dual-cedg__vs__V1-dual-retrieval: Δ=0.3236 p=5e-05 p_holm=0.0003 (n=258)
- V3-full__vs__A1-no-mcts: Δ=0.0 p=1.0 p_holm=1.0 (n=258)
- V3-full__vs__A2-no-db: Δ=0.0136 p=0.63077 p_holm=1.0 (n=258)
- V3-full__vs__V0-vanilla-rag: Δ=0.314 p=5e-05 p_holm=0.0003 (n=258)
- V3-full__vs__V1-dual-retrieval: Δ=0.3023 p=5e-05 p_holm=0.0003 (n=258)
- V3-full__vs__V2-dual-cedg: Δ=-0.0213 p=0.43558 p_holm=1.0 (n=258)
