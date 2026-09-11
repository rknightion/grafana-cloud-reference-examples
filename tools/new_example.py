#!/usr/bin/env python3
"""Scaffold a new example.

Run through `just new-example <name> [runtime]`, never directly.

Writes a complete example that already passes `just check`: manifest, README,
handler, tests, a Terraform root module calling the shared modules, and a
CloudFormation template copied from the canonical reference. The point is that
the structural decisions are already made, so a new example starts on its own
logic rather than on plumbing.

Deliberately does NOT touch git, and deliberately does not add release-please
config. Both are reviewed edits, not generated ones.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
REFERENCE_TEMPLATE = (
    REPO_ROOT / "common" / "cloudformation" / "templates" / "lambda-s3-loki.reference.yaml"
)
TEMPLATE_EXAMPLE = "generic-s3"

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,48}[a-z0-9]$")

# Kept in step with common/cloudformation/conformance.yaml. A scaffolded example
# must not start life on a runtime that is about to be deprecated.
DEFAULT_RUNTIME = {
    "python": "python3.14",
    "nodejs": "nodejs24.x",
    "go": "provided.al2023",
}

MANIFEST = """\
# The manifest is the single source of truth for this example. `just lint` fails
# if the Terraform variable default or the CloudFormation parameter default
# disagrees with `runtime` here.
name: {name}
title: TODO one-line title for the README table
summary: >-
  TODO two or three sentences: what this ships, from where, to which Grafana
  Cloud surface, and anything a reader needs to know before deploying it.
status: planned
platform: aws
deliverable: lambda-zip

runtime:
  identifier: {runtime}
  architecture: arm64

lambda:
  handler: {module}.handler.lambda_handler
  package_root: src
  memory_mb: 512
  timeout_seconds: 300

destinations:
  - grafana-cloud-loki

iac:
  terraform: terraform
  cloudformation: cloudformation/template.yaml

release:
  component: {name}
"""

README = """\
# {name}: TODO one-line description

**Status: planned.** Scaffolded, not implemented.

TODO Replace this file before changing `status` in `example.yaml`. Until then
the conformance check exempts this example from the required file set.

Write these sections, in this order - they are the ones a reader actually needs:

1. **What it does**, in two sentences.
2. **What gets created**, as a list of AWS resources, and what it explicitly
   does not touch.
3. **Before you start** - the Grafana Cloud endpoint, tenant id and Cloud Access
   Policy token, and the `aws secretsmanager create-secret` command. Link
   `docs/grafana-cloud-credentials.md` rather than restating it.
4. **Deploying**, both the Terraform and the CloudFormation path.
5. **Configuration**, as a table of every environment variable with its default.
6. **Labels, and what deliberately is not one.** Name the fields that go to
   structured metadata instead, and say why. Link `docs/loki-ingestion.md`.
7. **Costs worth knowing before you turn it on.** Loki ingest, Lambda
   GB-seconds, CloudWatch Logs, and any per-request charge.
8. **Operating it** - what the alarms cover, and what to do when the DLQ fills.
9. **Limitations**, honestly.

See [`generic-s3`](../{template}) for a worked version of all nine.
"""

HANDLER = '''\
"""TODO what this ships, and to where.

Everything expensive is built at module scope, so it happens once per execution
environment rather than once per invocation, and a bad configuration fails the
first invocation with a clear message instead of degrading quietly.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

from grafana_cloud_common import (
    LogEntry,
    LokiClient,
    LokiConfig,
    StreamBatcher,
    configure,
    get_logger,
)
from grafana_cloud_common.aws import (
    CredentialProvider,
    LambdaContext,
    S3ObjectReader,
    S3ObjectRef,
    parse_event,
    process_messages,
)

configure()
_LOG = get_logger(__name__)

CONFIG = LokiConfig.from_env()
CREDENTIALS = CredentialProvider(CONFIG.credentials_secret_id, static_token=CONFIG.token)
CLIENT = LokiClient(CONFIG, CREDENTIALS.token)
READER = S3ObjectReader()


def labels_for(ref: S3ObjectRef) -> dict[str, str]:
    """Stream labels for one object.

    Low-cardinality dimensions only. Anything derived from an object key, a
    record id or a timestamp is one Loki stream per value; put it in the entry's
    structured metadata instead. The client rejects a high-cardinality label
    rather than letting you find out from the bill.
    """
    return {{
        "service_name": os.environ.get("SERVICE_NAME", "{name}"),
        "bucket": ref.bucket,
    }}


