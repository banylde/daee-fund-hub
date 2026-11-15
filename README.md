# 大鹅基金模拟盘总览

**模拟盘，非投资建议。** 虚拟资金，不接券商，不动真钱。仓库里没有密钥、账户或真实持仓。

把风林火山四个公开看板合成一个总账户：

| 子组合 | 看板 | 目标权重 | 允许区间 |
|---|---|---:|---:|
| 山部 五绝归山 | [daee-shan-equity](https://banylde.github.io/daee-shan-equity/) | 30% | 25–35% |
| 火部 四王盗火 | [daee-fire-theme](https://banylde.github.io/daee-fire-theme/) | 30% | 25–35% |
| 风部 八友御风 | [daee-feng-etf](https://banylde.github.io/daee-feng-etf/) | 20% | 15–25% |
| 林部 六仙出林 | [daee-lin-fund](https://banylde.github.io/daee-lin-fund/) | 20% | 15–25% |

总虚拟资金 1,000 万元，纯权益，不预留战略现金。火部按 300 万元记账；若其 `paper.json` 仍是 100 万元，本仓库不按比例放大那份持仓，而是按正式名单自行计算。

## 成交口径

- 收盘出信号，次日开盘成交。场外基金没有开盘价，按次日公布的单位净值成交。
- 佣金单边万分之三，滑点 0.1%（买在成交价之上、卖在成交价之下）。
- A 股股票卖出计 0.05% 印花税；ETF、场外基金不收印花税。港股未单列香港印花税，见数据缺口说明。
- A 股、ETF、场外基金：买入当日不可卖，下一上交所交易日可卖，不等满 24 小时。港股当日可卖。
- 涨停开盘买不进、跌停开盘卖不出，顺延到之后的交易日。
- 卖出资金当日可用。不模拟融资。

林部、山部没有现成的子组合净值。从建账日起，用当时的正式持仓加公开基金净值或股价逐日计算，不倒推建账日之前的业绩。

## 再平衡与风控

- 季末最后一个交易日收盘检查，次日开盘拉回目标权重。
- 任一部权重超出允许区间，当天盘后发信号，次日开盘拉回目标。
- 回撤降仓档位写在 `config/rules.json` 的 `drawdown_delever`，默认 `"enabled": false`。批准前不会降仓。

## 配置

主配置是 `config/rules.json`。

- `start_date` 为 `null` 时视为未建账：脚本不产生任何成交和净值，页面图表位置显示「模拟尚未开始」和目标配比，不画虚构曲线。
- 建账时把 `start_date` 写成交易日（`YYYY-MM-DD`）。若落在休市日，则从其后第一个交易日起算。

## 本地运行

```bash
python3 scripts/self_test.py    # 只用 tests/ 里的合成夹具
python3 scripts/build.py        # 先自测，再按配置计算并写 data/
python3 -m http.server 8080     # 浏览 index.html（不要直接用 file:// 拉 JSON）
```

只需 Python 3 标准库，没有第三方依赖。自测数据全部放在 `tests/`，不会进入 `data/` 或页面。

`data/summary.json` 供页面展示：总净值、各部净值与权重、贡献、当前回撤和最大回撤、基准、最近操作日志。`data/changes.json` 记录当日调入调出、再平衡和风控触发，供盘后提醒例行做一次比对；无变动时 `has_changes` 为 false。

交易日 18:00（北京时间）由 GitHub Actions 运行 `scripts/build.py` 并提交 `data/`。工作流只用仓库默认权限，不配置也不读取任何 secret。

## 基准

- 主基准：沪深300全收益。若公开接口取不到全收益，会退回到沪深300价格指数，并在 `gaps` 里写明。
- 辅基准：中证偏股型基金指数。

二者都从建账日定基为 1。

## 图表

`vendor/lightweight-charts/` 是 TradingView Lightweight Charts™ 5.3.0 官方独立版的原样副本（Apache-2.0），带 LICENSE 和 NOTICE，页面用相对路径加载，不走 CDN。脚本加载失败时，回退到页面内置的 SVG 曲线。

## 不是什么

这不是实盘、不是投顾，也不会自动下单。四个子组合的选股规则仍以各自看板为准。本仓库只做总账户记账、再平衡和展示。
