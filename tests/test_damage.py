from decimal import Decimal

import pytest

from investment_tool import damage

MA_PARAMS = {
    "affected_revenue_annual": {"low": "10e8", "high": "20e8", "source": "FY report seg. p12"},
    "net_margin": {"low": "0.10", "high": "0.20", "source": "H1 report"},
    "duration_years_temporary": {"value": 2, "source": "policy text: 2y transition"},
    "discount_rate": {"value": "0.10", "source": "config standard"},
    "mitigation_permanent": {"low": "0.0", "high": "0.3", "source": "mgmt call"},
}


def test_market_access_bracket_golden():
    b = damage.market_access(MA_PARAMS)
    # low: 10e8*0.10*annuity(2,10%) = 1e8*1.73553... = 173,553,719.0...
    assert b.low.quantize(Decimal("1")) == Decimal("173553719")
    # high: 20e8*0.20/0.10*(1-0) = 40e8
    assert b.high == Decimal("4E+9")


def test_missing_source_rejected():
    bad = {**MA_PARAMS, "net_margin": {"low": "0.1", "high": "0.2", "source": ""}}
    with pytest.raises(damage.DamageParamError, match="net_margin"):
        damage.market_access(bad)


def test_earnings_decomposition_only_structural_counts():
    params = {
        "components": [
            {"name": "沙特高基数", "classification": "BASE_EFFECT",
             "annual_profit_delta": "-30e8", "source": "FY25 report"},
            {"name": "确认时点", "classification": "TIMING",
             "annual_profit_delta": "-10e8", "source": "mgmt"},
            {"name": "户用退坡", "classification": "STRUCTURAL",
             "annual_profit_delta": "-5e8", "source": "531 policy"},
        ],
        "discount_rate": {"value": "0.10", "source": "config"},
        "duration_years_temporary": {"value": 2, "source": "assumption"},
    }
    b = damage.earnings_decomposition(params)
    assert b.high == Decimal("5E+9")  # only the structural 5e8 perpetuity


def test_classification_boundaries():
    b = damage.DamageBracket(low=Decimal("1e8"), high=Decimal("4e8"), template="t",
                             assumptions={})
    r = damage.classify(Decimal("0.5e8"), b, Decimal("1.5"))
    assert r["classification"] == "PRICED_LESS" and not r["admitted"]
    r = damage.classify(Decimal("2e8"), b, Decimal("1.5"))
    assert r["classification"] == "WITHIN_BRACKET" and not r["admitted"]
    r = damage.classify(Decimal("5e8"), b, Decimal("1.5"))
    assert r["classification"] == "EXCESS" and not r["admitted"]  # ratio 1.25 < 1.5
    r = damage.classify(Decimal("7e8"), b, Decimal("1.5"))
    assert r["classification"] == "EXCESS" and r["admitted"]  # ratio 1.75


def test_unknown_template_rejects():
    with pytest.raises(damage.DamageParamError, match="DAMAGE_MODEL_UNAVAILABLE"):
        damage.run_template("litigation", {})
