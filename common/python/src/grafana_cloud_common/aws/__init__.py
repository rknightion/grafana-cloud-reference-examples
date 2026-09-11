"""AWS-specific helpers.

Everything in this subpackage may import boto3. Everything outside it must not -
that split is what keeps a minimal example's deployment package to a few tens of
kilobytes, and it is enforced by a test in common/python/tests.
"""

from .credentials import (
    CredentialProvider,
    GrafanaCloudCredential,
    SecretsSender,
    parse_secret,
)
from .handler import (
    BatchOutcome,
    LambdaContext,
    SourceMessage,
    parse_event,
    process_messages,
)
from .s3 import (
    RecordFormat,
    S3ObjectReader,
    S3ObjectRef,
    S3Sender,
    detect_format,
    is_compressed,
)

__all__ = [
    "BatchOutcome",
    "CredentialProvider",
    "GrafanaCloudCredential",
    "LambdaContext",
    "RecordFormat",
    "S3ObjectReader",
    "S3ObjectRef",
    "S3Sender",
    "SecretsSender",
    "SourceMessage",
    "detect_format",
    "is_compressed",
    "parse_event",
    "parse_secret",
    "process_messages",
]
