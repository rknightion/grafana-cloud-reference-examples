#!/usr/bin/env bash
set -euo pipefail

terraform_version=$1
tflint_version=$2
shift 2
node_version=$(awk '$1 == "node-version:" { print $2; exit }' .github/workflows/ci.yml)
tools="$PWD/.tools/cursor"
bin_dir="$tools/$terraform_version-$tflint_version/bin"

export UV_PYTHON
UV_PYTHON=$(<.python-version)
export PATH="$bin_dir:$PATH"
export TF_PLUGIN_CACHE_DIR="$tools/terraform-plugins" TFLINT_PLUGIN_DIR="$tools/tflint-plugins"
export TF_CLI_CONFIG_FILE=/dev/null CHECKPOINT_DISABLE=1

if (( $# )); then
    exec fnm exec --using="$node_version" -- "$@"
fi

if ! uv python find --managed-python "$UV_PYTHON" >/dev/null 2>&1; then
    uv python install "$UV_PYTHON"
fi
if ! fnm exec --using="$node_version" -- node --version >/dev/null 2>&1; then
    fnm install "$node_version"
fi

case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64) arch=arm64 ;;
    *) printf 'Unsupported Linux architecture\n' >&2; exit 1 ;;
esac

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir -p "$bin_dir" "$TF_PLUGIN_CACHE_DIR" "$TFLINT_PLUGIN_DIR" "$stage/gh"

download() {
    curl -qfsSL --retry 2 --max-time 120 --proto '=https' --proto-redir '=https' "$1"
}

install_zip() {
    local tool=$1 url=$2 archive=$3 manifest=$4
    download "$url/$archive" >"$stage/$archive"
    download "$url/$manifest" |
        awk -v file="$archive" '$2 == file || $2 == "*" file' |
        (cd "$stage"; sha256sum --check --strict -)
    unzip -p "$stage/$archive" "$tool" >"$stage/$tool"
    install -m 0755 "$stage/$tool" "$bin_dir/$tool"
}

if [[ ! -x "$bin_dir/terraform" ]]; then
    install_zip terraform "https://releases.hashicorp.com/terraform/$terraform_version" \
        "terraform_${terraform_version}_linux_$arch.zip" "terraform_${terraform_version}_SHA256SUMS"
fi
if [[ ! -x "$bin_dir/tflint" ]]; then
    install_zip tflint "https://github.com/terraform-linters/tflint/releases/download/v$tflint_version" \
        "tflint_linux_$arch.zip" checksums.txt
fi

# The ordinary setup can use gh for plugin downloads. Use anonymous public access in Cursor.
env -u GH_TOKEN -u GITHUB_TOKEN -u GH_ENTERPRISE_TOKEN -u GITHUB_ENTERPRISE_TOKEN \
    -u TF_TOKEN_registry_terraform_io \
    GH_CONFIG_DIR="$stage/gh" npm_config_audit=false npm_config_fund=false \
    fnm exec --using="$node_version" -- just setup
