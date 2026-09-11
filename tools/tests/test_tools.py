"""Tests for the build tools.

These carry real logic with real branching - a zip that is not reproducible, a
module source the vendoring silently misses, or a conformance rule that passes a
bad template are all failures nobody would notice until a customer hit them.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_conformance as conformance
import new_example
import package_example as packager

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class TestDeterministicZip:
    def test_two_builds_of_the_same_tree_are_byte_identical(self, tmp_path: Path) -> None:
        """Without this, source_code_hash changes on every build and Terraform
        redeploys unchanged code."""
        source = tmp_path / "src"
        (source / "pkg").mkdir(parents=True)
        (source / "pkg" / "a.py").write_text("print('a')\n")
        (source / "b.txt").write_text("b\n")

        first = tmp_path / "first.zip"
        second = tmp_path / "second.zip"
        packager.write_deterministic_zip(source, first)
        packager.write_deterministic_zip(source, second)

        assert first.read_bytes() == second.read_bytes()

    def test_entries_carry_the_fixed_timestamp(self, tmp_path: Path) -> None:
        source = tmp_path / "src"
        source.mkdir()
        (source / "a.txt").write_text("a\n")
        archive = tmp_path / "out.zip"
        packager.write_deterministic_zip(source, archive)

        with zipfile.ZipFile(archive) as handle:
            assert handle.getinfo("a.txt").date_time == packager.FIXED_TIMESTAMP

    def test_prefix_is_applied_to_every_entry(self, tmp_path: Path) -> None:
        source = tmp_path / "src"
        source.mkdir()
        (source / "a.txt").write_text("a\n")
        archive = tmp_path / "out.zip"
        packager.write_deterministic_zip(source, archive, prefix="bundle-1.0/")

        with zipfile.ZipFile(archive) as handle:
            assert handle.namelist() == ["bundle-1.0/a.txt"]


class TestModuleSourceRewrite:
    @pytest.mark.parametrize(
        ("given", "expected_module"),
        [
            ('  source = "../../../common/terraform/modules/lambda-function"', "lambda-function"),
            ('    source = "../../common/terraform/modules/s3-event-source"', "s3-event-source"),
        ],
    )
    def test_matches_a_relative_shared_module(self, given: str, expected_module: str) -> None:
        match = packager._MODULE_SOURCE.search(given)
        assert match is not None
        assert match.group("module") == expected_module

    @pytest.mark.parametrize(
        "given",
        [
            '      source  = "hashicorp/aws"',
            "  source_code_hash = filebase64sha256(local.zip_path)",
            '  source = "terraform-aws-modules/lambda/aws"',
        ],
    )
    def test_does_not_match_a_provider_or_a_similarly_named_attribute(self, given: str) -> None:
        """`source_code_hash` and required_providers' `source` both start with
        "source"; an earlier version of this check matched both."""
        assert packager._MODULE_SOURCE.search(given) is None

    def test_module_block_scan_finds_only_module_sources(self) -> None:
        body = """
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 7.0"
    }
  }
}

