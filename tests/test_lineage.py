import gzip
import hashlib
import json

import pytest

from investment_tool.lineage import record_fetch
from investment_tool.quality import Quality, QualityState


def test_manifest_and_raw_written(conn, tmp_path):
    payload = b'{"hello": "world"}'
    m = record_fetch(
        conn, provider="testprov", dataset="ds", params={"a": 1},
        source_url="https://example.com/x", payload=payload, http_status=200,
        quality=Quality(QualityState.OK), config_version="v0", data_dir=tmp_path,
    )
    assert m.raw_sha256 == hashlib.sha256(payload).hexdigest()
    raw_file = tmp_path / m.raw_path
    assert gzip.open(raw_file, "rb").read() == payload

    row = conn.execute("SELECT * FROM manifest WHERE manifest_id=?", (m.manifest_id,)).fetchone()
    assert row["provider"] == "testprov"
    assert row["quality_state"] == "OK"
    assert json.loads(row["params_json"]) == {"a": 1}
    assert row["retrieved_at_utc"].endswith("Z")


def test_credentialed_url_rejected(conn, tmp_path):
    with pytest.raises(ValueError, match="credential"):
        record_fetch(
            conn, provider="p", dataset="d", params={},
            source_url="https://api.example.com/data?token=SECRET", payload=b"x",
            http_status=200, quality=Quality(QualityState.OK), config_version="v0",
            data_dir=tmp_path,
        )


def test_error_fetch_still_leaves_manifest(conn, tmp_path):
    m = record_fetch(
        conn, provider="p", dataset="d", params={}, source_url="https://example.com",
        payload=None, http_status=500, quality=Quality(QualityState.ERROR, "http=500"),
        config_version="v0", data_dir=tmp_path,
    )
    row = conn.execute("SELECT quality_state FROM manifest WHERE manifest_id=?",
                       (m.manifest_id,)).fetchone()
    assert row["quality_state"] == "ERROR"
