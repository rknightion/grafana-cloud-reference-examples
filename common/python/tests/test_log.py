from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from grafana_cloud_common.errors import ConfigError
from grafana_cloud_common.log import configure, sample_debug


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    level, formatters = root.level, [h.formatter for h in root.handlers]
    yield
    root.setLevel(level)
    for handler, formatter in zip(root.handlers, formatters, strict=False):
        handler.setFormatter(formatter)
    configure("INFO", debug_sample_rate=0.0)
    root.setLevel(level)


def test_a_sampled_invocation_does_not_leave_a_warm_container_at_debug() -> None:
    configure("INFO", debug_sample_rate=0.25)
    root = logging.getLogger()

    assert sample_debug(roll=lambda: 0.1) is True
    assert root.level == logging.DEBUG

    assert sample_debug(roll=lambda: 0.9) is False
    assert root.level == logging.INFO


def test_zero_rate_never_samples() -> None:
    configure("WARNING", debug_sample_rate=0.0)

    assert sample_debug(roll=lambda: 0.0) is False
    assert logging.getLogger().level == logging.WARNING


@pytest.mark.parametrize("raw", ["often", "1.5", "-0.1"])
def test_invalid_env_rate_fails_at_cold_start(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("LOG_DEBUG_SAMPLE_RATE", raw)

    with pytest.raises(ConfigError, match="LOG_DEBUG_SAMPLE_RATE"):
        configure("INFO")
