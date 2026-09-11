#!/usr/bin/env python3
"""Validate every example against the repo's structural contract.

Run through `just lint`, never directly - that is the only supported entry point.

Three jobs:

1. Each `examples/*/example.yaml` validates against `common/schemas/example.schema.json`,
   and a non-planned example has the full required file set.
2. The runtime identifier agrees across all three places it appears: the
   manifest, the Terraform variable default, and the CloudFormation parameter
   default. This is the check that stops a runtime bump from being applied in one
   place and silently missed in the other two.
3. Each CloudFormation template satisfies `common/cloudformation/conformance.yaml`.
   CloudFormation reuse here is by copy rather than nested stacks, so this is
   what stops the copies diverging on the things that matter.

Exit 0 when everything passes, 1 otherwise, with every failure printed rather
than stopping at the first.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
SCHEMA_PATH = REPO_ROOT / "common" / "schemas" / "example.schema.json"
CONFORMANCE_PATH = REPO_ROOT / "common" / "cloudformation" / "conformance.yaml"

# Files a non-planned example must have. A planned example is exempt from this
# and nothing else.
REQUIRED_FILES = (
    "README.md",
    "example.yaml",
)
REQUIRED_DIRS_BY_DELIVERABLE = {
    "lambda-zip": ("src", "tests", "terraform", "cloudformation"),
    "container-image": ("src", "tests"),
    "script": ("src",),
    "terraform-module": ("terraform",),
    "helm-chart": ("chart",),
}


class CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation's short-form intrinsics.

    A plain SafeLoader dies on `!Ref` with "could not determine a constructor".
    The values are turned back into the `{"Ref": ...}` long form so the rule
    checks below can walk one consistent shape.
    """


def _construct_intrinsic(loader: CfnLoader, tag_suffix: str, node: yaml.Node) -> Any:
    key = f"Fn::{tag_suffix}" if tag_suffix not in {"Ref", "Condition"} else tag_suffix
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
        if tag_suffix == "GetAtt" and isinstance(value, str):
            value = value.split(".")
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        raise yaml.constructor.ConstructorError(
            None, None, f"unsupported node type for !{tag_suffix}", node.start_mark
        )
    return {key: value}


CfnLoader.add_multi_constructor("!", _construct_intrinsic)


@dataclass
class Report:
    failures: list[str] = field(default_factory=list)

    def fail(self, where: str, message: str) -> None:
        self.failures.append(f"{where}: {message}")

    @property
    def ok(self) -> bool:
        return not self.failures


def load_yaml(path: Path, *, cfn: bool = False) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=CfnLoader if cfn else yaml.SafeLoader)  # noqa: S506


def walk(node: Any) -> Any:
    """Yield every mapping and list nested anywhere in a parsed template."""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def resources_of_type(template: dict[str, Any], type_name: str) -> dict[str, dict[str, Any]]:
    resources = template.get("Resources") or {}
    return {
        name: body
        for name, body in resources.items()
        if isinstance(body, dict) and body.get("Type") == type_name
    }


# --- Terraform -----------------------------------------------------------------

# Deliberately a regex rather than an HCL parser. A parser is a dependency for
# one field, and the shape being matched is fixed by this repo's own convention.
# If this ever fails to find the default, the error below says exactly which
# block to look at rather than silently passing.
_TF_VARIABLE_BLOCK = re.compile(
    r'variable\s+"(?P<name>[a-z0-9_]+)"\s*\{(?P<body>.*?)\n\}', re.DOTALL
)
_TF_DEFAULT = re.compile(r'^\s*default\s*=\s*"(?P<value>[^"]*)"\s*$', re.MULTILINE)


def terraform_variable_default(terraform_dir: Path, variable: str) -> str | None:
    """The string default of one Terraform variable, or None if not found."""
    for tf_file in sorted(terraform_dir.glob("*.tf")):
        for match in _TF_VARIABLE_BLOCK.finditer(tf_file.read_text(encoding="utf-8")):
            if match.group("name") != variable:
                continue
            default = _TF_DEFAULT.search(match.group("body"))
            return default.group("value") if default else None
    return None