module "function" {
  source = "./modules/lambda-function"
  name   = "x"
}
"""
        blocks = list(packager._MODULE_BLOCK.finditer(body))
        assert len(blocks) == 1
        found = packager._SOURCE_ATTRIBUTE.search(blocks[0].group("body"))
        assert found is not None
        assert found.group("value") == "./modules/lambda-function"


class TestDistributionName:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("boto3==1.43.92", "boto3"),
            ("python-dateutil==2.9.0.post0", "python-dateutil"),
            ("urllib3==2.7.0 ; python_version >= '3.10'", "urllib3"),
            ("boto3[crt]==1.43.92", "boto3"),
        ],
    )
    def test_parses_a_requirements_line(self, line: str, expected: str) -> None:
        assert packager._distribution_name(line) == expected


class TestCfnLoader:
    def test_short_form_intrinsics_become_the_long_form(self, tmp_path: Path) -> None:
        """A plain SafeLoader dies on !Ref, so every conformance rule would be
        unreachable without this."""
        path = tmp_path / "t.yaml"
        path.write_text(
            "Resources:\n"
            "  A:\n"
            "    Type: AWS::IAM::Role\n"
            "    Properties:\n"
            "      Name: !Ref Thing\n"
            "      Arn: !GetAtt Other.Arn\n"
            "      Text: !Sub 'x-${Thing}'\n"
        )
        loaded = conformance.load_yaml(path, cfn=True)
        properties = loaded["Resources"]["A"]["Properties"]
        assert properties["Name"] == {"Ref": "Thing"}
        assert properties["Arn"] == {"Fn::GetAtt": ["Other", "Arn"]}
        assert properties["Text"] == {"Fn::Sub": "x-${Thing}"}


class TestConformanceRules:
    @pytest.fixture
    def rules(self) -> dict[str, Any]:
        loaded: dict[str, Any] = conformance.load_yaml(conformance.CONFORMANCE_PATH)
        return loaded

    def test_a_credential_parameter_with_a_default_fails(self, rules: dict[str, Any]) -> None:
        report = conformance.Report()
        conformance._check_secret_defaults(
            "t.yaml", {"GrafanaCloudToken": {"Type": "String", "Default": "glc_abc"}}, rules, report
        )
        assert not report.ok
        assert "looks like a credential" in report.failures[0]

    def test_a_credential_parameter_with_no_default_passes(self, rules: dict[str, Any]) -> None:
        report = conformance.Report()
        conformance._check_secret_defaults(
            "t.yaml", {"GrafanaCloudToken": {"Type": "String"}}, rules, report
        )
        assert report.ok

    def test_credentials_secret_id_is_not_treated_as_a_secret(self, rules: dict[str, Any]) -> None:
        """It names where the credential lives, not the credential, so it is
        allowed a default."""
        report = conformance.Report()
        conformance._check_secret_defaults(
            "t.yaml",
            {"CredentialsSecretId": {"Type": "String", "Default": "grafana-cloud/loki"}},
            rules,
            report,
        )
        assert report.ok

    def test_a_preview_default_runtime_fails(self, rules: dict[str, Any]) -> None:
        report = conformance.Report()
        conformance._check_runtime_parameter(
            "t.yaml",
            {
                "LambdaRuntime": {
                    "Default": "python3.15",
                    "AllowedValues": ["python3.14", "python3.15"],
                }
            },
            rules,
            report,
        )
        assert not report.ok
        assert "not a GA runtime" in report.failures[0]

    def test_a_free_text_runtime_fails(self, rules: dict[str, Any]) -> None:
        report = conformance.Report()
        conformance._check_runtime_parameter(
            "t.yaml", {"LambdaRuntime": {"Default": "python3.14"}}, rules, report
        )
        assert not report.ok
        assert "no AllowedValues" in report.failures[0]

    def test_an_unknown_offered_runtime_fails(self, rules: dict[str, Any]) -> None:
        report = conformance.Report()
        conformance._check_runtime_parameter(
            "t.yaml",
            {"LambdaRuntime": {"Default": "python3.14", "AllowedValues": ["python3.14", "go1.x"]}},
            rules,
            report,
        )
        assert not report.ok
        assert "go1.x" in report.failures[0]

    def test_never_expire_log_retention_fails(self, rules: dict[str, Any]) -> None:
        report = conformance.Report()
        conformance._check_log_retention(
            "t.yaml", {"LogRetentionInDays": {"AllowedValues": [0, 30]}}, {}, rules, report
        )
        assert not report.ok
        assert "never expire" in report.failures[0]

    def test_a_log_group_without_retention_fails(self, rules: dict[str, Any]) -> None:
        report = conformance.Report()
        conformance._check_log_retention(
            "t.yaml",
            {"LogRetentionInDays": {"AllowedValues": [30]}},
            {"Logs": {"Type": "AWS::Logs::LogGroup", "Properties": {"LogGroupName": "x"}}},
            rules,
            report,
        )
        assert not report.ok
        assert "never expires" in report.failures[0]

    def test_an_event_source_mapping_without_partial_failure_reporting_fails(self) -> None:
        report = conformance.Report()
        template = {
            "Resources": {"Mapping": {"Type": "AWS::Lambda::EventSourceMapping", "Properties": {}}}
        }
        conformance._check_partial_batch_failure(
            "t.yaml", template, {"AWS::Lambda::EventSourceMapping"}, report
        )
        assert not report.ok
        assert "ReportBatchItemFailures" in report.failures[0]

    def test_a_queue_with_no_redrive_policy_anywhere_fails(self) -> None:
        report = conformance.Report()
        template = {"Resources": {"Q": {"Type": "AWS::SQS::Queue", "Properties": {}}}}
        conformance._check_dead_letter_queue("t.yaml", template, {"AWS::SQS::Queue"}, report)
        assert not report.ok
        assert "RedrivePolicy" in report.failures[0]

    @pytest.mark.parametrize("key", ["Resource", "Action"])
    def test_a_wildcard_iam_statement_fails(self, key: str) -> None:
        report = conformance.Report()
        conformance._check_iam_wildcards(
            "t.yaml", {"Doc": {"Effect": "Allow", key: "*", "Sid": "x"}}, report
        )
        assert not report.ok

    def test_a_scoped_iam_statement_passes(self) -> None:
        report = conformance.Report()
        conformance._check_iam_wildcards(
            "t.yaml",
            {
                "Doc": {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject"],
                    "Resource": ["arn:aws:s3:::b/*"],
                }
            },
            report,
        )
        assert report.ok

    def test_a_token_environment_variable_fails(self, rules: dict[str, Any]) -> None:
        report = conformance.Report()
        template = {
            "Resources": {
                "Fn": {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {"Environment": {"Variables": {"GRAFANA_CLOUD_TOKEN": "x"}}},
                }
            }
        }
        conformance._check_environment_secrets("t.yaml", template, rules, report)
        assert not report.ok
        assert "readable by anyone" in report.failures[0]

    def test_a_secret_id_environment_variable_passes(self, rules: dict[str, Any]) -> None:
        report = conformance.Report()
        template = {
            "Resources": {
                "Fn": {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {
                        "Environment": {
                            "Variables": {"GRAFANA_CLOUD_CREDENTIALS_SECRET_ID": "loki"}
                        }
                    },
                }
            }
        }
        conformance._check_environment_secrets("t.yaml", template, rules, report)
        assert report.ok


class TestTerraformVariableDefault:
    def test_reads_a_string_default(self, tmp_path: Path) -> None:
        (tmp_path / "variables.tf").write_text(
            'variable "runtime" {\n'
            "  description = <<-EOT\n"
            "    Multi-line, containing the word default to be awkward.\n"
            "  EOT\n"
            "  type        = string\n"
            '  default     = "python3.14"\n'
            "}\n"
        )
        assert conformance.terraform_variable_default(tmp_path, "runtime") == "python3.14"

    def test_returns_none_when_the_variable_has_no_default(self, tmp_path: Path) -> None:
        (tmp_path / "variables.tf").write_text('variable "runtime" {\n  type = string\n}\n')
        assert conformance.terraform_variable_default(tmp_path, "runtime") is None

    def test_returns_none_when_the_variable_is_absent(self, tmp_path: Path) -> None:
        (tmp_path / "variables.tf").write_text('variable "other" {\n  default = "x"\n}\n')
        assert conformance.terraform_variable_default(tmp_path, "runtime") is None


class TestScaffolder:
    @pytest.fixture(autouse=True)
    def isolated_examples_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Scaffold into a temp directory, never into the real examples/."""
        staged = tmp_path / "examples"
        staged.mkdir()
        for name in (new_example.TEMPLATE_EXAMPLE,):
            (staged / name).mkdir()
            for sub in ("terraform", "cloudformation"):
                (staged / name / sub).mkdir()
            for tf in sorted((REPO_ROOT / "examples" / name / "terraform").glob("*.tf")):
                (staged / name / "terraform" / tf.name).write_text(tf.read_text())
            source = REPO_ROOT / "examples" / name / "cloudformation" / "parameters.example.json"
            (staged / name / "cloudformation" / "parameters.example.json").write_text(
                source.read_text()
            )
        monkeypatch.setattr(new_example, "EXAMPLES_DIR", staged)
        return staged

    def test_rejects_an_invalid_name(self) -> None:
        assert new_example.main(["Not_Valid"]) == 1

    def test_refuses_to_overwrite(self, isolated_examples_dir: Path) -> None:
        (isolated_examples_dir / "taken").mkdir()
        assert new_example.main(["taken"]) == 1

    def test_produces_a_schema_valid_manifest(self, isolated_examples_dir: Path) -> None:
        assert new_example.main(["my-thing"]) == 0
        manifest = yaml.safe_load((isolated_examples_dir / "my-thing" / "example.yaml").read_text())
        assert manifest["name"] == "my-thing"
        assert manifest["status"] == "planned"
        assert manifest["release"]["component"] == "my-thing"
        # Must be a GA runtime: a scaffolded example should not start life on a
        # preview runtime or one that is nearly deprecated.
        rules = conformance.load_yaml(conformance.CONFORMANCE_PATH)
        assert manifest["runtime"]["identifier"] in rules["runtimes"]["ga"]

    def test_converts_the_name_to_a_python_module(self, isolated_examples_dir: Path) -> None:
        assert new_example.main(["my-thing"]) == 0
        handler = isolated_examples_dir / "my-thing" / "src" / "my_thing" / "handler.py"
        assert handler.is_file()
        assert "my_thing.handler.lambda_handler" in handler.read_text()

    def test_the_generated_handler_is_syntactically_valid(
        self, isolated_examples_dir: Path
    ) -> None:
        assert new_example.main(["my-thing"]) == 0
        handler = isolated_examples_dir / "my-thing" / "src" / "my_thing" / "handler.py"
        compile(handler.read_text(), str(handler), "exec")

    def test_rejects_a_runtime_it_cannot_scaffold_yet(self, isolated_examples_dir: Path) -> None:
        """Better a clear refusal than a half-written Node example."""
        assert new_example.main(["my-thing", "--runtime", "nodejs"]) == 1
        assert not (isolated_examples_dir / "my-thing").exists()


