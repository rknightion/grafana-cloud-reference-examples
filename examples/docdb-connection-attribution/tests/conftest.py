"""The handler itself is not imported in tests: it resolves AWS clients and secrets at module
scope, and everything with branching lives in the modules these tests exercise directly."""

from __future__ import annotations

import pytest
from docdb_fakes import FakeDynamoDB


@pytest.fixture
def ddb() -> FakeDynamoDB:
    return FakeDynamoDB()
