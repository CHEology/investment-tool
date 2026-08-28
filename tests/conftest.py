import pytest

from investment_tool.db import connect


@pytest.fixture
def conn(tmp_path):
    """Isolated database + data dir per test."""
    c = connect(tmp_path)
    yield c
    c.close()
