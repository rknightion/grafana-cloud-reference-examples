"""Resolve the Grafana Cloud credential from Secrets Manager or SSM.

Fetched once per execution environment and cached, because a Secrets Manager
GetSecretValue on every invocation is a per-invocation cost, a per-invocation
latency, and a throttling risk at any real concurrency.

Cached with a TTL rather than forever, so a rotated token is picked up by a warm
container within the TTL instead of at its next cold start - which under steady
traffic may be hours away.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol, cast

import boto3
from botocore.exceptions import ClientError

from grafana_cloud_common.errors import ConfigError, PermanentError, RetryableError
from grafana_cloud_common.log import get_logger

_LOG = get_logger(__name__)


class SecretsSender(Protocol):
    """The one call this module makes. See S3Sender in .s3 for the reasoning."""

    def get_secret_value(self, **kwargs: Any) -> Any: ...


# Long enough that the fetch is amortised across a warm container's lifetime,
# short enough that a rotation takes effect without a deploy.
DEFAULT_CACHE_TTL_SECONDS = 900.0

# Keys accepted inside a JSON secret, in preference order. Several are supported
# because customers store this alongside credentials for other tools and the
# naming is never consistent.
_TOKEN_KEYS = ("token", "loki_token", "password", "api_key", "grafana_cloud_token")
_TENANT_KEYS = ("tenant_id", "username", "user", "instance_id", "loki_user")


@dataclass(frozen=True, slots=True)
class GrafanaCloudCredential:
    """A Cloud Access Policy token, and optionally the tenant id stored with it."""

    token: str
    tenant_id: str | None = None


def parse_secret(payload: str) -> GrafanaCloudCredential:
    """Parse a secret that is either a bare token or a JSON object.

    A bare string is accepted because that is what a customer produces when they
    paste a token into the Secrets Manager console without choosing key/value.
    """
    stripped = payload.strip()
    if not stripped:
        raise ConfigError("the credentials secret is empty")

    # A bare token can never start with a JSON opener, so anything that does is
    # meant to be JSON and a parse failure is a real error rather than a token
    # that happens to look odd.
    if not stripped.startswith(("{", "[")):
        return GrafanaCloudCredential(token=stripped)

    try:
        decoded: Any = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"credentials secret is neither a bare token nor JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ConfigError("credentials secret JSON must be an object")

    token = next((str(decoded[key]) for key in _TOKEN_KEYS if decoded.get(key)), None)
    if token is None:
        raise ConfigError(
            f"credentials secret JSON has no token. Expected one of: {', '.join(_TOKEN_KEYS)}"
        )
    tenant = next((str(decoded[key]) for key in _TENANT_KEYS if decoded.get(key)), None)
    return GrafanaCloudCredential(token=token, tenant_id=tenant)


class CredentialProvider:
    """Caching resolver for the Grafana Cloud credential.

    Pass ``provider.token`` as ``LokiClient``'s ``token_provider``; the client
    calls it again after a 401, and a rotated token is picked up without any
    coordination between the two.
    """

    __slots__ = ("_cached", "_client", "_expires_at", "_secret_id", "_static", "_ttl")

    def __init__(
        self,
        secret_id: str | None,
        *,
        static_token: str | None = None,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        client: SecretsSender | None = None,
    ) -> None:
        if secret_id is None and static_token is None:
            raise ConfigError("a secret id or a static token is required")
        self._secret_id = secret_id
        self._static = static_token
        self._ttl = ttl_seconds
        self._client = client
        self._cached: GrafanaCloudCredential | None = None
        self._expires_at = 0.0

    def _secretsmanager(self) -> SecretsSender:
        # Created lazily so a unit test never needs AWS credentials, and so a
        # static-token deployment makes no boto3 session at all. Assigned through
        # a local so the None is narrowed away for the type checker.
        client = self._client
        if client is None:
            # cast for the same reason as S3Sender in .s3 - boto3-stubs uses
            # explicit keyword parameters, which do not satisfy a **kwargs
            # Protocol.
            client = cast(SecretsSender, boto3.client("secretsmanager"))
            self._client = client
        return client

    def resolve(self, *, force: bool = False) -> GrafanaCloudCredential:
        """Return the credential, fetching it if the cache is cold or stale."""
        now = time.monotonic()
        if not force and self._cached is not None and now < self._expires_at:
            return self._cached

        if self._secret_id is None:
            assert self._static is not None  # noqa: S101 - guarded in __init__
            credential = GrafanaCloudCredential(token=self._static)
        else:
            credential = parse_secret(self._fetch(self._secret_id))

        self._cached = credential
        self._expires_at = now + self._ttl
        return credential

    # Secrets Manager models only some of its failures as named exception classes.
    # AccessDeniedException and ThrottlingException are NOT among them - they
    # arrive as a generic ClientError - so dispatch on the error code rather than
    # on `client.exceptions.<Name>`, which raises AttributeError for those two.
    _PERMANENT_CODES = frozenset(
        {
            "ResourceNotFoundException",
            "InvalidRequestException",
            "InvalidParameterException",
            "AccessDeniedException",
            "UnrecognizedClientException",
        }
    )
    _RETRYABLE_CODES = frozenset(
        {"InternalServiceError", "ThrottlingException", "LimitExceededException"}
    )

    def _fetch(self, secret_id: str) -> str:
        client = self._secretsmanager()
        try:
            raw: dict[str, Any] = dict(client.get_secret_value(SecretId=secret_id))
        except ClientError as exc:
            raise self._classify(exc, secret_id) from exc

        payload: Any = raw.get("SecretString")
        if payload is None:
            binary = raw.get("SecretBinary")
            if binary is None:
                raise ConfigError(f"secret {secret_id!r} has neither a string nor a binary value")
            payload = bytes(binary).decode()

        _LOG.debug("resolved grafana cloud credential", secret_id=secret_id)
        return str(payload)

    @classmethod
    def _classify(cls, exc: ClientError, secret_id: str) -> Exception:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code == "ResourceNotFoundException":
            return PermanentError(f"secret {secret_id!r} does not exist")
        if code in {"AccessDeniedException", "UnrecognizedClientException"}:
            return PermanentError(
                f"not permitted to read secret {secret_id!r}. The function role needs "
                f"secretsmanager:GetSecretValue on it, and kms:Decrypt on its key if "
                f"it uses a customer-managed key."
            )
        if code in cls._RETRYABLE_CODES:
            return RetryableError(f"Secrets Manager unavailable for {secret_id!r}: {code}")
        if code in cls._PERMANENT_CODES:
            return PermanentError(f"Secrets Manager rejected {secret_id!r}: {code}")
        return RetryableError(f"Secrets Manager error for {secret_id!r}: {code or exc}")

    def token(self) -> str:
        """Token-only accessor, shaped for LokiClient's token_provider parameter."""
        return self.resolve().token

    def invalidate(self) -> None:
        """Drop the cache so the next resolve() refetches."""
        self._cached = None
        self._expires_at = 0.0
