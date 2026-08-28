"""候选卡片 (candidate cards): Chinese-primary rendering + content-addressed
freezing. Freezing starts the forward-validation clock (DESIGN 15/S1)."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import utc_now

CARDS_DIR = DEFAULT_DATA_DIR / "research" / "cards"

_STATE_ZH = {
    "PROFILED_EXCESS": "候选（超额定价）",
    "NOT_ADMITTED_WITHIN_BRACKET": "未入围（跌幅在保守损害区间内）",
    "NOT_ADMITTED_PRICED_LESS": "未入围（跌幅小于保守损害下限）",
    "NOT_ADMITTED_EXCESS": "未入围（超额但低于准入比例）",
    "PENDING_ATTRIBUTION": "待归因（未找到经核实的原因）",
    "AWAITING_PARAMS": "待损害参数（C0 需人工提供带来源的假设）",
    "AWAITING_CONTENT_REVIEW": "待内容审阅（公告需人工确认负面性与重要性）",
    "ATTRIBUTION_FETCH_DEGRADED": "归因数据源降级（待重试）",
}


def render_card_zh(conn: sqlite3.Connection, candidate_row) -> str:
    p = json.loads(candidate_row["profile_json"])
    company = conn.execute(
        "SELECT c.name_zh, c.name_en, l.ticker, l.exchange, l.board FROM company c"
        " JOIN listing l ON l.company_id = c.company_id WHERE c.company_id=?",
        (candidate_row["company_id"],),
    ).fetchone()
    name = company["name_zh"] or company["name_en"] or company["ticker"]
    lines = [
        f"# 候选卡片：{name}（{company['ticker']}.{company['exchange']}）",
        "",
        f"- **通道**: Lane A（负面冲击过度反应） · **状态**: "
        f"{_STATE_ZH.get(candidate_row['state'], candidate_row['state'])}",
        f"- **事件起点 t0**: {p.get('t0')} · **窗口状态**: {p.get('window_state')}",
        f"- **同业调整累计超额收益 CAR[0,+3]**: {_pct(p.get('car_peer'))}"
        f" · **市场模型 CAR**: {_pct(p.get('car_mm'))}",
        f"- **残差波动 σ**: {_pct(p.get('sigma'))} · **流动性**: {p['liquidity']['class']}"
        f"（60日中位成交额 ≈ ${p['liquidity']['adv60_usd']:,.0f}）"
        if p.get("liquidity", {}).get("adv60_usd") else
        f"- **残差波动 σ**: {_pct(p.get('sigma'))}",
        "",
        "## 事件时间线（公告，均为发行人一手来源）",
        "（⚠=硬性负面 ⊙=需内容审阅 +=正面 ·=中性；⌛=晚于价格事件起点，不可作为归因原因）",
    ]
    _mark = {"HARD_NEGATIVE": "⚠", "CONTENT_REVIEW_REQUIRED": "⊙", "POSITIVE": "+"}
    for a in p.get("announcements", []):
        flag = _mark.get(a.get("relevance"), "·")
        late = "" if a.get("eligible_for_t0", True) else " ⌛"
        lines.append(
            f"- {flag}{late} {a.get('published') or '?'} [{a.get('type')}] {a.get('title')}"
        )
    attr = p.get("attribution")
    if attr:
        lines.append("")
        lines.append(
            f"归因状态：合格硬性负面 {attr.get('hard_negative_eligible', 0)} · "
            f"合格待审阅 {attr.get('content_review_eligible', 0)} · "
            f"时间上不合格 {attr.get('relevant_but_temporally_ineligible', 0)}"
        )
    dmg = p.get("damage")
    if dmg and "classification" in dmg:
        lines += [
            "",
            "## 保守损害区间 vs 市场重定价（冻结 v0 规则）",
            f"- 模板: {dmg['template']}",
            f"- 异常市值变化 |ΔMcap|: {_yi(dmg['dmcap_abnormal'])}"
            f"（t0前市值 {_yi(dmg['mcap_t0_minus_1'])} × |同业调整CAR|）",
            f"- 损害区间 [低, 高]: [{_yi(dmg['damage_low'])}, {_yi(dmg['damage_high'])}]",
            f"- **分类**: {dmg['classification']}"
            + (f" · 超额比例 {dmg['excess_ratio']:.2f}（准入线见配置）"
               if dmg.get("excess_ratio") else ""),
        ]
        srcs = p.get("damage_sources") or {}
        if srcs:
            lines.append("- **区间驱动假设（含来源）**：")
            for k, v in srcs.items():
                if isinstance(v, dict):
                    vals = {kk: vv for kk, vv in v.items() if kk != "source"}
                    lines.append(f"  - {k}: {vals} — 来源: {v.get('source', '?')}")
        lines += [
            "- 说明: 损害为税后利润现值（企业现金流层面），与股权市值比较基于债务未受损假设；"
            "跌幅中可能包含折现率/情绪重定价成分，本系统不声称其'必然错杀'。",
        ]
    lines += [
        "",
        "## 数据质量与核验债务",
    ]
    for d in p.get("verification_debt", []):
        lines.append(f"- {d}")
    lines += [
        "",
        f"*配置版本 {candidate_row['config_version']} · 生成 {utc_now()} · "
        "仅为研究流程产物，不构成任何投资建议。*",
    ]
    return "\n".join(lines)


def _pct(v) -> str:
    return f"{v * 100:.2f}%" if isinstance(v, (int, float)) else "n/a"


def _yi(s) -> str:
    try:
        return f"{float(s) / 1e8:,.1f}亿元"
    except (TypeError, ValueError):
        return str(s)


def freeze_card(conn: sqlite3.Connection, candidate_row, content: str) -> dict:
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    version = 1 + (conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM frozen_artifact WHERE candidate_id=?",
        (candidate_row["candidate_id"],),
    ).fetchone()["v"])
    sha = hashlib.sha256(content.encode()).hexdigest()
    path = CARDS_DIR / f"{candidate_row['candidate_id']}_v{version}.md"
    path.write_text(content)
    artifact_id = f"card_{candidate_row['candidate_id']}_v{version}"
    conn.execute(
        "UPDATE frozen_artifact SET status='SUPERSEDED',"
        " status_note=COALESCE(status_note, ?)"
        " WHERE candidate_id=? AND kind='CARD' AND status='VALID'",
        (f"Superseded by {artifact_id}", candidate_row["candidate_id"]),
    )
    conn.execute(
        "INSERT INTO frozen_artifact(artifact_id, kind, candidate_id, version, frozen_at_utc,"
        " content_sha256, path, config_version, status) VALUES(?,?,?,?,?,?,?,?,?)",
        (artifact_id, "CARD", candidate_row["candidate_id"], version, utc_now(), sha,
         str(path), candidate_row["config_version"], "VALID"),
    )
    conn.commit()
    return {"artifact_id": artifact_id, "sha256": sha, "path": str(path), "version": version}


_PCT = lambda v: f"{v * 100:+.2f}%" if isinstance(v, (int, float)) else "n/a"  # noqa: E731

_US_STATE_ZH = {
    "US_TRIAL_CANDIDATE": "试验候选（可能过度反应）",
    "US_TRIAL_NEAR_MISS": "接近触发/观察名单",
    "US_TRIAL_INSUFFICIENT_DATA": "数据不足",
}


def render_us_card_zh(conn, candidate_row) -> str:
    p = json.loads(candidate_row["profile_json"])
    rx = p.get("reaction", {})
    company = conn.execute(
        "SELECT c.name_en, l.ticker, l.exchange FROM company c"
        " JOIN listing l ON l.company_id=c.company_id WHERE c.company_id=?"
        " ORDER BY l.listing_id LIMIT 1", (candidate_row["company_id"],),
    ).fetchone()
    state_zh = _US_STATE_ZH.get(candidate_row["state"],
                                candidate_row["state"].replace("US_TRIAL_REJECTED_", "未入围："))
    content = p.get("content") or {}
    lines = [
        f"# 美股试验候选卡片：{company['name_en'] or company['ticker']}"
        f"（{company['ticker']}.{company['exchange']}）",
        "",
        f"- **状态**: {state_zh} · **市值**: 暂无法获得（XBRL基本面数据留待后续切片）",
        f"- **触发事件**: {p.get('event_type')} · 文件号 "
        f"{p.get('accession') or '（停牌事件，无文件）'}",
        f"- **SEC受理时间**: {p.get('accepted_at_utc') or '未获得（日期精度）'}"
        f" · **系统首次观察**: {p.get('first_seen_at_utc')}",
        "",
        "## 多周期市场反应（经SPY调整；QQQ对照另列）",
        f"- 事件后1个交易日: {_PCT(rx.get('post_ret1'))}（市场调整后 "
        f"{_PCT(rx.get('mkt_adj_post_ret1'))}）· 事件后累计: {_PCT(rx.get('post_cum'))}"
        f"（调整后 {_PCT(rx.get('mkt_adj_post_cum'))}）",
        f"- 截至评估日的 1/5/21/63 日收益: {_PCT(rx.get('ret1'))} / {_PCT(rx.get('ret5'))}"
        f" / {_PCT(rx.get('ret21'))} / {_PCT(rx.get('ret63'))}",
        f"- 市场调整后 5/21/63 日: {_PCT(rx.get('mkt_adj_ret5'))} /"
        f" {_PCT(rx.get('mkt_adj_ret21'))} / {_PCT(rx.get('mkt_adj_ret63'))}"
        f" · QQQ调整后1日: {_PCT(rx.get('qqq_adj_ret1'))}",
        f"- 事件日成交量/20日中位: {rx.get('volume_ratio'):.1f}x"
        if isinstance(rx.get("volume_ratio"), (int, float)) else
        "- 事件日成交量/20日中位: n/a",
        f"- 触发条件命中: {', '.join(p.get('trigger_legs') or []) or '无'}",
        "",
        "## 文件内容（初步判定）",
        f"- 主分类: **{content.get('primary', '未获取')}** · 关键词标记: "
        f"{', '.join(content.get('flags') or []) or '无'}（{content.get('content_version', '')}）",
        "- 暂时性中断 vs 持久性恶化: "
        + ("倾向暂时性/有界（初步）" if candidate_row["state"] == "US_TRIAL_CANDIDATE" else "未定"),
        "",
        "## 为何跌幅可能（或可能不）过度",
        f"- {p.get('excess_rationale') or p.get('reject_rationale') or '见上'}",
        "- 反对证据：若文件披露伴随基本面恶化（收入/客户/融资条款），则跌幅可能是合理的；"
        "本试验层未接入基本面数据，无法排除。",
        "",
        "## 未决问题",
    ]
    for qline in p.get("unresolved_questions") or ["（无记录）"]:
        lines.append(f"- {qline}")
    lines += [
        "",
        f"*试验配置 {p.get('config_version')} · 生成 {utc_now()} ·"
        " 数据为PROVISIONAL（yfinance扫描层，终选经EODHD抽验）·"
        " 本卡片为研究流程产物，不构成任何投资建议，无任何仓位或买卖指令。*",
    ]
    return "\n".join(lines)
