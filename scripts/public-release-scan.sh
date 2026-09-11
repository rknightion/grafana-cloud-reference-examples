#!/usr/bin/env bash
#
# Scan the working tree and every reachable Git revision for things that must
# never be published from this repository.
#
# Invoked by `just public-release-scan`, and as the first stage of `just check`.
#
# THE POINT OF SCANNING HISTORY: a banned literal that reaches a commit is not
# fixed by a later commit that removes it. Once this repository is public, the
# blob is fetchable forever and rewriting published history is not an option. So
# the gate has to fail before the commit, not after.
#
# THE TRAP THAT CATCHES AGENTS: absolute local paths. The home-directory prefix
# is rejected case-sensitively, and tooling instructions, pasted command lines
# and generated docs carry one by default. Derive paths from
# `git rev-parse --show-toplevel`, or use relative paths. Never hard-code one -
# in this file included, which is why it excludes itself from its own scan.
set -euo pipefail

if ! command -v rg >/dev/null 2>&1; then
  echo "public-release scan: ripgrep (rg) is required. Without it the working-tree" >&2
  echo "half of every check below silently matches nothing and the scan reports a" >&2
  echo "false pass, which is worse than no scan at all." >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

self="scripts/public-release-scan.sh"
failed=0

# Every working-tree search runs with these. Two of them are load-bearing.
#
# `--no-ignore` is here because ripgrep honours ignore files by default, which
# made this scan blind to whole classes of file. It is `--no-ignore` rather than
# the narrower `--no-ignore-vcs` because that one still honours `.ignore` and
# `.rgignore`, neither of which is a VCS ignore - so one `.ignore` file could
# blind this control while leaving `.gitignore` untouched. This repository's .gitignore
# carries a blanket `*.log` from the upstream Python template, so every committed
# log fixture was invisible to the working-tree half of the scan while being
# perfectly publishable - `git add -f` is all it takes, and the history half only
# notices once the commit already exists. An ignore rule is a convenience for the
# developer; it is not a statement that a file cannot be published, so it must not
# decide what gets inspected.
#
# The exclusions below then re-add what the ignore file was usefully doing: the
# build, dependency and state directories. Each is written `**/name/**` rather
# than `name/**`, because a glob containing a slash is anchored to the search
# root - so the unanchored form left every nested build directory scanned. Those are generated, enormous, and not
# publishable content. Anything ignored for any *other* reason is now scanned.
declare -a RG_ARGS=(
  --hidden
  --no-ignore
  --glob '!.git/**'
  --glob '!**/dist/**'
  --glob '!**/build/**'
  --glob '!**/node_modules/**'
  --glob '!**/.venv/**'
  --glob '!**/.tools/**'
  --glob '!**/__pycache__/**'
  --glob '!**/.pytest_cache/**'
  --glob '!**/.mypy_cache/**'
  --glob '!**/.ruff_cache/**'
  --glob '!**/.terraform/**'
  --glob '!uv.lock'
  --glob '!package-lock.json'
  --glob "!${self}"
)

history_revisions=()
while IFS= read -r revision; do
  history_revisions+=("$revision")
done < <(git rev-list --all)

# Runs a search and writes its matches to stdout. A no-match status of 1 is a
# clean result; anything above 1 means the search itself broke, which must abort
# the whole scan rather than read as "found nothing".
#
# The status is returned rather than acted on here, because every caller invokes
# this inside $(...) and an `exit` in a command substitution only leaves the
# subshell - which is how an earlier version of this script printed a regex
# error and then reported a pass.
run_search() {
  local status=0
  "$@" || status=$?
  return "$status"
}

# Wraps run_search for the callers: captures output, and kills the scan on a
# search failure.
search_or_die() {
  local output status=0
  output=$(run_search "$@") || status=$?
  if ((status > 1)); then
    echo "public-release scan: search command failed with status $status: $*" >&2
    echo "A failed search is not a clean result. Fix the pattern." >&2
    exit 2
  fi
  printf '%s' "$output"
}

