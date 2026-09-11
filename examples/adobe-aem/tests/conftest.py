"""Import-time environment for the handler under test, plus fixture access.

`adobe_aem.handler` resolves its configuration at module scope on purpose, so a
bad config fails the first invocation loudly rather than degrading quietly. That
means a test has to set the environment before the first import, and reload the
module to exercise a different setting.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

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
    "KEY_PATTERN",
    "AEM_PROGRAM_ID",
    "AEM_ENV_ID",
    "AEM_ENV_TYPE",
    "AEM_TIER",
    "LINE_CONTENT",
    "SNIFF_CONTENT",
    "CORRELATE_REQUESTS",
    "MAX_PENDING_REQUESTS",
    "DROP_CLIENT_IP",
    "LOG_UTC_OFFSET_SECONDS",
    "MAX_TIMESTAMP_AGE_SECONDS",
    "SOURCE_KEY_SUFFIX",
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

        module = load_handler(AEM_ENV_ID="e67890", LINE_CONTENT="message")
    """

    def _load(**environment: str) -> ModuleType:
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        import adobe_aem.handler as handler

        return importlib.reload(handler)

    yield _load
    import adobe_aem.handler as handler

    for key in HANDLER_ENV_KEYS:
        if key not in BASE_ENV:
            os.environ.pop(key, None)
    importlib.reload(handler)


@pytest.fixture(scope="session")
def fixture_lines() -> dict[str, list[str]]:
    """Every committed fixture, keyed by filename.

    Session-scoped because these are read-only and there are a thousand lines of
    them; re-reading per test is pure overhead.
    """
    return {
        path.name: path.read_text(encoding="utf-8").splitlines()
        for path in sorted(FIXTURES.glob("*.log"))
    }
