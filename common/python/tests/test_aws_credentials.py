from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from grafana_cloud_common.aws.credentials import CredentialProvider, parse_secret
from grafana_cloud_common.errors import ConfigError, PermanentError, RetryableError


def client_error(code: str) -> ClientError:
    """A real ClientError, because Secrets Manager does not model AccessDenied or
    Throttling as named exception classes - they arrive as this."""
    # Typed as Any because the boto3 stubs require the full ResponseMetadata
    # TypedDict, and a test does not need HostId or RetryAttempts to be real.
    response: Any = {"Error": {"Code": code, "Message": code}}
    return ClientError(response, "GetSecretValue")


class FakeSecretsManager:
    def __init__(self, value: str | None = None, raises: Exception | None = None) -> None:
        self._value = value
        self._raises = raises
        self.call_count = 0

    def get_secret_value(self, **_: Any) -> dict[str, Any]:
        self.call_count += 1
        if self._raises is not None:
            raise self._raises
        return {"SecretString": self._value}


class TestParseSecret:
    def test_accepts_a_bare_token(self) -> None:
        """What a customer produces when they paste a token into the console
        without choosing key/value."""
        assert parse_secret("  glc_abc123  ").token == "glc_abc123"

    def test_accepts_the_documented_json_shape(self) -> None:
        credential = parse_secret('{"tenant_id":"123456","token":"glc_abc"}')
        assert credential == parse_secret('{"tenant_id":"123456","token":"glc_abc"}')
        assert credential.token == "glc_abc"
        assert credential.tenant_id == "123456"

    @pytest.mark.parametrize("key", ["token", "loki_token", "password", "api_key"])
    def test_accepts_the_token_under_any_common_key(self, key: str) -> None:
        assert parse_secret(f'{{"{key}":"glc_abc"}}').token == "glc_abc"

    @pytest.mark.parametrize("key", ["username", "user", "instance_id", "loki_user"])
    def test_accepts_the_tenant_under_any_common_key(self, key: str) -> None:
        secret = f'{{"token":"t","{key}":"99"}}'
        assert parse_secret(secret).tenant_id == "99"

    def test_rejects_an_empty_secret(self) -> None:
        with pytest.raises(ConfigError, match="is empty"):
            parse_secret("   ")

    def test_rejects_json_with_no_token(self) -> None:
        with pytest.raises(ConfigError, match="no token"):
            parse_secret('{"tenant_id":"123456"}')

    def test_rejects_a_json_array(self) -> None:
        with pytest.raises(ConfigError, match="must be an object"):
            parse_secret('["glc_abc"]')


class TestCredentialProvider:
    def test_requires_a_source(self) -> None:
        with pytest.raises(ConfigError, match="secret id or a static token"):
            CredentialProvider(None)

    def test_a_static_token_never_calls_aws(self) -> None:
        provider = CredentialProvider(None, static_token="glc_local")
        assert provider.token() == "glc_local"

    def test_caches_within_the_ttl(self) -> None:
        client = FakeSecretsManager('{"token":"glc_abc"}')
        provider = CredentialProvider("secret", client=client)  # type: ignore[arg-type]
        assert provider.token() == "glc_abc"
        assert provider.token() == "glc_abc"
        assert client.call_count == 1

    def test_a_zero_ttl_refetches_so_a_rotation_is_picked_up(self) -> None:
        client = FakeSecretsManager('{"token":"glc_abc"}')
        provider = CredentialProvider("secret", ttl_seconds=0.0, client=client)  # type: ignore[arg-type]
        provider.token()
        provider.token()
        assert client.call_count == 2

    def test_invalidate_forces_a_refetch(self) -> None:
        client = FakeSecretsManager('{"token":"glc_abc"}')
        provider = CredentialProvider("secret", client=client)  # type: ignore[arg-type]
        provider.token()
        provider.invalidate()
        provider.token()
        assert client.call_count == 2

    def test_a_missing_secret_is_permanent(self) -> None:
        client = FakeSecretsManager(raises=client_error("ResourceNotFoundException"))
        provider = CredentialProvider("secret", client=client)  # type: ignore[arg-type]
        with pytest.raises(PermanentError, match="does not exist"):
            provider.token()

    def test_access_denied_names_the_two_permissions_it_needs(self) -> None:
        """AccessDeniedException is not a modelled Secrets Manager exception, so a
        handler written against client.exceptions.AccessDeniedException raises
        AttributeError instead of reporting the real problem."""
        client = FakeSecretsManager(raises=client_error("AccessDeniedException"))
        provider = CredentialProvider("secret", client=client)  # type: ignore[arg-type]
        with pytest.raises(PermanentError, match="kms:Decrypt"):
            provider.token()

    @pytest.mark.parametrize(
        "code", ["ThrottlingException", "InternalServiceError", "LimitExceededException"]
    )
    def test_transient_failures_are_retryable(self, code: str) -> None:
        client = FakeSecretsManager(raises=client_error(code))
        provider = CredentialProvider("secret", client=client)  # type: ignore[arg-type]
        with pytest.raises(RetryableError):
            provider.token()

    def test_an_unknown_code_is_retryable_rather_than_fatal(self) -> None:
        client = FakeSecretsManager(raises=client_error("SomethingNew"))
        provider = CredentialProvider("secret", client=client)  # type: ignore[arg-type]
        with pytest.raises(RetryableError, match="SomethingNew"):
            provider.token()