def _entries(ref: S3ObjectRef) -> Iterator[tuple[dict[str, str], LogEntry]]:
    labels = labels_for(ref)
    now_ns = time.time_ns()
    for record_number, line in READER.lines(ref):
        # TODO parse `line` into whatever this example actually ships, and set
        # the timestamp from the record if it carries one. Ingestion time is the
        # safe default: Loki rejects samples older than the tenant's
        # reject_old_samples_max_age with a 400 that no retry fixes.
        yield (
            labels,
            LogEntry(
                timestamp_ns=now_ns,
                line=line,
                structured_metadata={{"object_key": ref.key, "record": str(record_number)}},
            ),
        )


def ship_object(ref: S3ObjectRef) -> int:
    """Read one object and push every line. Returns the line count."""
    if ref.size_bytes == 0:
        _LOG.info("skipping zero-byte object", uri=ref.uri)
        return 0

    batcher = StreamBatcher(max_lines=CONFIG.batch_max_lines, max_bytes=CONFIG.batch_max_bytes)
    shipped = 0
    for batch in batcher.drain(_entries(ref)):
        shipped += CLIENT.push(batch)
    _LOG.info("object shipped", uri=ref.uri, lines=shipped)
    return shipped


def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Entry point. Configured as `{module}.handler.lambda_handler`.

    Always returns the SQS partial-batch-failure response. It is ignored for a
    direct S3 invocation; for an SQS source it is the difference between
    redelivering one bad message and redelivering the whole batch.
    """
    log = _LOG.bind(aws_request_id=getattr(context, "aws_request_id", "local"))
    messages = parse_event(event)
    if not messages:
        log.info("event carried no objects; nothing to do")
        return {{"batchItemFailures": []}}

    outcome = process_messages(messages, ship_object, context=context)
    log.info(
        "invocation complete",
        objects_processed=outcome.objects_processed,
        objects_failed=outcome.objects_failed,
        lines_shipped=outcome.lines_shipped,
    )
    return outcome.to_response()
'''

TEST = '''\
"""Tests for {name}.

Start with the parsing and the label derivation - those are where the bugs are.
The S3 reading, batching, Loki push and partial-failure handling are covered by
common/python/tests and do not need retesting here.
"""

from __future__ import annotations

from {module}.handler import labels_for

from grafana_cloud_common.aws.s3 import S3ObjectRef
from grafana_cloud_common.loki import validate_labels


def test_labels_are_low_cardinality() -> None:
    """validate_labels rejects anything derived from a key, an id or a time, so
    this fails loudly rather than at deploy time."""
    labels = labels_for(S3ObjectRef(bucket="acme-logs", key="deep/path/to/object.log.gz"))
    validate_labels(labels)


def test_labels_do_not_include_the_object_key() -> None:
    labels = labels_for(S3ObjectRef(bucket="acme-logs", key="a/b/c.log"))
    assert "a/b/c.log" not in labels.values()
'''

PYPROJECT = """\
[project]
name = "grafana-cloud-example-{name}"
version = "0.1.0"
description = "TODO one-line description"
readme = "README.md"
requires-python = ">={python_requires}"
license = "Apache-2.0"

# grafana-cloud-common is a workspace path dependency, vendored into the zip by
# `just package {name}`, not installed from an index.
dependencies = [
  "grafana-cloud-common",
  "boto3>=1.40.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{module}"]
"""

TFVARS = """\
# Copy to terraform.tfvars and edit. terraform.tfvars is gitignored; only this
# .example file is tracked.

source_bucket_name = "acme-logs"

grafana_cloud_loki_endpoint  = "https://logs-prod-012.grafana.net"
grafana_cloud_loki_tenant_id = "123456"

# The secret must already exist. The token is never a Terraform input, so it
# never lands in state. Create it from a file rather than inline, so the token
# does not land in your shell history or in the process list:
#
#   umask 077 && cat > /tmp/gc-loki.json <<'JSON'
#   {{"tenant_id":"123456","token":"glc_..."}}
#   JSON
#   aws secretsmanager create-secret --name grafana-cloud/loki \\
#     --secret-string file:///tmp/gc-loki.json
#   rm -f /tmp/gc-loki.json
credentials_secret_id = "grafana-cloud/loki"

