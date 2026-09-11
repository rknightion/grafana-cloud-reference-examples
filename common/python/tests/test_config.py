from __future__ import annotations

import pytest

from grafana_cloud_common.config import LokiConfig, normalise_push_url
from grafana_cloud_common.errors import ConfigError

_REQUIRED = {
    "GRAFANA_CLOUD_LOKI_ENDPOINT": "logs-prod-012.grafana.net",
    "GRAFANA_CLOUD_LOKI_TENANT_ID": "123456",
    "GRAFANA_CLOUD_CREDENTIALS_SECRET_ID": "grafana-cloud/loki",
}


class TestNormalisePushUrl:
    @pytest.mark.parametrize(
        "given",
        [
            "logs-prod-012.grafana.net",
            "https://logs-prod-012.grafana.net",
            "https://logs-prod-012.grafana.net/",
            "https://logs-prod-012.grafana.net/loki",
            "https://logs-prod-012.grafana.net/loki/api/v1/push",
        ],
    )
    def test_every_form_a_customer_pastes_resolves_to_the_push_url(self, given: str) -> None:
        assert normalise_push_url(given) == ("https://logs-prod-012.grafana.net/loki/api/v1/push")

    def test_rejects_plain_http(self) -> None:
        with pytest.raises(ConfigError, match="must be https"):
            normalise_push_url("http://logs-prod-012.grafana.net")

    def test_rejects_an_unrecognised_path_rather_than_guessing(self) -> None:
        with pytest.raises(ConfigError, match="not recognised"):
            normalise_push_url("https://logs-prod-012.grafana.net/prometheus/api/v1/write")

    def test_rejects_an_empty_endpoint(self) -> None:
        with pytest.raises(ConfigError, match="empty"):
            normalise_push_url("   ")


class TestFromEnv:
    def test_builds_from_the_minimum_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key, value in _REQUIRED.items():
            monkeypatch.setenv(key, value)
        config = LokiConfig.from_env()
        assert config.tenant_id == "123456"
        assert config.credentials_secret_id == "grafana-cloud/loki"
        assert config.push_url.endswith("/loki/api/v1/push")

    def test_requires_a_credential_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GRAFANA_CLOUD_LOKI_ENDPOINT", "logs.example.net")
        monkeypatch.setenv("GRAFANA_CLOUD_LOKI_TENANT_ID", "1")
        monkeypatch.delenv("GRAFANA_CLOUD_CREDENTIALS_SECRET_ID", raising=False)
        monkeypatch.delenv("GRAFANA_CLOUD_LOKI_TOKEN", raising=False)
        with pytest.raises(ConfigError, match="GRAFANA_CLOUD_CREDENTIALS_SECRET_ID"):
            LokiConfig.from_env()

    def test_treats_an_empty_string_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty Terraform variable or CloudFormation parameter renders as "",
        which must fail loudly rather than producing a request to https:///."""
        for key, value in _REQUIRED.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("GRAFANA_CLOUD_LOKI_TENANT_ID", "  ")
        with pytest.raises(ConfigError, match="required but unset or empty"):
            LokiConfig.from_env()

    def test_rejects_a_non_numeric_batch_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key, value in _REQUIRED.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("LOKI_BATCH_MAX_LINES", "lots")
        with pytest.raises(ConfigError, match="must be an integer"):
            LokiConfig.from_env()

    def test_rejects_a_zero_batch_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key, value in _REQUIRED.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("LOKI_BATCH_MAX_LINES", "0")
        with pytest.raises(ConfigError, match="must be positive"):
            LokiConfig.from_env()

    def test_parses_static_labels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key, value in _REQUIRED.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("LOKI_STATIC_LABELS", '{"env":"prod","region":"eu-west-1"}')
        assert LokiConfig.from_env().static_labels == {"env": "prod", "region": "eu-west-1"}

    def test_rejects_static_labels_that_are_not_an_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key, value in _REQUIRED.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("LOKI_STATIC_LABELS", '["env","prod"]')
        with pytest.raises(ConfigError, match="must be a JSON object"):
            LokiConfig.from_env()

    @pytest.mark.parametrize(("given", "expected"), [("false", False), ("0", False), ("on", True)])
    def test_parses_booleans(
        self, monkeypatch: pytest.MonkeyPatch, given: str, expected: bool
    ) -> None:
        for key, value in _REQUIRED.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("LOKI_COMPRESS", given)
        assert LokiConfig.from_env().compress is expected
