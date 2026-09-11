"""The core must not pull boto3 in.

This is the test that stops the dependency split in AGENTS.md from rotting. A
single stray `import boto3` in loki.py or config.py adds ~15 MB to every
example's deployment package and a third-party CVE surface to patch, and nothing
else in the gate would notice.

Run in a subprocess with a clean interpreter, because by the time the rest of
this test session runs, boto3 is already in sys.modules from the AWS tests.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import sys
import grafana_cloud_common  # noqa: F401
import grafana_cloud_common.batching  # noqa: F401
import grafana_cloud_common.config  # noqa: F401
import grafana_cloud_common.errors  # noqa: F401
import grafana_cloud_common.log  # noqa: F401
import grafana_cloud_common.loki  # noqa: F401
import grafana_cloud_common.testing  # noqa: F401

leaked = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in {"boto3", "botocore", "requests", "urllib3", "pytest"}
)
print(",".join(leaked))
"""


def test_core_imports_without_third_party_packages() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    leaked = [name for name in result.stdout.strip().split(",") if name]
    assert leaked == [], (
        f"the core imported third-party modules: {leaked}. Move anything needing "
        f"them into grafana_cloud_common.aws."
    )


def test_the_aws_subpackage_does_import_boto3() -> None:
    """The inverse assertion, so the test above cannot pass by the import failing."""
    probe = "import sys, grafana_cloud_common.aws; print('boto3' in sys.modules)"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "True"