# --- Manifest checks -----------------------------------------------------------


def check_manifest(example_dir: Path, validator: Draft202012Validator, report: Report) -> Any:
    where = f"examples/{example_dir.name}/example.yaml"
    manifest_path = example_dir / "example.yaml"
    if not manifest_path.is_file():
        report.fail(f"examples/{example_dir.name}", "example.yaml is missing")
        return None

    try:
        manifest = load_yaml(manifest_path)
    except yaml.YAMLError as exc:
        report.fail(where, f"is not valid YAML: {exc}")
        return None

    # An empty file parses to None, and a list parses to a list. Both would
    # crash on manifest.get() below rather than reporting a useful failure.
    if not isinstance(manifest, dict):
        report.fail(where, "does not parse to a mapping; it should be a YAML object")
        return None

    for error in sorted(validator.iter_errors(manifest), key=lambda e: list(e.path)):
        location = "/".join(str(part) for part in error.path) or "(root)"
        report.fail(where, f"{location}: {error.message}")

    if manifest.get("name") != example_dir.name:
        report.fail(
            where,
            f"name is {manifest.get('name')!r} but the directory is {example_dir.name!r}; "
            f"they must match because the name is also the release tag and the CI matrix key",
        )

    release = manifest.get("release") or {}
    if release and release.get("component") != manifest.get("name"):
        report.fail(
            where,
            f"release.component is {release.get('component')!r} but must equal name "
            f"{manifest.get('name')!r}, because it is also the required commit scope",
        )

    return manifest


def check_file_set(example_dir: Path, manifest: dict[str, Any], report: Report) -> None:
    where = f"examples/{example_dir.name}"
    if manifest.get("status") == "planned":
        return

    for name in REQUIRED_FILES:
        if not (example_dir / name).is_file():
            report.fail(where, f"{name} is missing (only status: planned is exempt)")

    deliverable = manifest.get("deliverable", "")
    for name in REQUIRED_DIRS_BY_DELIVERABLE.get(deliverable, ()):
        if not (example_dir / name).is_dir():
            report.fail(where, f"{name}/ is missing, required for deliverable {deliverable!r}")

    if manifest.get("platform") == "aws" and deliverable == "lambda-zip":
        for key in ("terraform", "cloudformation"):
            configured = (manifest.get("iac") or {}).get(key)
            if not configured:
                report.fail(where, f"iac.{key} is not set; both IaC paths are required")
            elif not (example_dir / configured).exists():
                report.fail(where, f"iac.{key} points at {configured!r}, which does not exist")


def check_runtime_agreement(
    example_dir: Path, manifest: dict[str, Any], conformance: dict[str, Any], report: Report
) -> None:
    """The runtime must be identical in the manifest, the Terraform and the CFN.

    This is the whole point of pinning the runtime in one place. A bump applied
    to the manifest but not the Terraform deploys the old runtime and nothing
    complains.
    """
    where = f"examples/{example_dir.name}"
    declared = (manifest.get("runtime") or {}).get("identifier")
    if declared is None:
        return

    ga = conformance["runtimes"]["ga"]
    preview = conformance["runtimes"]["preview"]
    if declared not in ga:
        hint = (
            " (that is a preview runtime, which is never the default)"
            if declared in preview
            else ""
        )
        report.fail(
            where,
            f"runtime.identifier {declared!r} is not a GA runtime in "
            f"common/cloudformation/conformance.yaml{hint}",
        )

    # A planned example has no IaC yet, so there is nothing to agree with. The
    # identifier itself is still checked above, so a planned example cannot
    # declare a runtime that has already been deprecated.
    if manifest.get("status") == "planned":
        return

    tf_dir = example_dir / ((manifest.get("iac") or {}).get("terraform") or "terraform")
    if tf_dir.is_dir():
        tf_default = terraform_variable_default(tf_dir, "runtime")
        if tf_default is None:
            report.fail(
                where,
                f'{tf_dir.name}/ has no variable "runtime" with a string default; '
                f"the runtime must be a variable, never hardcoded",
            )
        elif tf_default != declared:
            report.fail(
                where,
                f'terraform variable "runtime" defaults to {tf_default!r} but '
                f"example.yaml says {declared!r}",
            )

    cfn_path = example_dir / ((manifest.get("iac") or {}).get("cloudformation") or "")
    if cfn_path.is_file():
        template = load_yaml(cfn_path, cfn=True)
        parameter = (template.get("Parameters") or {}).get("LambdaRuntime") or {}
        cfn_default = parameter.get("Default")
        if cfn_default != declared:
            report.fail(
                where,
                f"CloudFormation parameter LambdaRuntime defaults to {cfn_default!r} "
                f"but example.yaml says {declared!r}",
            )


