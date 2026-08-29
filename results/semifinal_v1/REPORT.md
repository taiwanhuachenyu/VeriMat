# 热电构效 v1 — 闭环自动评测结果

预注册：semifinal-v1-thermo｜语料：热电材料 2000–2021，验证窗 2022–2025

| 方法 | 决策准确率 | 反证召回 | 过度宣称 | 回放精度 | Brier | tokens/有效 |
|---|---|---|---|---|---|---|
| A1-no-mcts | 0.4875 | 0.2353 | 0.0583 | 1.0 | 0.4771 | 3332.7 |
| A2-no-db | 0.4625 | 0.2941 | 0.0333 | 1.0 | 0.4869 | 2072.7 |
| V0-vanilla-rag | 0.1417 | 0.0 | 0.1417 | 1.0 | 0.34 | 0.0 |
| V1-dual-retrieval | 0.1417 | 0.0588 | 0.1333 | 1.0 | 0.4869 | 10999.5 |
| V2-dual-cedg | 0.4417 | 0.2353 | 0.0417 | 1.0 | 0.4935 | 1916.9 |
| V3-full | 0.4625 | 0.1176 | 0.0667 | 1.0 | 0.4869 | 10470.6 |

**already-known false-gap rate**: 0.25 (novel gaps: 8)

## 配对比较（Holm 校正）

- V2-dual-cedg__vs__V1-dual-retrieval: Δ=0.3 p=5e-05 p_holm=0.0003 (n=120)
- V3-full__vs__A1-no-mcts: Δ=-0.025 p=0.61712 p_holm=1.0 (n=120)
- V3-full__vs__A2-no-db: Δ=0.0 p=1.0 p_holm=1.0 (n=120)
- V3-full__vs__V0-vanilla-rag: Δ=0.3208 p=5e-05 p_holm=0.0003 (n=120)
- V3-full__vs__V1-dual-retrieval: Δ=0.3208 p=5e-05 p_holm=0.0003 (n=120)
- V3-full__vs__V2-dual-cedg: Δ=0.0208 p=0.63557 p_holm=1.0 (n=120)
