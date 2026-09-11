#!/usr/bin/env python3
"""Build one example's release bundle.

Run through `just package <name>` or `just package-all`, never directly.

Output is `dist/<name>-<version>.zip`, containing everything a customer needs
and nothing outside itself:

    <name>-<version>/
      README.md
      LICENSE
      MANIFEST.json          what was built, from what, and with which hashes
      lambda.zip             the deployment package
      terraform/             the example's root module
        modules/<m>/         shared modules, vendored, sources rewritten
        lambda.zip           a copy, so `terraform apply` needs no configuration
      cloudformation/        the flat template and its example parameters

Two things here matter more than they look:

* **Vendoring with a source rewrite.** In-repo a module source is
  `../../../common/terraform/modules/<m>`, which escapes the example directory.
  The rewrite makes it `./modules/<m>` so the bundle is self-contained.

* **Deterministic zips.** Every entry gets a fixed timestamp and fixed
  permissions, so building the same commit twice produces byte-identical
  archives and the same SHA-256. Without that, `source_code_hash` changes on
  every build and Terraform redeploys unchanged code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
COMMON_PYTHON_SRC = REPO_ROOT / "common" / "python" / "src"
COMMON_TF_MODULES = REPO_ROOT / "common" / "terraform" / "modules"

# A fixed DOS timestamp (1980-01-01 00:00:00), the earliest a zip can express.
# Any fixed value works; this one is the conventional choice.
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

# Test doubles are part of the library for the repo's own tests, but shipping
# them puts pytest-shaped code in a customer's Lambda.
VENDOR_EXCLUDE = {"testing.py"}

EXCLUDE_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".terraform",
    "node_modules",
}
EXCLUDE_FILE_SUFFIXES = {".pyc", ".pyo", ".tfstate", ".tfstate.backup"}

# Matches a relative module source pointing into common/terraform/modules.
_MODULE_SOURCE = re.compile(
    r'^(?P<indent>\s*)source\s*=\s*"(?:\.\./)+common/terraform/modules/(?P<module>[a-z0-9-]+)"\s*$',
    re.MULTILINE,
)

# A `module "name" { ... }` block and its body. Scoped deliberately: `source`
# also appears inside required_providers (hashicorp/aws) and next to
# source_code_hash, neither of which is a module reference.
_MODULE_BLOCK = re.compile(r'^module\s+"[^"]+"\s*\{(?P<body>.*?)^\}', re.DOTALL | re.MULTILINE)
_SOURCE_ATTRIBUTE = re.compile(r'^\s*source\s*=\s*"(?P<value>[^"]*)"\s*$', re.MULTILINE)

# uv's platform tags for the two Lambda architectures. Cross-installing with
# these means a Linux-targeted wheel set is built on macOS without Docker.
UV_PLATFORM = {
    "arm64": "aarch64-manylinux2014",
    "x86_64": "x86_64-manylinux2014",
}

# Distributions the Lambda Python runtime already provides. Excluding them takes
# a deployment package from ~16 MB to ~50 KB, which is a real difference to
# upload time and to how readable the artefact is.
#
# AWS recommends bundling the SDK so an automatic runtime update cannot change
# its version under you, and that recommendation is right for a function that
# leans on recent or unusual SDK behaviour. An example that only calls GetObject
# and GetSecretValue does not, so it opts out via
# `lambda.rely_on_runtime_aws_sdk` in example.yaml. The choice is per-example and
# deliberate, never a packaging default.
RUNTIME_PROVIDED_AWS_SDK = frozenset(
    {
        "boto3",
        "botocore",
        "jmespath",
        "python-dateutil",
        "s3transfer",
        "six",
        "urllib3",
    }
)


class PackagingError(RuntimeError):
    """Something about the example prevents it being packaged."""


@dataclass(frozen=True)
class Example:
    directory: Path
    manifest: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.manifest["name"])

    @property
    def version(self) -> str:
        """Version from the example's pyproject, which release-please maintains."""
        pyproject = self.directory / "pyproject.toml"
        if not pyproject.is_file():
            return "0.0.0"
        match = re.search(
            r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE
        )
        return match.group(1) if match else "0.0.0"

    @property
    def python_package_name(self) -> str:
        match = re.search(
            r'^name\s*=\s*"([^"]+)"',
            (self.directory / "pyproject.toml").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match is None:
            raise PackagingError(f"{self.name}: pyproject.toml has no name")
        return match.group(1)

    @property
    def runtime(self) -> str:
        return str((self.manifest.get("runtime") or {}).get("identifier", ""))

    @property
    def architecture(self) -> str:
        return str((self.manifest.get("runtime") or {}).get("architecture", "arm64"))

    @property
    def python_version(self) -> str:
        match = re.match(r"^python(\d+\.\d+)$", self.runtime)
        if match is None:
            raise PackagingError(
                f"{self.name}: runtime {self.runtime!r} is not a Python runtime; "
                f"packaging a {self.runtime} deliverable is not implemented"
            )
        return match.group(1)


def load_example(name: str) -> Example:
    directory = EXAMPLES_DIR / name
    manifest_path = directory / "example.yaml"
    if not manifest_path.is_file():
        raise PackagingError(f"no such example: examples/{name}/example.yaml not found")
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)
    example = Example(directory=directory, manifest=manifest)
    if manifest.get("status") == "planned":
        raise PackagingError(
            f"{name} is status: planned and has nothing to package yet. "
            f"Implement it and set a real status first."
        )
    if manifest.get("deliverable") != "lambda-zip":
        raise PackagingError(
            f"{name} has deliverable {manifest.get('deliverable')!r}; only lambda-zip "
            f"is implemented. Add the new deliverable's build here rather than "
            f"scripting it separately."
        )
    return example


