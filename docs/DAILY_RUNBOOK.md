# 美股日报运行流程

**当前已冻结。** 用户已将优先级切换到 A 股官方基准复现；美股日报自动化处于 `PAUSED`。下列流程保留供历史查阅，恢复需用户明确要求。

这是 Codex 的运行说明。程序负责可重复的行情、特征、排序和计算；Codex 负责读取 IBKR、查证财报和事件、形成中文判断。输出保存在本地 `artifacts/daily-system/`，不提交账户资料或日报到 GitHub。

## 每次执行

1. 在本仓库读取 `investment-research-loop` 技能。用 `expected_session()` 判断最新完成且已过两小时发布缓冲的 XNYS 交易日。查看该日 `manifest.json`：如果已经 `completed` 且文件校验通过，本次结束；休市日不重复出旧日报。`awaiting_research` 表示需要继续完成研究，不表示日报已完成。
2. 使用已连接 IBKR 的 `get_account_positions`、`get_account_summary`、`get_account_balances` 只读工具。将工具的完整原始响应保存到当日 `evidence/ibkr-*.json`，记录真实读取 UTC 时间及 SHA256。把返回值整理为 `portfolio.json`，数值保留十进制字符串。连接失败时记录原因并继续市场研究，明确持仓未更新；不将旧快照标成当前持仓，不需要用户重输已可读取的数据。没有交易授权。
3. 从持仓中取得可明确识别的美股或美国 ETF 代码，传入下列入口；期权、非美元资产或不明确代码独立列示，不能当普通美股送入价格模型。

```sh
.venv/bin/invest --root artifacts/daily-system daily --holdings <逗号分隔的代码>
```

无持仓时省略 `--holdings`。程序按股票配置训练，只为额外持仓取行情。首跑训练模型，此后复用，满 21 个交易日或研究配置变更才重训。程序验证完整行情并保留原始响应。失败时报告具体缺口，不能换用过时价格冒充成功。

4. 阅读返回目录的 `packet.json` 和前一个已完成日报的 `analysis.json`、`follow-up.json`。比较持仓数量、仓位、候选进入退出与先前判断。第一次只建立基线；模型变化时说明排名变化包含重训影响。不同股票池的旧回测不能替当前模型背书。
5. 对真实持仓、重要宏观事件和约 3 个最有研究价值的候选查一手资料，优先公司 IR、SEC、基金发行人、BLS。保留网页工具原始返回或实际 HTTP 原文，不把自己整理的摘录当原始证据；记录 URL、读取时间和文件哈希。核对发布日与财务期。财报、估值、价格和判断分别写清楚，计算保存到 `calculations.json`。模型很弱时直接降低排名权重，不强行给买入名单。
6. 写 `analysis.json`，调用下列命令完成报告。检查主要数字、来源链接、HTML 表格和段落。`brief` 校验行情包与引用证据，写 `report.html`、`follow-up.json`、完成状态和 `latest.json`。

```sh
.venv/bin/invest brief artifacts/daily-system/days/<交易日>
```

7. 在本聊天交付简洁中文摘要与报告绝对路径链接：市场与持仓变化、值得跟进的机会、当前行动和下一观察条件。对每个新的交易日给一份日报；相同交易日且没有有意义的新情况保持安静。遇到运行失败或需要用户操作时说明具体问题。不要自动改写代码、调参、交易、推送 Git 或向其他人发送消息。

## 本地文件格式

`portfolio.json` 使用 IBKR 返回的原字段；`positions` 是数组，`summary` 是对象，`balances` 是数组：

```json
{
  "retrieved_at": "实际读取时间，UTC ISO8601",
  "positions": [],
  "summary": {},
  "balances": [],
  "sources": [
    {"provider": "IBKR", "dataset": "positions", "raw_path": "evidence/ibkr-positions.json", "retrieved_at": "实际读取时间", "sha256": "原始文件SHA256"}
  ]
}
```

券商汇总、逐项市值和交易所收盘价可能来自不同时间，分别保留；不强行对齐，不由缺少交易流水的两个净值推断投资回报。只在相同币种下计算净值占比。持仓目标、资金用途未记录时，不擅自设置用户的长期配置。

`analysis.json` 格式如下。每个候选应包括支持证据、主要反证、下一观察条件，避免只重复技术排名。所有文章内容使用正常字号，不加页脚、小字免责声明或制作标记。

```json
{
  "as_of": "YYYY-MM-DD",
  "headline": "直接给出今天最重要的判断",
  "takeaways": ["2—3 条关键结论"],
  "portfolio": {"paragraphs": ["组合判断"], "source_ids": ["source1"]},
  "market": {"paragraphs": ["市场事实与意义"], "source_ids": ["source1"]},
  "ideas": [{"symbol": "代码", "decision": "研究结论", "case": "支持理由", "challenge": "反证", "next_check": "下一步", "source_ids": ["source1"]}],
  "events": [{"when": "纽约时间", "event": "事件", "implication": "意义", "source_ids": ["source1"]}],
  "actions": ["今天应做什么及其条件"],
  "model_assessment": ["根据 packet.json 说明模型实际区分能力、适用范围、名单变化"],
  "follow_up": [{"symbol": "代码", "thesis": "可证伪的判断", "decision": "当前决定", "next_check": "待核对事实", "check_on": "YYYY-MM-DD"}],
  "sources": {"source1": {"title": "来源标题", "url": "原始链接", "retrieved_at": "实际读取UTC时间", "evidence": "evidence/source1.json", "sha256": "文件SHA256"}}
}
```

## 定时安排

Codex 当前聊天的定时唤醒：纽约时间周一至周五 18:30。应用负责调度，本仓库不安装另一个调度器。运行时本机需要开机且 Codex 可执行任务；券商连接需保持有效。交易所休市通过程序日历跳过。

首次上线先完整手动执行、检查真实日报，再启用自动化。以后按新完成的交易日继续同一个研究记录。