# --- CloudFormation conformance ------------------------------------------------


def check_template(path: Path, conformance: dict[str, Any], report: Report) -> None:
    where = str(path.relative_to(REPO_ROOT))
    try:
        template = load_yaml(path, cfn=True)
    except yaml.YAMLError as exc:
        report.fail(where, f"is not valid YAML: {exc}")
        return
    if not isinstance(template, dict):
        report.fail(where, "does not parse to a mapping")
        return

    parameters: dict[str, Any] = template.get("Parameters") or {}
    outputs: dict[str, Any] = template.get("Outputs") or {}
    resources: dict[str, Any] = template.get("Resources") or {}
    present_types = {body.get("Type") for body in resources.values() if isinstance(body, dict)}

    for name in conformance["required_parameters"]:
        if name not in parameters:
            report.fail(where, f"parameter {name} is required by conformance.yaml")

    for name in conformance["required_outputs"]:
        if name not in outputs:
            report.fail(where, f"output {name} is required by conformance.yaml")

    for type_name in conformance["required_resource_types"]:
        if type_name not in present_types:
            report.fail(where, f"no {type_name} resource; conformance.yaml requires one")

    _check_secret_defaults(where, parameters, conformance, report)
    _check_runtime_parameter(where, parameters, conformance, report)
    _check_preview_gate(where, template, parameters, present_types, conformance, report)
    _check_log_retention(where, parameters, resources, conformance, report)
    _check_partial_batch_failure(where, template, present_types, report)
    _check_dead_letter_queue(where, template, present_types, report)
    _check_iam_wildcards(where, template, report)
    _check_environment_secrets(where, template, conformance, report)


def _check_secret_defaults(
    where: str, parameters: dict[str, Any], conformance: dict[str, Any], report: Report
) -> None:
    patterns = [re.compile(p) for p in conformance["secret_parameter_patterns"]]
    for name, spec in parameters.items():
        if not isinstance(spec, dict):
            continue
        if not any(pattern.search(name) for pattern in patterns):
            continue
        default = spec.get("Default")
        if default not in (None, ""):
            report.fail(
                where,
                f"parameter {name} looks like a credential and has a non-empty default. "
                f"A default credential is either a real one committed by accident, or a "
                f"placeholder that deploys and then 401s at runtime.",
            )


def _check_runtime_parameter(
    where: str, parameters: dict[str, Any], conformance: dict[str, Any], report: Report
) -> None:
    rule = conformance["rules"]["runtime_parameter"]
    spec = parameters.get(rule["parameter"])
    if not isinstance(spec, dict):
        return

    allowed = spec.get("AllowedValues")
    if rule["require_allowed_values"] and not allowed:
        report.fail(
            where,
            f"{rule['parameter']} has no AllowedValues. A free-text runtime lets a typo "
            f"through to a stack failure with an opaque message.",
        )
        return

    ga = conformance["runtimes"]["ga"]
    preview = conformance["runtimes"]["preview"]
    known = set(ga) | set(preview)
    for value in allowed or []:
        if value not in known:
            report.fail(
                where,
                f"{rule['parameter']} offers {value!r}, which is not listed under "
                f"runtimes in conformance.yaml",
            )

    default = spec.get("Default")
    if rule["default_must_be_ga"] and default not in ga:
        report.fail(
            where,
            f"{rule['parameter']} defaults to {default!r}, which is not a GA runtime. "
            f"A preview runtime has no SLA and no support, so it is never the default.",
        )