# --- Copy helpers --------------------------------------------------------------


def _ignore(directory: str, names: list[str]) -> set[str]:
    del directory
    return {
        name
        for name in names
        if name in EXCLUDE_DIR_NAMES
        or any(name.endswith(suffix) for suffix in EXCLUDE_FILE_SUFFIXES)
    }


def copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(source, target, ignore=_ignore, dirs_exist_ok=True)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_deterministic_zip(source_dir: Path, target: Path, *, prefix: str = "") -> None:
    """Zip a directory reproducibly.

    Entries are sorted, timestamps fixed and permissions normalised, so two
    builds of the same tree are byte-identical. Directory entries are written
    too, because some tooling expects them.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    paths = sorted(source_dir.rglob("*"), key=lambda p: p.relative_to(source_dir).as_posix())

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in paths:
            relative = path.relative_to(source_dir).as_posix()
            arcname = f"{prefix}{relative}" if prefix else relative
            if path.is_dir():
                info = zipfile.ZipInfo(f"{arcname}/", date_time=FIXED_TIMESTAMP)
                info.external_attr = (0o040755 << 16) | 0x10
                archive.writestr(info, b"")
                continue
            info = zipfile.ZipInfo(arcname, date_time=FIXED_TIMESTAMP)
            # 0o755 on anything executable, 0o644 otherwise. Lambda needs the
            # handler module readable; nothing here needs to be executable, but
            # a shipped script would.
            mode = 0o755 if path.suffix in {".sh", ".py"} and path.stat().st_mode & 0o100 else 0o644
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())


# --- Build steps ---------------------------------------------------------------


def build_lambda_zip(example: Example, staging: Path) -> Path:
    """Assemble and zip the deployment package."""
    build_dir = staging / "lambda-build"
    build_dir.mkdir(parents=True)

    package_root = example.directory / str(example.manifest["lambda"]["package_root"])
    if not package_root.is_dir():
        raise PackagingError(f"{example.name}: {package_root} does not exist")
    copy_tree(package_root, build_dir)

    # Vendor the shared library rather than depending on a published package, so
    # one downloaded zip is the whole deliverable.
    vendored = build_dir / "grafana_cloud_common"
    copy_tree(COMMON_PYTHON_SRC / "grafana_cloud_common", vendored)
    for name in VENDOR_EXCLUDE:
        (vendored / name).unlink(missing_ok=True)

    _install_third_party(example, build_dir)

    zip_path = staging / "lambda.zip"
    write_deterministic_zip(build_dir, zip_path)
    return zip_path


def _install_third_party(example: Example, build_dir: Path) -> None:
    """Install the example's third-party dependencies for the Lambda platform.

    Resolved from uv.lock rather than from the pyproject ranges, so the bundle
    pins exactly what CI tested. Workspace members are excluded because they are
    vendored above.
    """
    platform_tag = UV_PLATFORM.get(example.architecture)
    if platform_tag is None:
        raise PackagingError(f"{example.name}: unsupported architecture {example.architecture!r}")

    export = subprocess.run(
        [
            "uv",
            "export",
            "--package",
            example.python_package_name,
            "--no-dev",
            "--no-emit-workspace",
            "--no-hashes",
            "--format",
            "requirements.txt",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    requirements = [
        line for line in export.stdout.splitlines() if line and not line.startswith("#")
    ]

    if example.manifest["lambda"].get("rely_on_runtime_aws_sdk", False):
        excluded = {
            line for line in requirements if _distribution_name(line) in RUNTIME_PROVIDED_AWS_SDK
        }
        requirements = [line for line in requirements if line not in excluded]
        if excluded:
            print(
                f"  using the runtime-provided AWS SDK; excluded "
                f"{', '.join(sorted(_distribution_name(line) for line in excluded))}"
            )

    if not requirements:
        return

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write("\n".join(requirements) + "\n")
        requirements_path = Path(handle.name)

    try:
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--target",
                str(build_dir),
                "--requirements",
                str(requirements_path),
                # Cross-install for the Lambda platform. This is what removes the
                # need for Docker or a Linux builder: a macOS developer and CI
                # produce the same wheel set.
                "--python-platform",
                platform_tag,
                "--python-version",
                example.python_version,
                "--only-binary",
                ":all:",
                "--no-installer-metadata",
                "--no-compile-bytecode",
                "--quiet",
            ],
            cwd=REPO_ROOT,
            check=True,
        )
    finally:
        requirements_path.unlink(missing_ok=True)

    # dist-info directories carry install-time metadata that differs between
    # machines, which would break reproducibility.
    for path in sorted(build_dir.glob("*.dist-info")):
        shutil.rmtree(path, ignore_errors=True)
    for name in ("bin", "__pycache__"):
        shutil.rmtree(build_dir / name, ignore_errors=True)
    # uv leaves a lock file in the target directory; it is not code.
    (build_dir / ".lock").unlink(missing_ok=True)


def _distribution_name(requirement: str) -> str:
    """Normalised distribution name from a `name==version` requirement line."""
    return re.split(r"[=<>!~\[; ]", requirement.strip(), maxsplit=1)[0].strip().lower()


def build_terraform(example: Example, bundle: Path, lambda_zip: Path) -> list[str]:
    """Copy the root module, vendor the shared modules, rewrite the sources."""
    source_dir = example.directory / (
        (example.manifest.get("iac") or {}).get("terraform") or "terraform"
    )
    if not source_dir.is_dir():
        raise PackagingError(f"{example.name}: {source_dir} does not exist")

    target_dir = bundle / "terraform"
    copy_tree(source_dir, target_dir)
    for stale in (".terraform.lock.hcl", "terraform.tfvars"):
        (target_dir / stale).unlink(missing_ok=True)

    vendored: list[str] = []
    for tf_file in sorted(target_dir.glob("*.tf")):
        original = tf_file.read_text(encoding="utf-8")

        def replace(match: re.Match[str]) -> str:
            module = match.group("module")
            if module not in vendored:
                vendored.append(module)
            return f'{match.group("indent")}source = "./modules/{module}"'

        rewritten = _MODULE_SOURCE.sub(replace, original)
        if rewritten != original:
            tf_file.write_text(rewritten, encoding="utf-8")

    # Fail loudly on a module source the rewrite could not handle. A registry
    # reference or an absolute path would leave the bundle unusable, and the
    # failure would only show up on the customer's `terraform init`.
    for tf_file in sorted(target_dir.glob("*.tf")):
        for block in _MODULE_BLOCK.finditer(tf_file.read_text(encoding="utf-8")):
            source = _SOURCE_ATTRIBUTE.search(block.group("body"))
            value = source.group("value") if source else "(none)"
            if not value.startswith("./modules/"):
                raise PackagingError(
                    f"{example.name}: {tf_file.name} has module source {value!r}. Shared "
                    f"modules must be sourced by a relative path into "
                    f"common/terraform/modules so the packager can vendor them."
                )

    for module in vendored:
        module_source = COMMON_TF_MODULES / module
        if not module_source.is_dir():
            raise PackagingError(f"{example.name}: no such shared module {module!r}")
        copy_tree(module_source, target_dir / "modules" / module)
        (target_dir / "modules" / module / ".terraform.lock.hcl").unlink(missing_ok=True)

    # A copy beside the root module, so `terraform apply` works straight out of
    # the bundle with no variable to set.
    shutil.copy2(lambda_zip, target_dir / "lambda.zip")
    return vendored


def build_cloudformation(example: Example, bundle: Path) -> None:
    configured = (example.manifest.get("iac") or {}).get("cloudformation")
    if not configured:
        raise PackagingError(f"{example.name}: iac.cloudformation is not set")
    source = example.directory / configured
    if not source.is_file():
        raise PackagingError(f"{example.name}: {source} does not exist")

    target_dir = bundle / "cloudformation"
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target_dir / source.name)
    for extra in sorted(source.parent.glob("parameters.example.json")):
        shutil.copy2(extra, target_dir / extra.name)


def build_dashboards(example: Example, bundle: Path) -> list[str]:
    """Copy an example's `dashboards/` into the bundle, if it has one.

    Optional on purpose: an example that ships no dashboards is not defective,
    and requiring an empty directory would just add one to every example. The
    returned list goes into MANIFEST.json so the bundle says what it contains.

    Only `.json` is copied. A dashboard is imported through Grafana's API or the
    UI, so anything else in that directory is working material rather than part
    of the deliverable.
    """
    source = example.directory / "dashboards"
    if not source.is_dir():
        return []
    found = sorted(path for path in source.glob("*.json") if path.is_file())
    if not found:
        return []
    target = bundle / "dashboards"
    target.mkdir(parents=True, exist_ok=True)
    for path in found:
        shutil.copy2(path, target / path.name)
    return [path.name for path in found]


def package(example: Example, out_dir: Path) -> Path:
    version = example.version
    bundle_name = f"{example.name}-{version}"

    with tempfile.TemporaryDirectory(prefix="gcre-package-") as tmp:
        staging = Path(tmp)
        bundle = staging / bundle_name
        bundle.mkdir(parents=True)

        lambda_zip = build_lambda_zip(example, staging)
        shutil.copy2(lambda_zip, bundle / "lambda.zip")

        vendored_modules = build_terraform(example, bundle, lambda_zip)
        build_cloudformation(example, bundle)

        shutil.copy2(example.directory / "README.md", bundle / "README.md")
        shutil.copy2(REPO_ROOT / "LICENSE", bundle / "LICENSE")
        dashboards = build_dashboards(example, bundle)

        manifest = {
            "example": example.name,
            "version": version,
            "title": example.manifest.get("title"),
            "status": example.manifest.get("status"),
            "runtime": example.runtime,
            "architecture": example.architecture,
            "handler": example.manifest["lambda"]["handler"],
            "lambda_zip_sha256": sha256_of(lambda_zip),
            "lambda_zip_bytes": lambda_zip.stat().st_size,
            "vendored_terraform_modules": vendored_modules,
            "dashboards": dashboards,
            "source_commit": _git_commit(),
            # Not a build timestamp: a timestamp would make every bundle unique
            # and defeat the point of a deterministic archive.
            "reproducible": True,
        }
        (bundle / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        out_dir.mkdir(parents=True, exist_ok=True)
        archive = out_dir / f"{bundle_name}.zip"
        write_deterministic_zip(bundle, archive, prefix=f"{bundle_name}/")

        # A copy of the bare deployment package, so a deploy that only needs the
        # function code does not have to unpack the bundle.
        shutil.copy2(lambda_zip, out_dir / f"{example.name}-{version}-lambda.zip")

    print(
        f"{example.name} {version}: {archive.relative_to(REPO_ROOT)} "
        f"({archive.stat().st_size // 1024} KiB, lambda.zip "
        f"{manifest['lambda_zip_bytes'] // 1024} KiB, sha256 "
        f"{manifest['lambda_zip_sha256'][:12]}...)"
    )
    return archive


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.strip() or None


def packageable_examples() -> list[str]:
    names: list[str] = []
    for directory in sorted(EXAMPLES_DIR.iterdir()):
        manifest_path = directory / "example.yaml"
        if not manifest_path.is_file():
            continue
        with manifest_path.open(encoding="utf-8") as handle:
            manifest = yaml.safe_load(handle) or {}
        if manifest.get("status") != "planned" and manifest.get("deliverable") == "lambda-zip":
            names.append(directory.name)
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", nargs="?", help="example to package")
    parser.add_argument("--all", action="store_true", help="package every buildable example")
    parser.add_argument(
        "--out-dir", type=Path, default=REPO_ROOT / "dist", help="where to write the archives"
    )
    args = parser.parse_args(argv)

    if args.all == bool(args.name):
        parser.error("give either an example name or --all, not both and not neither")

    names = packageable_examples() if args.all else [str(args.name)]
    failures = 0
    for name in names:
        try:
            package(load_example(name), args.out_dir)
        except PackagingError as exc:
            print(f"error: {exc}", file=sys.stderr)
            failures += 1
        except subprocess.CalledProcessError as exc:
            print(f"error: {name}: {exc.cmd[0]} failed with exit {exc.returncode}", file=sys.stderr)
            if exc.stderr:
                print(exc.stderr, file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
