from investment_tool.annc import CONTENT_REVIEW, HARD_NEGATIVE, POSITIVE, classify, eligible_from


def test_hard_negatives():
    assert classify("关于收到中国证监会立案告知书的公告")[2] == HARD_NEGATIVE
    assert classify("关于持股5%以上股东减持股份计划的公告")[2] == HARD_NEGATIVE
    assert classify("关于收到行政处罚决定书的公告")[2] == HARD_NEGATIVE


def test_periodic_report_requires_content_review_not_negative():
    etype, lane, relevance = classify("2026年半年度报告")
    assert etype == "PERIODIC_REPORT" and relevance == CONTENT_REVIEW
    assert classify("2026年半年度业绩预告")[2] == CONTENT_REVIEW


def test_positive_and_lane_b():
    assert classify("关于回购公司股份方案的公告")[2] == POSITIVE
    assert classify("关于预中标重大项目的提示性公告")[:2] == ("CONTRACT_AWARD_NOTICE", "B")
    assert classify("关于取得医疗器械注册证书的公告")[:2] == ("CERTIFICATION_APPROVAL", "B")


def test_other_neutral():
    assert classify("关于召开2026年第一次临时股东大会的通知")[2] == "NEUTRAL"


def test_eligible_from_is_beijing_date():
    # 2026-08-28T16:00:00Z == 2026-08-29 00:00 Beijing -> eligible from 08-29
    assert eligible_from("2026-08-28T16:00:00Z") == "2026-08-29"
    assert eligible_from("2026-08-27T04:00:00Z") == "2026-08-27"
    assert eligible_from(None) is None


def test_temporal_eligibility_semantics():
    """An announcement first available AFTER a price episode's t0 cannot
    attribute that episode (review issue 1)."""
    t0 = "2026-08-27"
    e = eligible_from("2026-08-28T16:00:00Z")  # available 08-29 Beijing
    assert not (e <= t0)
