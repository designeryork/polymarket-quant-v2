# 纸盘 v2（paper_v2）

这是 **Polymarket 纸盘 v2**，一个与 v1 并行对照的实验仓。

- **v1 基线**：`designeryork/polymarket-quant`，tag `paper-v1-freeze-20261105`（commit `8de0cb5`）
- **v2 基线**：本仓 tag `v1-baseline`（同一个 commit）
- **设计依据**：`Docs/047`（诊断）、`Docs/048`（建议）、`Docs/049`（To-Do List）

## 当前状态

**Phase 1（本地脚手架）**：

- 起始本金 **$100**（纸面虚拟本金，不连接钱包）；
- 账本、心跳、面板、日志、锁全部隔离在独立目录；
- 每条 EVAL / SIGNAL / RESOLUTION / 平仓 / 审计记录都带：
  - `variant: paper_v2`
  - `strategy_version: v2.0-measure`
- 面板标题「纸盘 v2 · v2.0-measure」；
- **云端 workflow 尚未启用**：`deploy/polymarket_quant_v2.yml.draft` 只是草稿，Phase 6 再移入 `.github/workflows/`。

## 本地运行

```bash
cd "D:/2026_APPs by AI/049_polymarket_paper_v2"

# 单轮
python quant_engine.py --paper

# 常驻，每 60 秒一轮
python quant_engine.py --loop --interval 60 --paper
```

`.env`（不提交）：

```env
PAPER='true'
ALLOW_LIVE='false'
ALLOW_NEW_ENTRIES='true'
MAX_CAPITAL_USDC='100'
VARIANT='paper_v2'
STRATEGY_VERSION='v2.0-measure'
```

**禁止**在 `.env` 里填实盘私钥、API passphrase、钱包地址。纸面模式不签名、不上链。

## 阶段计划

| 阶段 | 版本 | 唯一改动 | 状态 |
|---|---|---|---|
| Phase 0 | — | v1 冻结与备份 | ✅ |
| Phase 1 | `v2.0-measure` | 测量脚手架，交易逻辑同 v1 | 🟡 本地完成，云端未启用 |
| Phase 2 | `v2.0-measure` | 结算反查与每日指标 | ⬜ |
| Phase 3 | `v2.1-exit` | 出场改回 logit 单位 | ⬜ |
| Phase 4 | `v2.2-entry` | 经验条件概率入场 | ⬜ |
| Phase 5 | `v2.3-sizing` | 删除凯利抬升 | ⬜ |
| Phase 6 | — | v1/v2 云端并行对照 | ⬜ |

## 安全

- `PAPER=true` + `ALLOW_LIVE=false` 双保险；
- 无实盘私钥；
- 不复用 v1 的账本、心跳、面板、锁；
- 不与 v1 合并代码，直到 Phase 7 决策通过。
