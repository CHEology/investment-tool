"""Lane A conservative damage-PV templates (deterministic; DESIGN 8).

Two v0 templates. Every parameter must carry a non-empty `source` string; a
parameter without a source fails validation — the calculator never invents
numbers. All arithmetic in Decimal. Other event types reject with
DAMAGE_MODEL_UNAVAILABLE rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from investment_tool.numeric import dec

TEMPLATES = ("market_access", "earnings_decomposition")


class DamageParamError(ValueError):
    pass


def _req(params: dict, key: str) -> dict:
    node = params.get(key)
    if not isinstance(node, dict) or not str(node.get("source", "")).strip():
        raise DamageParamError(f"parameter '{key}' missing or lacks a source")
    return node


def _annuity(years: Decimal, r: Decimal) -> Decimal:
    """PV factor of a level annual amount over `years` at rate r."""
    one = Decimal(1)
    return (one - (one + r) ** (-years)) / r


@dataclass
class DamageBracket:
    low: Decimal
    high: Decimal
    template: str
    assumptions: dict


def market_access(params: dict) -> DamageBracket:
    """Loss of access to a market/geography.

    low  = temporary scenario: low affected profit for `duration_years`.
    high = permanent scenario: high affected profit as perpetuity, reduced by
           the LOW mitigation bound (conservative = worst plausible damage).
    Profit-level inputs are post-tax (net margin), so the bracket is an
    equity-comparable PV under the debt-unimpaired assumption the survival
    floor checks (EV<->equity reconciliation, DESIGN 8).
    """
    rev = _req(params, "affected_revenue_annual")
    margin = _req(params, "net_margin")
    dur = _req(params, "duration_years_temporary")
    r = dec(str(_req(params, "discount_rate")["value"]))
    mit = _req(params, "mitigation_permanent")

    low = dec(str(rev["low"])) * dec(str(margin["low"])) * _annuity(dec(str(dur["value"])), r)
    mit_low = dec(str(mit["low"]))
    high = dec(str(rev["high"])) * dec(str(margin["high"])) / r * (Decimal(1) - mit_low)
    return DamageBracket(low=low, high=high, template="market_access", assumptions=params)


def earnings_decomposition(params: dict) -> DamageBracket:
    """Headline-miss decomposition: only STRUCTURAL components are damage;
    BASE_EFFECT / TIMING / ONE_OFF components explain the optics but carry no
    forward PV. low = structural profit delta for `duration_years`; high =
    structural delta as perpetuity."""
    comps = params.get("components")
    if not isinstance(comps, list) or not comps:
        raise DamageParamError("components list required")
    structural = Decimal(0)
    for c in comps:
        if not str(c.get("source", "")).strip():
            raise DamageParamError(f"component '{c.get('name')}' lacks a source")
        if c.get("classification") == "STRUCTURAL":
            structural += dec(str(c["annual_profit_delta"]))
    r = dec(str(_req(params, "discount_rate")["value"]))
    dur = dec(str(_req(params, "duration_years_temporary")["value"]))
    structural = abs(structural)
    return DamageBracket(
        low=structural * _annuity(dur, r), high=structural / r,
        template="earnings_decomposition", assumptions=params,
    )


def run_template(name: str, params: dict) -> DamageBracket:
    if name == "market_access":
        return market_access(params)
    if name == "earnings_decomposition":
        return earnings_decomposition(params)
    raise DamageParamError(f"DAMAGE_MODEL_UNAVAILABLE: no template '{name}'")


def classify(dmcap_abnormal: Decimal, bracket: DamageBracket,
             excess_ratio_min: Decimal) -> dict:
    """PRICED_LESS | WITHIN_BRACKET | EXCESS(ratio); admission needs
    EXCESS and ratio >= excess_ratio_min (frozen v0)."""
    d = abs(dmcap_abnormal)
    if d < bracket.low:
        cls, ratio = "PRICED_LESS", None
    elif d <= bracket.high:
        cls, ratio = "WITHIN_BRACKET", None
    else:
        cls = "EXCESS"
        ratio = d / bracket.high if bracket.high > 0 else None
    admitted = cls == "EXCESS" and ratio is not None and ratio >= excess_ratio_min
    return {
        "classification": cls,
        "excess_ratio": float(ratio) if ratio is not None else None,
        "admitted": admitted,
        "dmcap_abnormal": str(d),
        "damage_low": str(bracket.low),
        "damage_high": str(bracket.high),
        "template": bracket.template,
    }