class TestReadmeContract:
    """The example README checks.

    These exist because the failures they catch are invisible in the repository
    and only appear for the customer: a link that works here and not in the zip,
    and a `terraform output` that does not exist.
    """

    HEADINGS = "\n".join(conformance.REQUIRED_README_HEADINGS)

    def _example(self, tmp_path: Path, readme: str, outputs: str = "") -> Path:
        example = tmp_path / "an-example"
        (example / "terraform").mkdir(parents=True)
        (example / "README.md").write_text(readme, encoding="utf-8")
        if outputs:
            (example / "terraform" / "outputs.tf").write_text(outputs, encoding="utf-8")
        return example

    def test_the_full_heading_set_passes(self, tmp_path: Path) -> None:
        report = conformance.Report()
        example = self._example(tmp_path, f"# Title\n\n{self.HEADINGS}\n")
        conformance.check_readme(example, {"status": "alpha"}, report)
        assert report.ok, report.failures

    def test_a_missing_heading_fails(self, tmp_path: Path) -> None:
        without = self.HEADINGS.replace("## Troubleshooting\n", "")
        report = conformance.Report()
        conformance.check_readme(
            self._example(tmp_path, f"# Title\n\n{without}\n"), {"status": "alpha"}, report
        )
        assert not report.ok
        assert "## Troubleshooting" in report.failures[0]

    def test_headings_out_of_order_fail(self, tmp_path: Path) -> None:
        """A customer who has read one example navigates the next by shape."""
        reordered = list(conformance.REQUIRED_README_HEADINGS)
        reordered[0], reordered[-1] = reordered[-1], reordered[0]
        report = conformance.Report()
        conformance.check_readme(
            self._example(tmp_path, "# Title\n\n" + "\n".join(reordered) + "\n"),
            {"status": "alpha"},
            report,
        )
        assert not report.ok
        assert "out of order" in " ".join(report.failures)

    def test_a_planned_example_is_exempt_from_the_headings(self, tmp_path: Path) -> None:
        report = conformance.Report()
        conformance.check_readme(
            self._example(tmp_path, "# Title\n\nNot written yet.\n"),
            {"status": "planned"},
            report,
        )
        assert report.ok, report.failures

    @pytest.mark.parametrize(
        "link",
        [
            "](../../docs/loki-ingestion.md)",
            "](../generic-s3)",
            "](  ../../LICENSE)",
            "](<../../docs/x.md>)",
        ],
    )
    def test_a_link_escaping_the_example_fails(self, tmp_path: Path, link: str) -> None:
        """The bundle holds one README and no docs/, so these are dead on delivery."""
        report = conformance.Report()
        conformance.check_readme(
            self._example(tmp_path, f"# Title\n\n{self.HEADINGS}\n\n[x{link}\n"),
            {"status": "alpha"},
            report,
        )
        assert not report.ok
        assert "climbs out of the example directory" in " ".join(report.failures)

    @pytest.mark.parametrize(
        ("target", "escapes"),
        [
            ("../../docs/x.md", True),
            # Normalised, not pattern-matched: neither of the next two starts
            # with `../`, and both escape.
            ("./../../docs/x.md", True),
            ("../generic-s3", True),
            ("../x.md#frag", True),
            ("./terraform/main.tf", False),
            ("terraform/main.tf", False),
            ("dashboards/x.json#L3", False),
            ("#anchor", False),
            ("https://example.com/a", False),
            ("mailto:a@b.c", False),
            ("/abs/path", False),
            ("", False),
        ],
    )
    def test_link_target_normalisation(self, target: str, escapes: bool) -> None:
        assert conformance._escapes_example_directory(target) is escapes

    @pytest.mark.parametrize("link", ["](./terraform/main.tf)", "](https://example.com/x)"])
    def test_links_that_survive_the_zip_pass(self, tmp_path: Path, link: str) -> None:
        report = conformance.Report()
        conformance.check_readme(
            self._example(tmp_path, f"# Title\n\n{self.HEADINGS}\n\n[x{link}\n"),
            {"status": "alpha"},
            report,
        )
        assert report.ok, report.failures

    def test_a_planned_example_is_not_exempt_from_the_link_rule(self, tmp_path: Path) -> None:
        """A broken link is broken whether or not the code exists yet."""
        report = conformance.Report()
        conformance.check_readme(
            self._example(tmp_path, "# Title\n\n[x](../../docs/x.md)\n"),
            {"status": "planned"},
            report,
        )
        assert not report.ok

    def test_an_undeclared_terraform_output_fails(self, tmp_path: Path) -> None:
        report = conformance.Report()
        example = self._example(
            tmp_path,
            f"# Title\n\n{self.HEADINGS}\n\n`terraform output -raw dlq_arn`\n",
            outputs='output "dlq_url" {\n  value = 1\n}\n',
        )
        conformance.check_readme(example, {"status": "alpha"}, report)
        assert not report.ok
        assert "dlq_arn" in report.failures[0]

    def test_a_declared_terraform_output_passes(self, tmp_path: Path) -> None:
        report = conformance.Report()
        example = self._example(
            tmp_path,
            f"# Title\n\n{self.HEADINGS}\n\n`terraform output -raw dlq_arn`\n",
            outputs='output "dlq_arn" {\n  value = 1\n}\n',
        )
        conformance.check_readme(example, {"status": "alpha"}, report)
        assert report.ok, report.failures


class TestRootReadmeTable:
    def test_the_real_table_agrees_with_every_manifest(self) -> None:
        """It said adobe-aem was `planned` long after it was not."""
        report = conformance.Report()
        conformance.check_root_readme(report)
        assert report.ok, report.failures

    def test_the_row_regex_reads_name_runtime_and_status(self) -> None:
        row = "| [`generic-s3`](examples/generic-s3) | Ships things | `python3.14` | alpha |"
        match = conformance._ROOT_TABLE_ROW.search(row)
        assert match is not None
        assert match.group("name") == "generic-s3"
        assert match.group("runtime") == "python3.14"
        assert match.group("status") == "alpha"

    def test_a_row_whose_link_does_not_match_its_name_is_not_read(self) -> None:
        """Guards against a copy-paste that points one example's row at another."""
        row = "| [`generic-s3`](examples/adobe-aem) | x | `python3.14` | alpha |"
        assert conformance._ROOT_TABLE_ROW.search(row) is None


class TestRepoState:
    def test_conformance_passes_on_the_real_repo(self) -> None:
        """The check that keeps every other check honest."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "check_conformance.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
