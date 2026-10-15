# Investment Tool

美股投资研究助手：Qlib Alpha158 提取特征，LightGBM 生成约 20 个交易日的相对排序，滚动回测比较模型、动量、等权股票池和 SPY，再输出待基本面复核的候选名单。

旧实现已整体替换，Git 历史完整保留。恢复点：`archive/pre-qlib-rebuild-20261014`。

## 每日投资研究

日常入口使用 48 只跨行业美股作为固定研究样本，并跟踪 SPY、QQQ、IWM、TLT。Codex 从已连接 IBKR 读取真实持仓，将其代码作为额外行情输入：

```sh
uv run --no-sync invest --root artifacts/daily-system daily --holdings <持仓代码，逗号分隔>
```

程序输出当天的 `packet.json`：收盘表现、样本广度、Qlib 排名、候选变化和模型诊断。模型通常每 21 个交易日重训，日常复用；同一交易日重复执行会返回已有记录。

Codex 按 [日报运行说明](docs/DAILY_RUNBOOK.md) 读取持仓、核对官方财报与事件、保存证据并写 `analysis.json`，再完成报告：

```sh
uv run --no-sync invest brief artifacts/daily-system/days/<交易日>
```

结果是包含组合复核、市场变化、候选支持与反证、事件日历和行动条件的 `report.html`。`follow-up.json` 保存待验证判断，下一次日报继续复核。`awaiting_research` 表示只完成量化计算，`completed` 才表示日报完成。

定时运行由 Codex 当前聊天唤醒并执行完整流程；不需要另一个模型 API 或常驻服务。账户资料与日报仅保存在被 Git 忽略的 `artifacts/` 中。量化排序用于研究优先级，实际效度由验证结果决定。

## 开始使用

Python 3.12；macOS 先安装 LightGBM 的 OpenMP 依赖：

```sh
brew install libomp
uv sync --frozen --extra quant --extra dev --python 3.12
uv run --no-sync invest demo
```

`demo` 用确定性的合成行情运行真正的 Qlib 和 LightGBM，输出目录中包含 `report.html`、模型、预测、每日净值和运行记录。合成数据用于检查程序。

## 真实行情研究

下面的 12 只股票是工程验证样本；`config/us-research.toml` 因此只选 3 只（`top_k` 不得超过候选池的 30%）。`--end` 使用已经收盘且经过两小时发布缓冲的美股交易日。

```sh
uv run --no-sync invest fetch \
  --symbols SPY,AAPL,MSFT,NVDA,AMZN,GOOGL,META,JPM,V,UNH,XOM,COST,HD \
  --start 2018-01-01 --end 2026-10-14

uv run --no-sync invest validate artifacts/snapshots/<snapshot-id>
uv run --no-sync invest research artifacts/snapshots/<snapshot-id> \
  --config config/us-research.toml

uv run --no-sync invest observe artifacts/runs/<run-id>
```

`fetch` 返回数据目录，`research` 返回结果目录。打开其中的 `report.html` 查看结果。`observe` 把最新完整交易日的候选变化写入 `artifacts/paper/journal.json`，同一天重复执行不会重复记录。不同股票池或配置使用不同的 `--root`，例如 `invest --root artifacts/experiment-b research ...`。

也可导入自己的数据：

```sh
uv run --no-sync invest import-csv /absolute/path/prices.csv
```

输入列：

```text
date,symbol,exchange,currency,open,high,low,close,adj_close,volume
```

日期为 `YYYY-MM-DD`；交易所支持 `XNYS`、`XNAS`、`ARCX`，币种为 USD。原始 OHLC 与复权收盘价分别提供；数值以十进制文本存储。当前版本要求每只股票覆盖同一组交易日，缺失行情直接报错，便于发现停牌、上市时间和数据覆盖问题。

## 已实现

- 免费 Yahoo 日线抓取、原始响应保存、CSV 导入和数据质量检查。
- 固定 Qlib 0.9.8 源码提交及依赖锁文件。
- Alpha158、LightGBM、20 日标签和滚动训练；训练与验证、验证与测试之间各隔离 21 个交易日。
- 信号次日收盘执行，每 20 个交易日调整组合；每日净值、仓位漂移、双边成本、最终清仓成本和加倍成本情景。
- 与等权股票池、60 日动量、12-1 月动量和 SPY 的同区间对比；相对等权的主动收益、跟踪误差和信息比率。
- 按窗口和年份的 Rank IC、不重叠日期 IC t 值、未学到信号的窗口标记；同分平分仓位；`top_k` 超过候选池 30% 时直接报错。
- 候选名单、基本面复核资料包和本地观察日志。

## 当前范围

使用当前选择的股票回测历史，会受到幸存者偏差和选择偏差影响。当前结果对应所选样本，不能据此判断整个 S&P 500 的选股效果。Yahoo 的复权历史可能修订；原始响应和每次运行均保存，方便复现同一份输入。

日线 VWAP 缺失，Alpha158 中对应特征保持缺失值，由 LightGBM 处理。模拟允许碎股，现金收益设为零；尚未加入税费、成交容量、停牌成交处理和行业权重约束。`max_weight` 是调仓目标上限，持有期间权重随价格漂移。

Codex 已串联 IBKR 只读持仓、官方来源复核、中文日报和当前聊天的定时运行。`daily` 生成资料包，`brief` 将经复核的判断与计算结果汇总；仅运行 `research` 仍然输出量化研究报告。Telegram 发送和 A 股尚未接入，旧定时任务已经停用。

具体评估和后续顺序见 [方案评估](docs/ASSESSMENT.md)，计算约定见 [设计](docs/DESIGN.md)。

## 验证

```sh
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
```

测试覆盖数据质量、证据完整性、标签边界、未来数据不影响历史特征、次日执行、交易成本及真实 Qlib 集成。CI 使用同一套依赖和检查。

代码组织保持简单：`data` / `providers` 管输入，`qlib_engine` 管特征和训练，`backtest` 管收益计算，`pipeline` 串联回测，`daily` 更新每日资料，`brief` 输出日报，`watch` 记录独立回测的候选变化。没有旧代码兼容层。
