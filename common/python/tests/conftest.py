from __future__ import annotations

import pytest

from grafana_cloud_common.config import LokiConfig
from grafana_cloud_common.testing import loki_config


@pytest.fixture
def config() -> LokiConfig:
    return loki_config()
