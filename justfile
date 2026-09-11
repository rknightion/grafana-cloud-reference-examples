set shell := ["bash", "-euo", "pipefail", "-c"]

# Tool version pins. `just` itself is pinned by CI (setup-just) and by Homebrew
# locally. Python tooling is pinned in pyproject.toml dev-dependencies and Node
# tooling in package.json, so Renovate maintains those in one place each. Only
# tools with no manifest to live in are pinned here.

# renovate: datasource=github-releases depName=hashicorp/terraform
TERRAFORM_VERSION := "1.16.1"

# renovate: datasource=github-releases depName=terraform-linters/tflint
TFLINT_VERSION := "0.60.0"

TOOLS_DIR := justfile_directory() / ".tools"
DIST_DIR := justfile_directory() / "dist"

default:
    @just --list

# Install every dependency the gate needs: Python workspace, Node workspace, Terraform providers.
setup:
    uv sync --all-packages
    npm ci
    just _tf-init-all
    just _tflint-init

# Format Python, TypeScript, Terraform and this justfile in place.
[group('check')]
fmt:
    uv run ruff format .
    uv run ruff check --fix-only .
    npm run --silent fmt
    terraform fmt -recursive .
    just --fmt

# Fail if anything is unformatted. The version-sensitive half of the gate.
[group('check')]
fmt-check:
    uv run ruff format --check .
    npm run --silent fmt-check
    terraform fmt -recursive -check -diff .
    just --fmt --check

# Lint Python, TypeScript, Terraform, CloudFormation and the example manifests.
[group('check')]
lint:
    uv run ruff check .
    uv run mypy .
    npm run --silent lint
    uv run python tools/check_conformance.py
    just _tflint-all
    uv run cfn-lint "common/cloudformation/templates/*.yaml" "examples/*/cloudformation/*.yaml"

# Run the Python and TypeScript unit tests.
[group('check')]
test:
    uv run pytest
    npm test --silent

# The complete local gate, and the pre-commit gate. Everything here runs with only the language toolchains installed.
[group('check')]
check: fmt-check lint test
    @echo "check: green"

# List every example and the runtime it targets.
[group('dev')]
examples:
    @uv run python tools/check_conformance.py --list

# Build one example's release zip into dist/ (lambda code + vendored common + terraform + cloudformation).
[group('build')]
package name:
    uv run python tools/package_example.py {{ name }} --out-dir {{ DIST_DIR }}

# Build a release zip for every example.
[group('build')]
package-all:
    uv run python tools/package_example.py --all --out-dir {{ DIST_DIR }}

# Scaffold a new example from the template. Usage: just new-example my-thing python
[group('gen')]
new-example name runtime="python":
    uv run python tools/new_example.py {{ name }} --runtime {{ runtime }}

# terraform validate every shared module and every example's root module.
[group('infra')]
tf-validate: _tf-init-all
    #!/usr/bin/env bash
    set -euo pipefail
    for dir in common/terraform/modules/*/ examples/*/terraform/; do
        [[ -f "${dir}versions.tf" ]] || continue
        echo "==> terraform validate ${dir}"
        terraform -chdir="${dir}" validate
    done

# Lint and template-check every CloudFormation template.
[group('infra')]
cfn-validate:
    uv run cfn-lint "common/cloudformation/templates/*.yaml" "examples/*/cloudformation/*.yaml"
    uv run python tools/check_conformance.py --cloudformation-only

# Delete build output. Never touches tracked files.
[group('build')]
clean:
    rm -rf {{ DIST_DIR }} {{ justfile_directory() / "build" }}

[private]
_tf-init-all:
    #!/usr/bin/env bash
    set -euo pipefail
    for dir in common/terraform/modules/*/ examples/*/terraform/; do
        [[ -f "${dir}versions.tf" ]] || continue
        terraform -chdir="${dir}" init -backend=false -input=false >/dev/null
    done

# tflint refuses to run at all with an uninstalled plugin, so this is not
# optional even though `just lint` tolerates tflint being absent entirely.
# The plugin is fetched from the GitHub releases API, which rate-limits
# unauthenticated requests hard enough to fail on a shared IP - so pass a token
# when one is available.
[private]
_tflint-init:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v tflint >/dev/null 2>&1; then
        echo "tflint not installed; skipping plugin init. brew install tflint" >&2
        exit 0
    fi
    export GITHUB_TOKEN="${GITHUB_TOKEN:-$(gh auth token 2>/dev/null || true)}"
    tflint --init --config="{{ justfile_directory() }}/.tflint.hcl"

[private]
_tflint-all:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v tflint >/dev/null 2>&1; then
        echo "tflint not installed; skipping. Install it to match CI: brew install tflint" >&2
        exit 0
    fi
    tflint --recursive --config="$(pwd)/.tflint.hcl"
