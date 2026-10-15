# Investment Tool

当前主线是 **A 股 Qlib 官方基准复现**。美股研究与日报冻结在 `bc5b11c`（标签 `archive/us-research-frozen-20261014`），Codex 美股日报自动化已暂停。

本项目直接调用 Qlib 官方工作流，使用其 Alpha158、LightGBM、TopkDropout、信号评价和组合回测。保留上游配置原文，只调整本机路径和计算线程，不使用原有美股自定义训练与回测。

## 安装与执行

Python 3.12；macOS 需要 LightGBM 的 OpenMP 依赖：

```sh
brew install libomp
uv sync --frozen --extra quant --extra dev --python 3.12
uv run --no-sync invest cn-data
uv run --no-sync invest cn-benchmark
```

`cn-data` 下载并校验固定的历史示例数据包。`cn-benchmark` 检查数据，然后运行官方训练、预测、回测与评价，输出 `artifacts/cn-official/runs/<run-id>/report.html`。训练过程保存在同目录 `workflow.log`；完整原生实验记录保存在 `mlruns/cn-official/`。

## 官方基准设置

| 项目 | 设置 |
|---|---|
| 市场与基准 | 历史沪深 300 成分股；SH000300 |
| 特征与模型 | Alpha158；Qlib LGBModel 官方参数 |
| 训练／验证 | 2008—2014 年／2015—2016 年 |
| 测试 | 2017-01-01 至 2020-08-01 |
| 预测目标 | 官方默认的 t+1 收盘至 t+2 收盘收益 |
| 组合 | TopkDropout：持有 50 只，排名驱动更换 5 只 |
| 模拟本金 | 1 亿元，沿用官方设置 |
| 成本 | 买入 0.05%，卖出 0.15%，最低 5 元 |
| 成交约束 | 官方 CN 交易规则、收盘成交、9.5% 涨跌停阈值 |

配置与代码固定到 Qlib 提交 `54355232463878d2eebb91fe0ee5fa7fa1f5976c`。上游配置与来源哈希位于 `config/qlib/`。

## 结果与适用范围

这条流程首先回答“能否复现官方历史实验”。示例数据截至 2020 年，不提供当前 A 股行情或今日买入名单。

上游数据有 152 条成交记录不满足 OHLC 关系，VWAP 整列缺失。复现实验保留这些原始输入，并将投资用途的数据质量状态标为未通过。报告中的运行成功与数据质量分开记录；运行成功不代表策略通过投资验证。

官方表中的“年化收益”是扣费后的**年化超额收益**，采用算术均值年化；其“最大回撤”对应超额收益曲线。报告同时列出账户和指数的复合累计收益，避免混用口径。

详细设置、差异和实测结果见 [A 股基准说明](docs/CN_BENCHMARK.md)。原美股命令保留在 [冻结的美股流程](docs/US_WORKFLOW_FROZEN.md)。Git 历史和旧结果均保留；账户资料、原始行情和运行产物不提交到 GitHub。

## 验证

```sh
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
```
