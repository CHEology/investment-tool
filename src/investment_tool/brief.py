"""Render Codex's source-backed analysis alongside the computed daily facts."""

from decimal import Decimal
from html import escape
from pathlib import Path

import pandas as pd

from .data import DataError
from .storage import digest, now_utc, read_json, write_json


def table(headers, rows):
    head = "".join(f"<th>{escape(str(value))}</th>" for value in headers)
    body = "".join("<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows)
    return (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def percent(value):
    return f"{value:+.2%}"


def portfolio_html(portfolio):
    summary = portfolio["summary"]
    currency = summary["currency"]
    net = Decimal(summary["net_liquidation"])
    cash = Decimal(summary["total_cash_value"])
    cash_share = f"{cash / net:.1%}" if net else "—"
    rows = []
    position_total = Decimal(0)
    same_currency = all(p["currency"] == currency for p in portfolio["positions"])
    for position in portfolio["positions"]:
        value = Decimal(position["market_value"])
        weight = f"{value / net:.1%}" if position["currency"] == currency and net else "—"
        position_total += value
        rows.append(
            [
                escape(position["contract_description"]),
                escape(position["position"]),
                f"{Decimal(position['average_price']):,.2f}",
                f"{Decimal(position['market_price']):,.2f}",
                f"{value:,.2f} {escape(position['currency'])}",
                weight,
                f"{Decimal(position['unrealized_pnl']):+,.2f}",
            ]
        )
    timestamp = pd.Timestamp(portfolio["retrieved_at"]).tz_convert("America/New_York")
    result = (
        '<div class="metrics">'
        f"<div>账户净值<strong>{net:,.2f} {escape(currency)}</strong></div>"
        f"<div>现金<strong>{cash:,.2f} {escape(currency)}</strong></div>"
        f"<div>现金占比<strong>{cash_share}</strong></div></div>"
        f"<p>IBKR 读取时间：{timestamp:%Y-%m-%d %H:%M}（纽约）。"
        "下表是券商返回的估值；行情表另用当日收盘价。</p>"
    )
    result += table(
        ["持仓", "数量", "平均成本", "券商报价", "持仓市值", "净值占比约", "浮动盈亏"], rows
    )
    if same_currency:
        gross = Decimal(summary["gross_position_value"])
        result += (
            f"<p>逐项持仓市值合计 {position_total:,.2f}，账户汇总持仓市值 {gross:,.2f}；"
            f"差额 {position_total - gross:+,.2f} {escape(currency)}。"
            "接口未返回逐项报价时间，仓位比例按账户净值估算；这里不将券商日盈亏当作收盘收益。</p>"
        )
    return result


def render_brief(day: Path):
    manifest = read_json(day / "manifest.json")
    if digest(day / "packet.json") != manifest["packet_sha256"]:
        raise DataError("Daily packet hash mismatch")
    packet = read_json(day / "packet.json")
    analysis = read_json(day / "analysis.json")
    if analysis["as_of"] != packet["as_of"]:
        raise DataError("Research text and market session differ")
    sources = analysis["sources"]
    for source in sources.values():
        if digest(day / source["evidence"]) != source["sha256"]:
            raise DataError("Research evidence hash mismatch")

    def links(ids):
        return " · ".join(
            f'<a href="{escape(sources[key]["url"], quote=True)}">'
            f"{escape(sources[key]['title'])}</a>"
            for key in ids
        )

    def paragraphs(items):
        return "".join(f"<p>{escape(item)}</p>" for item in items)

    parts = [
        f'<p class="date">美股投资日报 · {escape(packet["as_of"])}</p>',
        f"<h1>{escape(analysis['headline'])}</h1>",
        '<ul class="takeaways">'
        + "".join(f"<li>{escape(x)}</li>" for x in analysis["takeaways"])
        + "</ul>",
        "<h2>你的组合</h2>",
    ]
    portfolio_path = day / "portfolio.json"
    if portfolio_path.exists():
        portfolio = read_json(portfolio_path)
        for source in portfolio["sources"]:
            if digest(day / source["raw_path"]) != source["sha256"]:
                raise DataError("Broker evidence hash mismatch")
        parts.append(portfolio_html(portfolio))
    else:
        parts.append("<p>本次未取得 IBKR 持仓，账户判断暂缺。</p>")
    parts.append(paragraphs(analysis["portfolio"]["paragraphs"]))
    parts.append(f"<p>{links(analysis['portfolio']['source_ids'])}</p>")
    parts.append("<h2>市场收盘</h2>")
    rows = []
    for symbol in dict.fromkeys(packet["market_symbols"] + packet["holding_symbols"]):
        price = packet["prices"][symbol]
        rows.append(
            [
                f'<a href="https://finance.yahoo.com/quote/{escape(symbol)}/history/">{escape(symbol)}</a>',
                f"{price['close']:,.2f}",
                *[percent(price[f"return_{n}d"]) for n in (1, 5, 20)],
            ]
        )
    parts.append(table(["标的", "收盘价 USD", "1 日", "5 日", "20 日"], rows))
    breadth = packet["breadth"]
    parts.append(
        f"<p>研究样本中 {breadth['advancing']}/{breadth['universe']} 只上涨，"
        f"{breadth['above_ma200']}/{breadth['universe']} 只在 200 日均线上方。"
        "涨跌幅用 Yahoo 复权收盘价计算，广度只代表该固定样本。</p>"
    )
    parts.append(paragraphs(analysis["market"]["paragraphs"]))
    parts.append(f"<p>{links(analysis['market']['source_ids'])}</p>")
    parts.append("<h2>今天值得研究的公司</h2>")
    for idea in analysis["ideas"]:
        parts.extend(
            [
                f"<article><h3>{escape(idea['symbol'])} · {escape(idea['decision'])}</h3>",
                f"<p><strong>支持理由：</strong>{escape(idea['case'])}</p>",
                f"<p><strong>主要反证：</strong>{escape(idea['challenge'])}</p>",
                f"<p><strong>下一步：</strong>{escape(idea['next_check'])}</p>",
                f"<p>{links(idea['source_ids'])}</p></article>",
            ]
        )
    parts.append("<h2>接下来关注什么</h2>")
    parts.append(
        table(
            ["纽约时间", "事件", "对你的意义"],
            [
                [
                    escape(e["when"]),
                    escape(e["event"]) + "<br>" + links(e["source_ids"]),
                    escape(e["implication"]),
                ]
                for e in analysis["events"]
            ],
        )
    )
    parts.append(
        "<h2>今日行动</h2><ol>"
        + "".join(f"<li>{escape(action)}</li>" for action in analysis["actions"])
        + "</ol>"
    )
    parts.append("<h2>量化筛选依据</h2>")
    parts.append(paragraphs(analysis["model_assessment"]))
    selected = [c for c in packet["candidates"] if c["selected"]]
    parts.append(
        table(
            ["名次", "标的", "收盘价 USD", "1 日", "20 日", "上次名次"],
            [
                [
                    str(c["rank"]),
                    escape(c["symbol"]),
                    f"{packet['prices'][c['symbol']]['close']:,.2f}",
                    percent(packet["prices"][c["symbol"]]["return_1d"]),
                    percent(packet["prices"][c["symbol"]]["return_20d"]),
                    str(c["previous_rank"]) if c["previous_rank"] is not None else "首次记录",
                ]
                for c in selected
            ],
        )
    )
    html = (
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>美股投资日报</title><style>
:root{color-scheme:light}body{margin:0;background:#eef2f4;color:#172c3c;
font:17px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
main{max-width:1040px;margin:36px auto;padding:48px 56px;background:white;border-radius:18px}
h1{font-size:36px;line-height:1.35;letter-spacing:-1px;margin:12px 0 26px}
h2{font-size:25px;margin:42px 0 16px;border-top:2px solid #dce5e9;padding-top:24px}
h3{font-size:21px;margin:0 0 12px}.date{color:#215f70;font-weight:600}
a{color:#176778;text-underline-offset:3px}li{margin:9px 0}
.takeaways{background:#edf5f5;padding:20px 24px 20px 46px;border-radius:12px}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.metrics>div{background:#f2f6f7;border-radius:10px;padding:16px}
strong{font-weight:650}.metrics strong{display:block;font-size:24px;margin-top:8px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:16px}
th,td{text-align:left;padding:12px 10px;border-bottom:1px solid #dce5e9;vertical-align:top}
th{background:#f1f5f6;white-space:nowrap}article{padding:22px 24px;margin:18px 0;
border-left:4px solid #3f7e89;background:#f6f9fa;border-radius:0 10px 10px 0}
p{margin:14px 0}@media(max-width:700px){main{margin:0;padding:24px 20px;border-radius:0}
h1{font-size:29px}.metrics{grid-template-columns:1fr}.metrics strong{font-size:23px}}
</style><main>"""
        + "\n".join(parts)
        + "</main></html>"
    )
    report = day / "report.html"
    report.write_text(html)
    files = ["packet.json", "analysis.json", "report.html"]
    if portfolio_path.exists():
        files.append("portfolio.json")
    for optional in ("calculations.json", "validation-reference.json"):
        if (day / optional).exists():
            files.append(optional)
    manifest.update(
        status="completed",
        completed_at=now_utc(),
        artifacts={name: digest(day / name) for name in files},
    )
    write_json(day / "follow-up.json", {"as_of": packet["as_of"], "items": analysis["follow_up"]})
    manifest["artifacts"]["follow-up.json"] = digest(day / "follow-up.json")
    write_json(day / "manifest.json", manifest)
    write_json(
        day.parent.parent / "latest.json", {"as_of": packet["as_of"], "day": str(day.resolve())}
    )
    return report
