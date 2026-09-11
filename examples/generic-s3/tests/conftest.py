"""Import-time environment for the handler under test.

`generic_s3.handler` resolves its configuration at module scope on purpose, so a
bad config fails the first invocation loudly rather than degrading quietly. That
means a test has to set the environment before the first import, and reload the
module to exercise a different setting.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from types import ModuleType

import pytest

BASE_ENV = {
    "GRAFANA_CLOUD_LOKI_ENDPOINT": "logs-prod-012.grafana.net",
    "GRAFANA_CLOUD_LOKI_TENANT_ID": "123456",
    # The static-token path, so importing the module makes no AWS call at all.
    "GRAFANA_CLOUD_LOKI_TOKEN": "glc_fake",
}

# Everything the handler reads. Cleared between tests so one test's override
# cannot leak into the next through a reload.
HANDLER_ENV_KEYS = (
    *BASE_ENV,
    "GRAFANA_CLOUD_CREDENTIALS_SECRET_ID",
    "SERVICE_NAME",
    "RECORD_FORMAT",
    "SOURCE_KEY_SUFFIX",
    "PREFIX_LABEL_DEPTH",
    "TIMESTAMP_FIELD",
    "TIMESTAMP_FORMAT",
    "MAX_TIMESTAMP_AGE_SECONDS",
    "LOKI_STATIC_LABELS",
    "LOKI_COMPRESS",
    "LOKI_BATCH_MAX_LINES",
    "LOKI_BATCH_MAX_BYTES",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in HANDLER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    yield


@pytest.fixture
def load_handler(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Import the handler with extra environment applied.

    Yields a callable so a test can choose its own configuration:

        module = load_handler(PREFIX_LABEL_DEPTH="2")
    """

    def _load(**environment: str) -> ModuleType:
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        import generic_s3.handler as handler

        return importlib.reload(handler)

    yield _load
    # Leave the module in the base configuration, so a test that imports it
    # directly rather than through the fixture sees a predictable state.
    import generic_s3.handler as handler

    for key in HANDLER_ENV_KEYS:
        if key not in BASE_ENV:
            os.environ.pop(key, None)
    importlib.reload(handler)
