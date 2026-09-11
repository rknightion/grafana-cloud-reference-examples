#!/usr/bin/env python3
"""Turn real Adobe Experience Manager Cloud Service logs into committable fixtures.

Real AEM logs carry a lot that must never reach a public repository: the
customer's program and environment ids, their pod names, their AEM tenant slug,
their hostnames, authenticated user email addresses, and every client IP that hit
the site. This rewrites all of it to documented placeholders while preserving
the *shape* of the data, so a fixture still exercises the parsers the way real
output does.

Two properties matter more than anything else here.

**Substitution is deterministic.** The same input token always maps to the same
placeholder, within and across runs, because it is derived from a keyed hash of
the token rather than from a counter. Without that, regenerating fixtures
produces a completely different diff every time, correlation between an access
log line and the CDN line for the same request is destroyed, and a test that
asserts on a specific node id breaks on every regeneration.

**The keyed hash is not a privacy mechanism.** The key is a fixed constant in
this file, so anyone can recompute the mapping. It exists for stability, not
secrecy. Privacy comes from the values being *replaced*, and from
`just public-release-scan` refusing the result if anything was missed. Never
present the hash as anonymisation of a reversible identifier.

Order matters in `_RULES`: the host rule has to run before the bare program and
environment rules, because `author-p159955-e1712369.adobeaemcloud.com` contains
both and rewriting the parts first leaves a half-substituted hostname that the
host rule no longer matches.

Run it through `just sanitise-aem-logs <src-dir> <dest-dir>`, then
`just public-release-scan`. The scan is the actual gate; this script is only the
thing that makes passing it likely.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import ipaddress
import json
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO

# Fixed so substitution is reproducible across runs and machines. Not a secret,
# and deliberately not configurable: a per-run key would defeat the whole point.
_HASH_KEY = b"grafana-cloud-reference-examples/aem-fixtures/v1"

# RFC 5737 TEST-NET-3, the range reserved for documentation. Using a real-looking
# public range here would put someone else's address in a public repository.
_PUBLIC_IP_NET = ipaddress.ip_network("203.0.113.0/24")
# RFC 1918. Pod-to-pod traffic in AEM Cloud Service genuinely comes from a
# private range, so a fixture that used a public one would misrepresent the data.
# The publication scan only rejects a private address that appears as a URL
# host, so a bare address in a log line is fine.
_PRIVATE_IP_NET = ipaddress.ip_network("10.0.0.0/16")
# RFC 3849, reserved for documentation. AEM's CDN log records whatever address
# the client connected from, which is increasingly IPv6.
_DOC_IPV6_NET = ipaddress.ip_network("2001:db8::/64")

# Addresses this script already produces, or that are reserved for documentation
# and so need no rewriting. Checked before anything else, which makes the script
# idempotent.
_ALREADY_SANITISED_NETS = (
    ipaddress.ip_network("203.0.113.0/24"),  # RFC 5737 TEST-NET-3
    ipaddress.ip_network("198.51.100.0/24"),  # RFC 5737 TEST-NET-2
    ipaddress.ip_network("192.0.2.0/24"),  # RFC 5737 TEST-NET-1
    ipaddress.ip_network("2001:db8::/32"),  # RFC 3849
    ipaddress.ip_network("10.0.0.0/16"),  # what the private branch below emits
)

# The placeholder program and environment. Adobe's own documentation uses
# p12345/e6789, so these read as obviously synthetic to anyone who knows AEM.
_PLACEHOLDER_PROGRAM = "p12345"
_PLACEHOLDER_ENVIRONMENT = "e67890"
_PLACEHOLDER_TENANT = "examplecorp"
_PLACEHOLDER_EMAIL_DOMAIN = "example.com"

_POD_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

# Below this, a literal tenant substitution does more harm than good. Four is
# the shortest slug that is plausibly distinctive in a log line.
_MIN_TENANT_LENGTH = 4


def _digest(token: str, salt: str) -> bytes:
    return hashlib.blake2b(token.encode(), key=_HASH_KEY, salt=salt.encode()[:16]).digest()


def _stable_int(token: str, salt: str, modulo: int) -> int:
    return int.from_bytes(_digest(token, salt)[:8], "big") % modulo


def _stable_word(token: str, salt: str, length: int) -> str:
    raw = _digest(token, salt)
    return "".join(_POD_ALPHABET[byte % len(_POD_ALPHABET)] for byte in raw[:length])


def _fake_ip(token: str) -> str:
    """Map one address to a documentation address, preserving public vs private.

    A parser that treats a private client IP differently from a public one - and
    an example dashboard that shows internal health-check traffic separately from
    real user traffic - both depend on that distinction surviving.

    Handles IPv6 as well as IPv4. An IPv4-only version was the dangerous case:
    Adobe's CDN log carries whatever address the client connected from, so an
    IPv6 client's address would have passed through verbatim into a public
    repository. The sampled corpus happened to contain none.
    """
    try:
        parsed = ipaddress.ip_address(token)
    except ValueError:
        return token
    if parsed.is_loopback:
        # 127.0.0.1 is in every AEM access log, from the JVM's own health check.
        # It identifies nobody and is worth keeping verbatim for realism.
        return token
    if any(parsed in network for network in _ALREADY_SANITISED_NETS):
        # Already a documentation address, so leave it alone and make the whole
        # script idempotent - re-running it over its own output is a no-op.
        #
        # Not merely tidy: Python's `is_private` returns **True** for the RFC
        # 5737 documentation ranges, because they are in the IANA
        # special-purpose registry. Without this branch a second pass would
        # classify 203.0.113.x as private and move every client address into
        # 10.0.0.0/16, quietly destroying the public-versus-internal split the
        # example dashboards rely on.
        return token
    if parsed.version == 6:
        # RFC 3849 reserves 2001:db8::/32 for documentation. Only the low 64 bits
        # are randomised, which keeps the result short enough to read.
        offset = int.from_bytes(_digest(token, "ip6")[:8], "big")
        return str(_DOC_IPV6_NET[offset])
    network = _PRIVATE_IP_NET if parsed.is_private else _PUBLIC_IP_NET
    offset = _stable_int(token, "ip", network.num_addresses - 2) + 1
    return str(network[offset])


def _fake_uuid(token: str) -> str:
    raw = _digest(token, "uuid")[:16]
    # Version 4, variant 1, so it is a well-formed UUID rather than 32 hex
    # digits that happen to have dashes in them.
    patched = bytearray(raw)
    patched[6] = (patched[6] & 0x0F) | 0x40
    patched[8] = (patched[8] & 0x3F) | 0x80
    hexed = patched.hex()
    return f"{hexed[:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:]}"


def _fake_email(local: str, domain: str) -> str:
    del domain  # every domain collapses to the placeholder; only the local part varies
    return f"user{_stable_int(local, 'email', 900) + 100}@{_PLACEHOLDER_EMAIL_DOMAIN}"


# --- The real PII ------------------------------------------------------------
#
# An IP address in an operational log is normal and useful, which is why this
# script leaves them alone by default. The categories below are the ones that
# actually matter, and none of them belongs in a log line at all - let alone in
# a committed fixture.
#
# These patterns DETECT rather than rewrite. A match is a hard failure, because
# a heuristic replacement of a medical record number or a date of birth gives
# false confidence: the right response is for a human to look at the source data
# and decide whether it should ever have been logged.
#
# The list is deliberately incomplete and cannot be otherwise. A log message is
# free text and can carry anything. It is a tripwire for the obvious shapes, not
# a guarantee.
_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a UK National Insurance number",
        re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]\b"),
    ),
    (
        # Label-scoped on purpose. The bare ten-digit shape matched an Apache
        # microsecond timestamp followed by a year (`093820 2026`) on every line
        # of the httpd error log, and a detector that fires on every line is one
        # people switch off.
        "a UK NHS number",
        re.compile(r"(?i)\bnhs\b[^\n]{0,20}?\b\d{3}[ -]?\d{3}[ -]?\d{4}\b"),
    ),
    (
        "a US social security number",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
    (
        # Context-scoped AND Luhn-checked. Neither alone is enough: the bare
        # 13-to-19-digit shape matched an Apache thread id and UUID fragments,
        # and Luhn is only a one-in-ten filter, so a fifteen-digit thread id
        # passed it by coincidence on every line of the httpd error log.
        #
        # Requiring a payment word nearby is the honest trade. A bare Luhn-valid
        # number in an application log is far more often an identifier than a
        # card, and a detector that fires on every line is one people switch off.
        # The cost is that an unlabelled card number is missed - which is why
        # this is a tripwire and `just public-release-scan` is the gate.
        "a payment card number",
        re.compile(
            r"(?i)\b(?:card|pan|payment|credit|debit|visa|mastercard|amex|cvv)\b"
            r"[^\n]{0,30}?(?P<digits>(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-]))"
        ),
    ),
    (
        "an international phone number",
        re.compile(r"(?<![\w.])\+\d{1,3}[\s-]?\(?\d{2,5}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}(?![\w.])"),
    ),
    (
        "a date of birth",
        # Only the labelled forms. A bare date is every log line's timestamp.
        re.compile(
            r"(?i)\b(?:dob|date[_\s-]?of[_\s-]?birth|birth[_\s-]?date|birthday)\b"
            r"\s*[:=]?\s*\S+"
        ),
    ),
    (
        "a UK postcode",
        re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b"),
    ),
    (
        "a health or medical field name",
        re.compile(
            r"(?i)\b(?:patient|diagnosis|prescription|medical[_\s-]?record"
            r"|nhs[_\s-]?number|health[_\s-]?record|icd10|snomed)\b"
        ),
    ),
    (
        "a home address field name",
        re.compile(r"(?i)\b(?:home[_\s-]?address|postal[_\s-]?address|street[_\s-]?address)\b"),
    ),
)


def _luhn_valid(digits: str) -> bool:
    """The checksum every card issuer uses. Cheap, and removes almost all noise."""
    numbers = [int(char) for char in digits if char.isdigit()]
    if not 13 <= len(numbers) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(numbers)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def find_sensitive(line: str) -> list[str]:
    """Labels of every high-risk PII shape in a line. Empty is the normal case."""
    found = []
    for label, pattern in _SENSITIVE_PATTERNS:
        match = pattern.search(line)
        if match is None:
            continue
        # `match.group("digits")`, not `match.group(0)`: the pattern is
        # context-scoped, so the whole match carries the payment keyword and up
        # to 30 characters of gap. Any digit in that gap - an amount, an order
        # number - would be folded into the checksum and make a real card fail
        # validation.
        if label == "a payment card number" and not _luhn_valid(match.group("digits")):
            continue
        found.append(label)
    return found


Rule = tuple[str, re.Pattern[str], Callable[[re.Match[str]], str]]

# Order is load-bearing. See the module docstring.
_RULES: tuple[Rule, ...] = (
    (
        "aem hostname",
        re.compile(
            r"\b(?P<tier>author|publish|preview)-p\d+-e\d+"
            r"(?P<suffix>\.adobeaemcloud\.com|\.adobeaemcloud\.net)\b"
        ),
        lambda m: (
            f"{m.group('tier')}-{_PLACEHOLDER_PROGRAM}-"
            f"{_PLACEHOLDER_ENVIRONMENT}{m.group('suffix')}"
        ),
    ),
    (
        "pod name",
        # cm-p159955-e1712369-aem-author-c7fb6c77f-g5qvd. The replicaset hash and
        # the pod suffix both identify a specific running instance, and the pod
        # suffix is what a per-node dashboard panel groups by, so it has to stay
        # distinct per pod rather than collapsing to one value.
        re.compile(r"\bcm-p\d+-e\d+-aem-(?P<role>[a-z]+)-(?P<rs>[a-z0-9]+)-(?P<pod>[a-z0-9]{5})\b"),
        lambda m: (
            f"cm-{_PLACEHOLDER_PROGRAM}-{_PLACEHOLDER_ENVIRONMENT}-aem-{m.group('role')}-"
            f"{_stable_word(m.group('rs'), 'rs', 9)}-{_stable_word(m.group('pod'), 'pod', 5)}"
        ),
    ),
    (
        # AEM's asset-processing log names the asset it is working on, and an
        # asset name is unconstrained customer content. In the sampled logs it
        # carried 608 distinct filenames including the customer's drug brand
        # names and their subsidiary's name - none of which any identifier
        # pattern would catch, because they are free text rather than an id.
        #
        # Rewritten to a stable placeholder that keeps the SHAPE - Adobe's
        # `ASSET-<year>-<number>` prefix and the extension - so the fixture
        # still exercises the parser and a dashboard grouping by asset still
        # shows a realistic spread.
        "dam asset filename",
        re.compile(
            r"\b(?P<prefix>ASSET-\d{4}-\d+)?[A-Za-z0-9_.()\[\]-]*?"
            r"\.(?P<ext>ai|psd|indd|jpe?g|png|gif|svg|tiff?|pdf|mp4|mov|zip|eps)\b"
        ),
        lambda m: (
            f"{m.group('prefix') or 'ASSET-2026-00000'}"
            f"_{_stable_word(m.group(0), 'asset', 10)}.{m.group('ext')}"
        ),
    ),
    (
        # The DAM path that asset sits under is equally customer content: the
        # folder names are the customer's own taxonomy.
        "dam content path",
        re.compile(r"/content/dam/(?P<rest>[A-Za-z0-9_./()\[\]-]+)"),
        lambda m: f"/content/dam/{_stable_word(m.group('rest'), 'dam', 8)}",
    ),
    (
        "email address",
        re.compile(r"\b(?P<local>[A-Za-z0-9._%+-]+)@(?P<domain>[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"),
        lambda m: _fake_email(m.group("local"), m.group("domain")),
    ),
    (
        "uuid",
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        lambda m: _fake_uuid(m.group(0)),
    ),
)

# Applied only when IP replacement is switched on. See `--ip-addresses`.
#
# Separate from `_RULES` because an IP address is not in the same category as a
# program id or a tenant slug: those identify the customer whatever the
# destination, while an address is ordinary operational telemetry that only
# needs rewriting when the data is being republished.
_IP_RULES: tuple[Rule, ...] = (
    (
        "ipv6 address",
        # Deliberately conservative: requires at least two colons and only
        # matches hex groups, so it does not fire on a timestamp (`00:00:04`) or
        # on a thread name containing colons. A compressed `::1` loopback is
        # covered by `_fake_ip` returning loopback unchanged.
        re.compile(
            r"(?<![:.\w])(?:[0-9A-Fa-f]{1,4}:){2,7}(?::|[0-9A-Fa-f]{1,4})"
            r"(?:::[0-9A-Fa-f]{0,4})?(?![:.\w])"
        ),
        lambda m: _fake_ip(m.group(0)),
    ),
    (
        "ipv4 address",
        # A bare `\b(?:\d{1,3}\.){3}\d{1,3}\b` also matches a four-part software
        # version, and AEM logs are full of them: it rewrote
        # `Chrome/119.0.0.0` to `Chrome/203.0.113.43`, which silently destroyed
        # the user-agent field the example dashboards group by.
        #
        # So: reject a match preceded by a letter and a slash (`Chrome/119...`)
        # while still allowing one preceded by a double slash, which is how an
        # address appears as a URL host, reject one preceded by a word
        # character, dot or hyphen, and reject one followed by a further dot or
        # word character so a five-part version is not clipped down to its
        # first four fields.
        #
        # The double-slash case is not spelled out as an example here on
        # purpose: a scheme followed by a private address is exactly what
        # `just public-release-scan` rejects, and it rejected an earlier draft
        # of this comment.
        re.compile(r"(?<![\w.-])(?<![A-Za-z]/)(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"),
        lambda m: _fake_ip(m.group(0)),
    ),
)

_IDENTITY_RULES_TAIL: tuple[Rule, ...] = (
    (
        "bare program id",
        re.compile(r"\bp\d{5,}\b"),
        lambda m: _PLACEHOLDER_PROGRAM,
    ),
    (
        "bare environment id",
        re.compile(r"\be\d{6,}\b"),
        lambda m: _PLACEHOLDER_ENVIRONMENT,
    ),
)

# Applied after the regex rules, to whole-token values the rules cannot see as a
# pattern. The AEM tenant slug is an arbitrary customer-chosen string, so nothing
# short of an explicit list finds it.
_LITERAL_KEYS = ("aem_tenant",)


def sanitise_line(line: str, *, tenants: set[str], replace_ips: bool = False) -> str:
    """Rewrite one line's identifiers.

    `replace_ips` is off by default. An IP address in an operational log is
    ordinary, useful telemetry - abuse investigation, geography, separating
    health-check traffic from real users all depend on it - and Grafana Cloud
    Loki is full of them by design. The data that actually needs care is
    content-level: medical details, health records, home addresses, dates of
    birth, phone numbers. Those are detected by `find_sensitive` and refused,
    not quietly rewritten.

    Turn it on for data being published somewhere it was not collected. The
    committed fixture corpus in this repository does exactly that, which is why
    `just sanitise-aem-logs` passes `--ip-addresses replace`: those addresses
    belong to real visitors to a customer's site, and a public repository is not
    where they were meant to end up.
    """
    rules = (*_RULES, *(_IP_RULES if replace_ips else ()), *_IDENTITY_RULES_TAIL)
    for _label, pattern, replace in rules:
        line = pattern.sub(replace, line)
    for tenant in sorted(tenants, key=len, reverse=True):
        # Bounded on both sides, so a slug does not match inside an unrelated
        # word, a content path, a Java logger name or a user agent.
        #
        # A slug shorter than the floor is refused rather than replaced: a
        # two-character tenant would rewrite fragments of half the file, and a
        # corrupted fixture is a worse outcome than a slug that has to be added
        # to the scan's pattern set by hand.
        if len(tenant) < _MIN_TENANT_LENGTH:
            print(
                f"refusing to replace tenant slug {tenant!r}: shorter than "
                f"{_MIN_TENANT_LENGTH} characters, so substitution would corrupt "
                f"unrelated text. Sanitise it by hand and confirm with "
                f"`just public-release-scan`.",
                file=sys.stderr,
            )
            continue
        line = re.sub(
            rf"(?<![A-Za-z0-9]){re.escape(tenant)}(?![A-Za-z0-9])",
            _PLACEHOLDER_TENANT,
            line,
            flags=re.IGNORECASE,
        )
    return line


def collect_tenants(lines: Iterator[str]) -> set[str]:
    """Find every `aem_tenant` value in a CDN log, so it can be replaced literally.

    Returned as a set and applied to *every* log type, not just the CDN one,
    because the tenant slug shows up in AEM error messages and request paths too
    and there is no pattern that finds it there.
    """
    found: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        for key in _LITERAL_KEYS:
            value = record.get(key)
            if isinstance(value, str) and value:
                found.add(value)
    return found


def _open_text(path: Path) -> IO[str]:
    if path.suffix == ".gz":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return path.open(encoding="utf-8", errors="replace")


def sample_lines(lines: list[str], limit: int, *, block: int = 25) -> list[str]:
    """Keep `limit` lines as contiguous blocks spread across the file.

    **Blocks, not every Nth line.** Adjacency carries meaning in these formats
    and stride sampling destroys it: the AEM request log pairs a `->` line with a
    `<-` line, and the error log follows an exception with its stack trace
    continuation lines. A fixture sampled by stride pairs almost nothing - it
    measured 6 pairs out of 137 responses where the full file pairs all of them -
    so a test written against it would encode the sampler's artefact as the
    expected behaviour.

    Blocks are taken from evenly spaced offsets so the fixture still spans the
    whole day rather than one quiet hour, and the final block is taken from the
    very end of the file, which is where the genuinely unpairable
    response-without-request cases live.
    """
    if len(lines) <= limit:
        return lines
    block = max(1, min(block, limit))
    blocks = max(1, limit // block)
    # The last block is pinned to the end of the file; the rest are spread over
    # what precedes it.
    tail = lines[-block:]
    body = lines[:-block]
    wanted = blocks - 1
    if wanted < 1:
        return tail[:limit]
    stride = max(1, len(body) // wanted)
    sampled: list[str] = []
    for index in range(wanted):
        start = min(index * stride, max(0, len(body) - block))
        sampled.extend(body[start : start + block])
    return (sampled + tail)[:limit]


def sanitise_file(
    source: Path,
    destination: Path,
    *,
    limit: int,
    tenants: set[str],
    replace_ips: bool,
    allow_sensitive: bool = False,
) -> tuple[int, dict[str, int]]:
    """Write one sanitised fixture. Returns the line count and any PII found.

    Writes nothing when high-risk PII is detected and `allow_sensitive` is off:
    a count of 0 with a non-empty `found` means the file was refused.

    The sensitive-content check runs on the **output**, after every rewrite, so
    it reports what would actually be committed rather than what was read.
    """
    with _open_text(source) as handle:
        raw = [line.rstrip("\n") for line in handle if line.strip()]
    kept = sample_lines(raw, limit)
    cleaned = [sanitise_line(line, tenants=tenants, replace_ips=replace_ips) for line in kept]

    found: dict[str, int] = {}
    for number, line in enumerate(cleaned, start=1):
        for label in find_sensitive(line):
            found[label] = found.get(label, 0) + 1
            if found[label] == 1:
                print(f"  {destination.name}:{number}: {label}", file=sys.stderr)

    if found and not allow_sensitive:
        # Nothing is written. Reporting the problem and then leaving the file on
        # disk anyway is the worst of both: the run says it refused, and the PII
        # is sitting in the working tree where the next `git add` picks it up.
        return 0, found

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
    return len(cleaned), found


def fixture_name(source: Path) -> str:
    """Normalise `author_aemaccess_2026-07-24 (1).log.gz` to `author_aemaccess.log`.

    The date and the browser's duplicate-download suffix are noise in a fixture
    name, and a `.gz` fixture would just make the tests decompress on every run
    for no coverage - the reader's gzip path is already covered in
    `common/python/tests`.
    """
    stem = source.name
    for suffix in (".gz", ".log"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    stem = re.sub(r"[_-]\d{4}-\d{2}-\d{2}$", "", stem)
    return f"{stem}.log"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="directory of real AEM log downloads")
    parser.add_argument("destination", type=Path, help="fixture directory to write")
    parser.add_argument(
        "--limit",
        type=int,
        default=250,
        help="lines to keep per log file (default: %(default)s)",
    )
    parser.add_argument(
        "--ip-addresses",
        choices=("keep", "replace"),
        default="keep",
        help=(
            "keep (default) leaves client IP addresses alone: they are ordinary "
            "operational telemetry and Grafana Cloud Loki is full of them by "
            "design. replace rewrites them into the RFC 5737 and RFC 3849 "
            "documentation ranges, which is what you want when publishing the "
            "data somewhere it was not collected - a public repository, a "
            "support ticket, a conference talk."
        ),
    )
    parser.add_argument(
        "--allow-sensitive",
        action="store_true",
        help=(
            "continue despite detecting high-risk content PII - medical or health "
            "data, a home address, a date of birth, a phone number, a national "
            "identifier, a payment card. Without this the run fails and writes "
            "nothing further, because none of that should be in a log line and a "
            "heuristic rewrite of it gives false confidence."
        ),
    )
    args = parser.parse_args(argv)

    if not args.source.is_dir():
        print(f"not a directory: {args.source}", file=sys.stderr)
        return 2

    sources = sorted(
        path
        for path in args.source.iterdir()
        if path.is_file() and (path.suffix == ".gz" or path.suffix == ".log")
    )
    if not sources:
        print(f"no .log or .log.gz files under {args.source}", file=sys.stderr)
        return 2

    # One pass over everything to find tenant slugs before rewriting anything,
    # because the slug found in the CDN log has to be replaced in the AEM logs
    # too and those are processed independently.
    tenants: set[str] = set()
    for path in sources:
        with _open_text(path) as handle:
            tenants |= collect_tenants(handle)
    if tenants:
        print(f"found {len(tenants)} tenant slug(s) to replace literally", file=sys.stderr)

    written: dict[str, int] = {}
    sensitive: dict[str, int] = {}
    for path in sources:
        target = args.destination / fixture_name(path)
        if target.name in written:
            # `author_aemaccess_2026-07-24 (1).log` and its sibling normalise to
            # the same fixture name. Taking the first and saying so beats
            # silently overwriting.
            print(f"skipping {path.name}: {target.name} already written", file=sys.stderr)
            continue
        count, found = sanitise_file(
            path,
            target,
            limit=args.limit,
            tenants=tenants,
            replace_ips=args.ip_addresses == "replace",
            allow_sensitive=args.allow_sensitive,
        )
        written[target.name] = count
        for label, hits in found.items():
            sensitive[label] = sensitive.get(label, 0) + hits
        print(f"{path.name} -> {target.name} ({count} lines)", file=sys.stderr)

    if sensitive and not args.allow_sensitive:
        print("", file=sys.stderr)
        print("REFUSING to treat these fixtures as sanitised. Found:", file=sys.stderr)
        for label, hits in sorted(sensitive.items()):
            print(f"  {hits} line(s) matching {label}", file=sys.stderr)
        print("", file=sys.stderr)
        print(
            "This is the PII that matters, and it is not something to rewrite with a\n"
            "regex: look at the source logs, work out why it is being logged at all,\n"
            "and fix it there. Re-run with --allow-sensitive only if every match above\n"
            "is a false positive, and say so in the commit.",
            file=sys.stderr,
        )
        return 1

    if args.ip_addresses == "keep":
        print(
            "\nClient IP addresses were KEPT (--ip-addresses keep, the default).\n"
            "That is right for a Loki tenant and wrong for a public repository.\n"
            "Pass --ip-addresses replace if this data is being published.",
            file=sys.stderr,
        )

    print(
        "\nNow run `just public-release-scan`. This script is best-effort; the scan is the gate.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