def _check_preview_gate(
    where: str,
    template: dict[str, Any],
    parameters: dict[str, Any],
    present_types: set[Any],
    conformance: dict[str, Any],
    report: Report,
) -> None:
    rule = conformance["rules"]["preview_runtime_gate"]
    offered = set((parameters.get("LambdaRuntime") or {}).get("AllowedValues") or [])
    if not offered & set(conformance["runtimes"]["preview"]):
        return

    gate = rule["requires_parameter"]
    if gate not in parameters:
        report.fail(
            where, f"offers a preview runtime but has no {gate} parameter to gate it behind"
        )
        return

    conditions = template.get("Conditions") or {}
    references_gate = any(
        isinstance(node, dict) and node.get("Ref") == gate
        for condition in conditions.values()
        for node in walk(condition)
    )
    if not references_gate:
        report.fail(where, f"no Condition references {gate}, so the gate does nothing")

    if rule["requires_resource_type"] not in present_types:
        report.fail(
            where,
            f"has no {rule['requires_resource_type']} guard, so selecting a preview "
            f"runtime without {gate} would deploy rather than fail",
        )


def _check_log_retention(
    where: str,
    parameters: dict[str, Any],
    resources: dict[str, Any],
    conformance: dict[str, Any],
    report: Report,
) -> None:
    rule = conformance["rules"]["log_retention"]
    spec = parameters.get(rule["parameter"])
    if isinstance(spec, dict):
        allowed = spec.get("AllowedValues")
        if rule["require_allowed_values"] and not allowed:
            report.fail(where, f"{rule['parameter']} has no AllowedValues")
        if allowed and rule["forbid_allowed_value"] in allowed:
            report.fail(
                where,
                f"{rule['parameter']} allows {rule['forbid_allowed_value']} (never expire), "
                f"which is a slow-growing bill nobody notices",
            )

    for name, body in resources.items():
        if not isinstance(body, dict) or body.get("Type") != "AWS::Logs::LogGroup":
            continue
        if "RetentionInDays" not in (body.get("Properties") or {}):
            report.fail(where, f"log group {name} has no RetentionInDays, so it never expires")


def _check_partial_batch_failure(
    where: str, template: dict[str, Any], present_types: set[Any], report: Report
) -> None:
    if "AWS::Lambda::EventSourceMapping" not in present_types:
        return
    for name, body in resources_of_type(template, "AWS::Lambda::EventSourceMapping").items():
        response_types = (body.get("Properties") or {}).get("FunctionResponseTypes") or []
        if "ReportBatchItemFailures" not in response_types:
            report.fail(
                where,
                f"event source mapping {name} does not declare ReportBatchItemFailures. "
                f"Without it Lambda ignores the handler's batchItemFailures response and "
                f"redelivers the whole batch, duplicating every line already shipped.",
            )


def _check_dead_letter_queue(
    where: str, template: dict[str, Any], present_types: set[Any], report: Report
) -> None:
    if "AWS::SQS::Queue" not in present_types:
        return
    queues = resources_of_type(template, "AWS::SQS::Queue")
    if not any("RedrivePolicy" in (body.get("Properties") or {}) for body in queues.values()):
        report.fail(
            where,
            "creates an SQS queue with no RedrivePolicy on any of them, so a repeatedly "
            "failing message is retried forever or dropped rather than landing in a DLQ",
        )