# --- Fixed-string scans ------------------------------------------------------

scan_fixed() {
  local label=$1 pattern=$2
  local hits
  hits=$(search_or_die rg "${RG_ARGS[@]}" -n -i -F -- "$pattern" .)
  if [[ -n $hits ]]; then
    printf '%s\n' "$hits"
    echo "public-release scan: found $label in the working tree" >&2
    failed=1
  fi
  if ((${#history_revisions[@]} > 0)); then
    hits=$(search_or_die git grep -I -n -i -F -- "$pattern" "${history_revisions[@]}" \
      -- . ":(exclude)${self}")
    if [[ -n $hits ]]; then
      printf '%s\n' "$hits"
      echo "public-release scan: found $label in reachable Git history" >&2
      failed=1
    fi
  fi
}

scan_fixed_case_sensitive() {
  local label=$1 pattern=$2
  local hits
  hits=$(search_or_die rg "${RG_ARGS[@]}" -n -F -- "$pattern" .)
  if [[ -n $hits ]]; then
    printf '%s\n' "$hits"
    echo "public-release scan: found $label in the working tree" >&2
    failed=1
  fi
  if ((${#history_revisions[@]} > 0)); then
    hits=$(search_or_die git grep -I -n -F -- "$pattern" "${history_revisions[@]}" \
      -- . ":(exclude)${self}")
    if [[ -n $hits ]]; then
      printf '%s\n' "$hits"
      echo "public-release scan: found $label in reachable Git history" >&2
      failed=1
    fi
  fi
}

scan_regex() {
  local label=$1 pattern=$2
  local hits
  hits=$(search_or_die rg "${RG_ARGS[@]}" --glob '!LICENSE' \
    -n -- "$pattern" .)
  if [[ -n $hits ]]; then
    printf '%s\n' "$hits"
    echo "public-release scan: found $label in the working tree" >&2
    failed=1
  fi
  if ((${#history_revisions[@]} > 0)); then
    hits=$(search_or_die git grep -I -n -E -- "$pattern" "${history_revisions[@]}" \
      -- . ':(exclude)LICENSE' ":(exclude)${self}")
    if [[ -n $hits ]]; then
      printf '%s\n' "$hits"
      echo "public-release scan: found $label in reachable Git history" >&2
      failed=1
    fi
  fi
}

scan_regex_ignorecase() {
  local label=$1 pattern=$2
  local hits
  hits=$(search_or_die rg "${RG_ARGS[@]}" --glob '!LICENSE' \
    -n -i -- "$pattern" .)
  if [[ -n $hits ]]; then
    printf '%s\n' "$hits"
    echo "public-release scan: found $label in the working tree" >&2
    failed=1
  fi
  if ((${#history_revisions[@]} > 0)); then
    hits=$(search_or_die git grep -I -n -i -E -- "$pattern" "${history_revisions[@]}" \
      -- . ':(exclude)LICENSE' ":(exclude)${self}")
    if [[ -n $hits ]]; then
      printf '%s\n' "$hits"
      echo "public-release scan: found $label in reachable Git history" >&2
      failed=1
    fi
  fi
}

# --- Credential material -----------------------------------------------------
#
# Token prefixes are matched with a LENGTH BOUND rather than as a bare prefix,
# because this repository documents its credentials and therefore contains
# placeholders on purpose: `glc_fake` in tests, `glc_eyJ...` in a README.
# A length bound distinguishes a real secret from a placeholder without an
# allowlist that has to be maintained every time the docs change.
#
# The prefixes are written split so this control does not itself put a
# greppable token prefix into a public repository.

scan_regex "a Grafana Cloud access policy token" "gl""c_[A-Za-z0-9+/=_-]{32,}"
scan_regex "a Grafana service account token" "gl""sa_[A-Za-z0-9+/=_-]{32,}"
scan_regex "a GitHub token" "(gh""p|gh""o|gh""u|gh""s|gh""r)_[A-Za-z0-9]{30,}|github""_pat_[A-Za-z0-9_]{20,}"
scan_regex "an AWS access key id" "(AKIA|ASIA)[A-Z0-9]{16}"
scan_regex "an AWS secret access key assignment" \
  "aws_secret_access_key[[:space:]]*[=:][[:space:]]*[\"']?[A-Za-z0-9/+=]{40}"
scan_regex "a Slack token" "xox[abprs]-[A-Za-z0-9-]{10,}"
scan_regex "a JWT" "eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\."
scan_fixed_case_sensitive "private key material" "-----BEGIN ""PRIVATE KEY-----"
scan_fixed_case_sensitive "RSA private key material" "-----BEGIN RSA ""PRIVATE KEY-----"
scan_fixed_case_sensitive "OpenSSH private key material" "-----BEGIN OPENSSH ""PRIVATE KEY-----"
scan_fixed_case_sensitive "an encrypted private key" "-----BEGIN ENCRYPTED ""PRIVATE KEY-----"

# --- Environment identity ----------------------------------------------------
#
# None of these are secret on their own. They are rejected because together they
# map out a private estate, and because a reference example that names one is
# telling every reader to copy it.

# Environment identity comes from an ERE passed IN BY ENVIRONMENT, never from
# this file. The pattern set names specific customers, accounts, stacks and
# tenant ids, so committing it to a public repository would disclose exactly
# what it protects. Split string literals are not a substitute: they defeat
# grep, not a reader.
#
# Accepted, in order of precedence:
#   --patterns-file <path>                      a file holding the ERE
#   GCRE_IDENTIFIER_PATTERN                     this repository's override
#   CUSTOMER_IDENTIFIER_PATTERN                 the shared set, and what CI provides
#   GCINSIGHT_CUSTOMER_IDENTIFIER_PATTERN       the existing local export
#
# Absent all four this exits 2 rather than passing, because a scan that silently
# skips its identity half is worse than no scan. Extend the pattern set when a
# new engagement starts: a missing identifier means the gate quietly passes.
identity_pattern=""
patterns_file=""
allow_missing="${GCRE_SCAN_ALLOW_MISSING_PATTERNS:-0}"

while (($# > 0)); do
  case $1 in
    --patterns-file)
      patterns_file=${2:?--patterns-file needs a path}
      shift 2
      ;;
    --allow-missing-patterns)
      allow_missing=1
      shift
      ;;
    *)
      echo "public-release scan: unknown argument $1" >&2
      exit 2
      ;;
  esac
done

if [[ -n $patterns_file ]]; then
  if [[ ! -r $patterns_file ]]; then
    echo "public-release scan: --patterns-file $patterns_file is not readable" >&2
    exit 2
  fi
  identity_pattern=$(tr -d '\n' <"$patterns_file")
else
  identity_pattern="${GCRE_IDENTIFIER_PATTERN:-${CUSTOMER_IDENTIFIER_PATTERN:-${GCINSIGHT_CUSTOMER_IDENTIFIER_PATTERN:-}}}"
fi

if [[ -z $identity_pattern ]]; then
  if [[ $allow_missing == 1 ]]; then
    echo "public-release scan: WARNING - no identity pattern set, so the identity half" >&2
    echo "of this scan did not run. Only the credential and structural checks did." >&2
    echo "This is only acceptable where secrets are genuinely unavailable, such as a" >&2
    echo "pull request from a fork. It is never acceptable before a visibility change." >&2
  else
    echo "public-release scan: no identity pattern available." >&2
    echo >&2
    echo "Set one of GCRE_IDENTIFIER_PATTERN, CUSTOMER_IDENTIFIER_PATTERN or" >&2
    echo "GCINSIGHT_CUSTOMER_IDENTIFIER_PATTERN, or pass --patterns-file <path>." >&2
    echo >&2
    echo "Exiting 2 rather than passing: a scan that skips its identity half while" >&2
    echo "reporting success is worse than no scan at all." >&2
    exit 2
  fi
else
  scan_regex_ignorecase "source-environment identity" "$identity_pattern"
fi

scan_regex "a private IPv4 endpoint" \
  "https?://(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)"

# Matches every candidate, then drops the lines that only contain a documented
# placeholder. A negative lookahead would be the obvious way to write this, and
# neither ripgrep's default engine nor `git grep -E` supports look-around - the
# first version of this file used one and the search errored out.
scan_regex_excluding() {
  local label=$1 pattern=$2 allowed=$3
  local hits filtered
  hits=$(search_or_die rg "${RG_ARGS[@]}" --glob '!LICENSE' \
    -n -- "$pattern" .)
  filtered=$(printf '%s' "$hits" | rg -v -- "$allowed" || true)
  if [[ -n $filtered ]]; then
    printf '%s\n' "$filtered"
    echo "public-release scan: found $label in the working tree" >&2
    failed=1
  fi
  if ((${#history_revisions[@]} > 0)); then
    hits=$(search_or_die git grep -I -n -E -- "$pattern" "${history_revisions[@]}" \
      -- . ':(exclude)LICENSE' ":(exclude)${self}")
    filtered=$(printf '%s' "$hits" | rg -v -- "$allowed" || true)
    if [[ -n $filtered ]]; then
      printf '%s\n' "$filtered"
      echo "public-release scan: found $label in reachable Git history" >&2
      failed=1
    fi
  fi
}

# 123456789012 is the account id AWS itself uses in documentation, and every
# example here uses it, so it is the one allowed form.
scan_regex_excluding "a real AWS account id in an ARN" \
  "arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}:" \
  "123456789012"

# 123456 is the placeholder tenant id used throughout the examples.
scan_regex_excluding "a real Grafana Cloud tenant id" \
  "GRAFANA_CLOUD_LOKI_TENANT_ID[[:space:]]*[=:][[:space:]]*[\"']?[0-9]{5,}" \
  "[^0-9]123456([^0-9]|$)"

# --- Forbidden filenames -----------------------------------------------------
#
# Checked in the tree and in history. A file that was committed once and deleted
# is still fetchable from a public repository.

forbidden_names=(
  terraform.tfvars
  terraform.tfvars.json
  .env
  .envrc
  parameters.json
  credentials
  id_rsa
  id_ed25519
  kubeconfig
  .netrc
  .npmrc
  .pypirc
)

for name in "${forbidden_names[@]}"; do
  if git ls-files --cached --others --exclude-standard -- "**/${name}" "${name}" | rg -q .; then
    echo "public-release scan: found forbidden file name $name in the working tree" >&2
    failed=1
  fi
  if git log --all --format= --name-only --diff-filter=A | rg -qx ".*(^|/)${name//./\\.}"; then
    echo "public-release scan: $name was committed at some point and is still reachable" >&2
    failed=1
  fi
done

# --- Binaries, archives and key containers -----------------------------------
#
# Not a security rule on its own, but an opaque blob is exactly where a
# credential hides from every scan above.

archive_pattern='\.(7z|bin|db|dmg|exe|gz|jks|key|kubeconfig|p12|pem|pfx|pkg|sqlite|tar|tgz|zip)$'

if git ls-files --cached --others --exclude-standard | rg -i "$archive_pattern"; then
  echo "public-release scan: found an archive, binary, key container or local database" >&2
  failed=1
fi

if git log --all --format= --name-only | rg -i "$archive_pattern"; then
  echo "public-release scan: an archive, binary or key container is reachable in Git history" >&2
  failed=1
fi

if ((failed != 0)); then
  echo >&2
  echo "public-release scan FAILED. Nothing above is fixed by a later commit: once this" >&2
  echo "repository is public, every reachable blob stays fetchable. Fix it before the" >&2
  echo "commit, or rewrite history while the repository is still private." >&2
  exit 1
fi

echo "Public-release scan passed: $(git rev-list --all --count) revisions, working tree clean."