service_name = "{name}"
"""


def die(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="example directory name, lower-kebab-case")
    parser.add_argument(
        "--runtime",
        default="python",
        choices=sorted(DEFAULT_RUNTIME),
        help="language family; the exact runtime identifier is chosen for you",
    )
    args = parser.parse_args(argv)

    name: str = args.name
    if not NAME_PATTERN.match(name):
        return die(
            f"{name!r} is not a valid example name. Use lower-kebab-case, starting with a "
            f"letter, 3-50 characters."
        )

    target = EXAMPLES_DIR / name
    if target.exists():
        return die(f"examples/{name} already exists")

    if args.runtime != "python":
        return die(
            f"--runtime {args.runtime} is not scaffolded yet. The shared "
            f"{args.runtime} base exists under common/, but this generator only "
            f"writes a Python example. Add the templates here rather than "
            f"hand-rolling the example."
        )

    module = name.replace("-", "_")
    runtime = DEFAULT_RUNTIME[args.runtime]
    python_requires = runtime.removeprefix("python")

    (target / "src" / module).mkdir(parents=True)
    (target / "tests").mkdir()
    (target / "terraform").mkdir()
    (target / "cloudformation").mkdir()

    (target / "example.yaml").write_text(
        MANIFEST.format(name=name, runtime=runtime, module=module), encoding="utf-8"
    )
    (target / "README.md").write_text(
        README.format(name=name, template=TEMPLATE_EXAMPLE), encoding="utf-8"
    )
    (target / "pyproject.toml").write_text(
        PYPROJECT.format(name=name, module=module, python_requires=python_requires),
        encoding="utf-8",
    )
    (target / "src" / module / "__init__.py").write_text(
        f'"""{name} Lambda.\n\nThe handler is ``{module}.handler.lambda_handler``.\n"""\n',
        encoding="utf-8",
    )
    (target / "src" / module / "handler.py").write_text(
        HANDLER.format(name=name, module=module), encoding="utf-8"
    )
    (target / "tests" / f"test_{module}.py").write_text(
        TEST.format(name=name, module=module), encoding="utf-8"
    )

    # The Terraform root module is copied from the template example rather than
    # generated: it is real, validated code, and a generated copy would drift
    # from the shared modules' actual interface.
    template_tf = EXAMPLES_DIR / TEMPLATE_EXAMPLE / "terraform"
    for tf_file in sorted(template_tf.glob("*.tf")):
        body = tf_file.read_text(encoding="utf-8")
        body = body.replace(f"grafana-cloud-{TEMPLATE_EXAMPLE}", f"grafana-cloud-{name}")
        body = body.replace(f'"{TEMPLATE_EXAMPLE}"', f'"{name}"')
        body = body.replace(
            f"{TEMPLATE_EXAMPLE.replace('-', '_')}.handler.lambda_handler",
            f"{module}.handler.lambda_handler",
        )
        (target / "terraform" / tf_file.name).write_text(body, encoding="utf-8")
    (target / "terraform" / "terraform.tfvars.example").write_text(
        TFVARS.format(name=name), encoding="utf-8"
    )

    # CloudFormation starts from the canonical base, not from another example,
    # so a scaffolded template inherits the conformance-satisfying shape.
    cfn = REFERENCE_TEMPLATE.read_text(encoding="utf-8")
    cfn = cfn.replace(
        "# CANONICAL BASE TEMPLATE - copy this to start a new example, do not reference it.",
        f"# {name}: TODO one-line description.\n#\n"
        f"# Copied from common/cloudformation/templates/lambda-s3-loki.reference.yaml.\n"
        f"# Standalone on purpose; see that file for why nested stacks are not used.",
    )
    cfn = cfn.replace(
        "    Description: Entrypoint, for example my_module.handler.lambda_handler",
        f"    Default: {module}.handler.lambda_handler\n"
        f"    Description: Entrypoint. Change this only if you have renamed the module.",
    )
    cfn = cfn.replace("    Default: s3-to-loki", f"    Default: {name}")
    (target / "cloudformation" / "template.yaml").write_text(cfn, encoding="utf-8")
    shutil.copy2(
        EXAMPLES_DIR / TEMPLATE_EXAMPLE / "cloudformation" / "parameters.example.json",
        target / "cloudformation" / "parameters.example.json",
    )

    print(f"Created examples/{name} ({runtime}, arm64).")
    print()
    print("Three edits are required before it will pass `just check`:")
    print(f'  1. pyproject.toml: add "examples/{name}" to tool.uv.workspace.members')
    print(f'  2. release-please-config.json: add an "examples/{name}" package entry,')
    print("     and the same path with version 0.1.0 in .release-please-manifest.json")
    print(f"  3. examples/{name}/example.yaml: fill in title and summary")
    print()
    print("Then: just setup && just check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