def _check_iam_wildcards(where: str, template: dict[str, Any], report: Report) -> None:
    for node in walk(template):
        if not isinstance(node, dict):
            continue
        if "Effect" not in node or node.get("Effect") != "Allow":
            continue
        for key in ("Resource", "Action"):
            value = node.get(key)
            values = value if isinstance(value, list) else [value]
            if "*" in values:
                report.fail(
                    where,
                    f'an IAM statement has {key}: "*". A reference example gets copied '
                    f"into production by someone who trusted it; scope it.",
                )


def _check_environment_secrets(
    where: str, template: dict[str, Any], conformance: dict[str, Any], report: Report
) -> None:
    patterns = [
        re.compile(p)
        for p in conformance["rules"]["no_credentials_in_environment"][
            "forbid_environment_variable_patterns"
        ]
    ]
    for name, body in resources_of_type(template, "AWS::Lambda::Function").items():
        variables = ((body.get("Properties") or {}).get("Environment") or {}).get("Variables") or {}
        for key in variables:
            if any(pattern.search(key) for pattern in patterns):
                report.fail(
                    where,
                    f"function {name} sets environment variable {key}, which names a "
                    f"credential. Environment variables are readable by anyone with "
                    f"lambda:GetFunctionConfiguration; pass a Secrets Manager id instead.",
                )


# --- Entry points --------------------------------------------------------------


def discover_examples() -> list[Path]:
    if not EXAMPLES_DIR.is_dir():
        return []
    return sorted(p for p in EXAMPLES_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))


def print_listing() -> int:
    rows: list[tuple[str, str, str, str]] = []
    for example_dir in discover_examples():
        manifest_path = example_dir / "example.yaml"
        if not manifest_path.is_file():
            rows.append((example_dir.name, "(no manifest)", "-", "-"))
            continue
        manifest = load_yaml(manifest_path) or {}
        rows.append(
            (
                str(manifest.get("name", example_dir.name)),
                str(manifest.get("status", "?")),
                str((manifest.get("runtime") or {}).get("identifier", "-")),
                str(manifest.get("title", "")),
            )
        )

    if not rows:
        print("No examples yet. `just new-example <name>` creates one.")
        return 0

    widths = [max(len(row[index]) for row in rows) for index in range(3)]
    header = ("EXAMPLE", "STATUS", "RUNTIME", "TITLE")
    widths = [max(widths[index], len(header[index])) for index in range(3)]
    print(
        f"{header[0]:<{widths[0]}}  {header[1]:<{widths[1]}}  {header[2]:<{widths[2]}}  {header[3]}"
    )
    for row in rows:
        print(f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]:<{widths[2]}}  {row[3]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the example table and exit")
    parser.add_argument(
        "--cloudformation-only",
        action="store_true",
        help="check only the CloudFormation conformance rules",
    )
    args = parser.parse_args(argv)

    if args.list:
        return print_listing()

    conformance = load_yaml(CONFORMANCE_PATH)
    report = Report()

    templates = sorted(
        [
            *(REPO_ROOT / "common" / "cloudformation" / "templates").glob("*.yaml"),
            *EXAMPLES_DIR.glob("*/cloudformation/*.yaml"),
        ]
    )
    for template_path in templates:
        check_template(template_path, conformance, report)

    if not args.cloudformation_only:
        validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
        for example_dir in discover_examples():
            manifest = check_manifest(example_dir, validator, report)
            if manifest is None:
                continue
            check_file_set(example_dir, manifest, report)
            check_runtime_agreement(example_dir, manifest, conformance, report)

    if report.ok:
        scope = "cloudformation templates" if args.cloudformation_only else "examples"
        print(f"conformance: {len(templates)} templates, {len(discover_examples())} {scope}: ok")
        return 0

    print(f"conformance: {len(report.failures)} failure(s)\n", file=sys.stderr)
    for failure in report.failures:
        print(f"  {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
