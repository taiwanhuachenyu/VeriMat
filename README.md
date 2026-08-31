# VeriMat — Evidence-Grounded and Falsification-Aware Materials Discovery

**GOAI 2026 赛道三（前沿探索 AI for Research）· 算法赛题 · 材料科学方向 · 复赛提交**

VeriMat 是一个可审计的文献驱动材料发现智能体核心：语言模型只产生候选与判断，证据定位、
时间截断、状态转换、预算与终态全部由确定性组件裁决。每个科学主张携带适用边界、支持证据、
反证证据与验证状态，每条证据可回放到语料快照中的原文并校验哈希。

- 复赛正式结果：`results/semifinal_v2/`（预注册 `semifinal-v2-thermo`，258 主张 × 6 方法）
- 实验报告：`docs/semifinal_report.md`；Prompt 逐字披露：`docs/PROMPTS.md`
- 方法学设计：`docs/semifinal_design.md`

---

## 1. 一键命令

**推荐复现环境：Linux + 本地 opencode server（代理 zhipuai/glm-5.3-flash，智谱 coding plan）。**
正式结果即在该环境产出；Windows/Claude Code 路线由同一传输协议支持（跨平台字节一致由 CI 校验）。

```bash
bash scripts/smoke_test.sh        # 冒烟测试：离线、无需任何 API key，约 1 分钟
bash scripts/reproduce_core.sh    # 复现核心结果：六方法对照 + oracle 裁定 + 发现包 + 报告
```

`reproduce_core.sh` 是幂等的：已完成的阶段自动跳过（产品文件存在即视为完成），已完成的
LLM 调用从操作缓存回放，不产生重复计费。删除某个产物文件即可强制重算该阶段。

单元测试（离线）：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q     # 430+ tests
```

## 2. 环境与依赖

- Python **3.11+**（开发环境 3.11/3.12/3.13，Linux/macOS/Windows CI 全绿）
- 安装：`python3 -m pip install -r requirements-dev.lock`（或 `requirements-runtime.lock` 仅运行时）
- 无其它系统依赖；文献解析未使用外部 OCR（全文经 Sciverse API 获取）

## 3. 数据

- 语料：Sciverse 科学智能数据库（组委会推荐源，开放获取文献，语义检索 + 全文定位）
- 发现窗（系统输入）：2000–2021，100 候选文档 → 22 篇可全文定位 → 227 片段，SHA-256 密封于
  `results/semifinal_v2/corpus_snapshot.json`
- 验证窗（自动真值）：2022–2025，经同一 API 检索，调用记录入 `retrieval_audit.jsonl`
- 数据获取：需组委会/Sciverse 注册 token（`.env` 的 `SCIVERSE_API_TOKEN`）；语料快照已随仓库
  提交，评分与统计阶段完全离线
- 许可：语料为开放获取文献，快照仅存定位符与哈希

## 4. 模型与配置

| 项 | 值 |
|---|---|
| 模型 | zhipuai/glm-5.3-flash（智谱 coding plan） |
| 端点 | `https://open.bigmodel.cn/api/coding/paas/v4`，经本地 OpenCode server (127.0.0.1:4124) 代理 |
| 接入 | OpenCode 1.18.21，benchmark agent，**全部工具禁用**，json_schema structured output（服务端 retryCount=2） |
| 采样参数 | 未设置 temperature/top_p（provider 默认） |
| 随机种子 | 统计置换 seed=20260903；管线其余部分确定性（内容寻址 id） |
| 预算 | 每方法 3M tokens 硬上限（`BudgetedTransport` 传输层强制）；实际总消耗 1.28M |
| 环境变量模板 | `config/verimat.env.example`（不含任何密钥） |

Prompt 逐字披露：`docs/PROMPTS.md`（8 类调用的系统提示词、载荷结构、响应 schema）。

## 5. 算力与成本

单机 CPU 即可（LLM 推理在云端）；全部正式 run 合计 **1.28M tokens**（智谱 coding plan
订阅额度内），约 8 小时挂钟时间（含自动重试）。评分与统计离线秒级。

## 6. 冒烟测试与核心复现

见第 1 节。`smoke_test.sh` 验证安装、MCTS 示例与评分器确定性（零网络）；`reproduce_core.sh`
从语料快照出发重建全部结果。评审核验建议路径：

1. `bash scripts/smoke_test.sh`（离线，快速确认环境）
2. 阅读 `results/semifinal_v2/REPORT.md` 与 `docs/semifinal_report.md`
3. 抽查任意主张的证据链：`claims.jsonl` → `relations.jsonl` → `corpus_snapshot.json` 逐级哈希定位
4. 需要全量重算时再运行 `reproduce_core.sh`（需要 Sciverse token 与模型端点）

## 7. 已知限制与披露

- 验证窗预言机由模型裁定（有逐字引文门约束），判定明细全文随仓库公开，可独立抽查
- OQMD 成分稳定性校验因网络不可达未启用（DB 消融记 uncovered，诚实披露）
- 4 个片段因反复超出 600s 模型超时被跳过（`skip_passages.json` 留档）
- 正式结果为 **v2 单次完整运行**；此前的 v1（120 主张）为开发性运行，结论方向一致，
  两次运行均随仓库公开，无筛选
- Agent 使用声明：系统构筑与实验运行由 **opencode**（Linux，GLM-5.3-Flash）驱动；
  早期 Windows 开发使用 **Claude Code**（Opus 5）。会话轨迹见 `submission/agent_traces/`
  （构筑与运行分目录存放）

## 8. 许可证

MIT License。复赛提交对应 commit：见 `submission/SUBMISSION.md` 记录的 tag。
