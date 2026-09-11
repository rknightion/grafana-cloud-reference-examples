"""The corpus tests: every committed fixture, end to end.

These are the tests that actually caught things. A unit test with a hand-written
line proves the regex accepts what you wrote; running 1036 lines of real
sanitised AEM output through the parsers proved it did not accept 41 of them.

They are also the guard on the fixtures themselves. A regenerated corpus that
silently lost its variety - only 200 responses, no WARN lines, every request
paired - would let a parser regression through, so the shape of the corpus is
asserted too.
"""

from __future__ import annotations

import collections
import ipaddress
import json
import pathlib
import re
import sys

import pytest

from adobe_aem.classify import (
    DEFAULT_KEY_PATTERN,
    compile_key_pattern,
    from_key,
    sniff_log_type,
    tier_from_name,
)
from adobe_aem.correlate import RequestCorrelator
from adobe_aem.labels import build_labels, detected_level_for
from adobe_aem.logtypes import LEVELLED_TYPES, LogType, Tier
from adobe_aem.parsers import parser_for

# The sanitiser lives in tools/, which is not an importable package. The one test
# that needs it adds this to sys.path locally rather than making the whole module
# depend on it.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# Every log type Adobe forwards must have a fixture. Without this the corpus can
# lose a file and the suite still passes with nothing to say about that type.
EXPECTED_FIXTURES = {
    "author_aemaccess.log": LogType.AEM_ACCESS,
    "author_aemcdn.log": LogType.AEM_CDN,
    "author_aemerror.log": LogType.AEM_ERROR,
    "author_aemrequest.log": LogType.AEM_REQUEST,
    "publish_aemdispatcher.log": LogType.AEM_DISPATCHER,
    "publish_aemhttpdaccess.log": LogType.AEM_HTTPD_ACCESS,
    "publish_aemhttpderror.log": LogType.AEM_HTTPD_ERROR,
}

# Terms that must never appear in a committed fixture. `just public-release-scan`
# is the real gate and covers far more, but a failure here names the fixture and
# the line, which is a much faster diagnosis than a scan hit in CI.
#
# The customer identifier itself is deliberately absent: it must not be
# committed to this repository even as a test constant. The scan holds it, from
# the environment.
FORBIDDEN_IN_FIXTURES = (
    # Real Cloud Manager ids from the source logs. The placeholders are
    # p12345/e67890.
    re.compile(r"\bp15\d{4}\b"),
    re.compile(r"\be17\d{5}\b"),
    # An email address at any domain other than the placeholder.
    re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)

# Networks a fixture is allowed to carry. Everything else is somebody's real
# address and must never be committed.
ALLOWED_NETWORKS = (
    ipaddress.ip_network("203.0.113.0/24"),  # RFC 5737 TEST-NET-3
    ipaddress.ip_network("198.51.100.0/24"),  # RFC 5737 TEST-NET-2
    ipaddress.ip_network("192.0.2.0/24"),  # RFC 5737 TEST-NET-1
    ipaddress.ip_network("2001:db8::/32"),  # RFC 3849
    ipaddress.ip_network("127.0.0.0/8"),  # loopback; AEM's own health check
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918, the cluster-internal ranges
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

# **This extraction is deliberately broader than the sanitiser's.**
#
# The sanitiser has to be narrow: it rewrites what it matches, so a loose
# pattern corrupts real data - an early version turned `Chrome/119.0.0.0` into
# an IP address. This test only reads, so it can afford to over-match and then
# discard whatever does not parse as an address.
#
# That asymmetry is the point. It is what catches an address the sanitiser's
# narrow rule skipped: `GET /client/8.8.8.8` is preceded by a letter and a
# slash, which the sanitiser excludes on purpose, so only a broad guard finds it.
#
# One ambiguity is genuinely unresolvable by pattern alone: a four-part software
# version IS a syntactically valid IPv4 address, and `Chrome/119.0.0.0` sits in
# exactly the same `Word/` position as `/client/8.8.8.8`. So the narrowest
# possible carve-out is applied, and only that: a candidate is ignored when it
# follows `Word/` **and** has the shape `N.0.0.0`, which is how browsers write a
# major-only version and which no host address in these logs takes.
_VERSION_AFTER_PRODUCT = re.compile(r"[A-Za-z]/\d{1,3}(?:\.0){3}$")
_CANDIDATE = re.compile(r"[0-9A-Fa-f][0-9A-Fa-f:.]{2,}[0-9A-Fa-f]")


def addresses_in(line: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every token in a line that is genuinely an IP address.

    Candidates are validated with `ipaddress`, which is the only reliable test.
    A timestamp (`00:00:04`), a byte count and a thread name simply fail to
    parse and are dropped.
    """
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for match in _CANDIDATE.finditer(line):
        candidate = match.group(0)
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if _VERSION_AFTER_PRODUCT.search(line[: match.end()]):
            continue
        found.append(address)
    return found


@pytest.fixture(scope="session")
def key_pattern() -> re.Pattern[str]:
    return compile_key_pattern(DEFAULT_KEY_PATTERN)


class TestCorpusIsSanitised:
    def test_every_expected_fixture_exists(self, fixture_lines: dict[str, list[str]]) -> None:
        assert set(fixture_lines) == set(EXPECTED_FIXTURES)

    def test_no_forbidden_identifiers(self, fixture_lines: dict[str, list[str]]) -> None:
        for name, lines in fixture_lines.items():
            for number, line in enumerate(lines, start=1):
                for pattern in FORBIDDEN_IN_FIXTURES:
                    found = pattern.search(line)
                    assert found is None, f"{name}:{number} contains {found.group(0)!r}"

    def test_every_address_is_in_an_allowed_network(
        self, fixture_lines: dict[str, list[str]]
    ) -> None:
        """A real public address in a public repository is somebody else's."""
        for name, lines in fixture_lines.items():
            for number, line in enumerate(lines, start=1):
                for address in addresses_in(line):
                    assert any(address in network for network in ALLOWED_NETWORKS), (
                        f"{name}:{number} carries {address}, which is not in a "
                        f"documentation, loopback or private range"
                    )

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # The two forms the narrower, sanitiser-style regex missed. Both are
            # real addresses and both must be caught by this guard.
            ('"GET /client/8.8.8.8 HTTP/1.1"', ["8.8.8.8"]),
            ('{"cli_ip":"2606:4700:4700::1111"}', ["2606:4700:4700::1111"]),
            # And the forms it must NOT flag, or the guard is unusable.
            # A browser version, not an address, despite parsing as one.
            ("Chrome/119.0.0.0 Safari/537.36", []),
            ("Mozilla/5.0 (X11) Chrome/150.0.0.0 Safari/537.36", []),
            ("24.07.2026 00:00:00.155 [sling-oak-observation-1]", []),
            ("<- 200 text/html;charset=utf-8 8ms", []),
            ("Events.Service.org.apache.sling.event", []),
        ],
    )
    def test_the_extractor_finds_addresses_and_not_lookalikes(
        self, line: str, expected: list[str]
    ) -> None:
        assert [str(address) for address in addresses_in(line)] == expected

    def test_no_high_risk_content_pii(self, fixture_lines: dict[str, list[str]]) -> None:
        """The PII that actually matters, as opposed to an IP address.

        Medical and health data, home addresses, dates of birth, phone numbers,
        national identifiers, payment cards. None of it belongs in a log line,
        and the sanitiser refuses to write a corpus containing any of it.
        """
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        from sanitise_aem_logs import find_sensitive

        for name, lines in fixture_lines.items():
            for number, line in enumerate(lines, start=1):
                found = find_sensitive(line)
                assert not found, f"{name}:{number} matches {found}: {line[:120]}"

    def test_no_customer_content_survives_in_asset_paths(
        self, fixture_lines: dict[str, list[str]]
    ) -> None:
        """AEM's asset-processing log names the asset, and that is free text.

        In the source logs those filenames carried the customer's drug brand
        names and their subsidiary's name - 608 distinct filenames, none of
        which any identifier pattern would catch, because they are content
        rather than an id. The sanitiser rewrites them to a stable placeholder
        that keeps the shape.

        This asserts the shape rather than the absence of specific brands: a
        list of brand names is exactly the thing that must not be committed
        here, and the publication scan holds that set from the environment.
        """
        asset = re.compile(r"asset=(?P<value>[A-Za-z0-9_./()\[\]-]+)")
        dam = re.compile(r"/content/dam/(?P<rest>[A-Za-z0-9_./()\[\]-]+)")
        # Either the opaque segment the sanitiser emits, or a path under the
        # placeholder tenant - which is what the hand-written publish-tier
        # fixtures use, and is no more real than `p12345`.
        allowed = re.compile(r"[a-z0-9]{8}|examplecorp(?:/[A-Za-z0-9_.()-]+)*")
        for name, lines in fixture_lines.items():
            for number, line in enumerate(lines, start=1):
                for match in dam.finditer(line):
                    rest = match.group("rest")
                    assert allowed.fullmatch(rest), (
                        f"{name}:{number} has an unredacted DAM path: {rest[:60]}"
                    )
                for match in asset.finditer(line):
                    value = match.group("value")
                    assert "/content/dam/" in value or re.fullmatch(
                        r"ASSET-\d{4}-\d+_[a-z0-9]{10}\.[a-z0-9]+", value
                    ), f"{name}:{number} has an unredacted asset name: {value[:60]}"

    def test_hostnames_use_the_placeholder_program(
        self, fixture_lines: dict[str, list[str]]
    ) -> None:
        host = re.compile(r"\b(author|publish|preview)-p(\d+)-e(\d+)\.adobeaemcloud\.com\b")
        for name, lines in fixture_lines.items():
            for number, line in enumerate(lines, start=1):
                for _tier, program, environment in host.findall(line):
                    assert (program, environment) == ("12345", "67890"), f"{name}:{number}"


class TestCorpusParses:
    def test_the_key_pattern_identifies_every_fixture(self, key_pattern: re.Pattern[str]) -> None:
        for name, expected in EXPECTED_FIXTURES.items():
            coordinates = from_key(name, key_pattern)
            assert LogType(coordinates["log_type"]) is expected, name
            assert coordinates["tier"] in {"author", "publish"}, name

    def test_no_line_in_any_fixture_is_unparsed(self, fixture_lines: dict[str, list[str]]) -> None:
        """The regression guard for the nested-bracket thread-name bug."""
        failures: list[str] = []
        for name, expected in EXPECTED_FIXTURES.items():
            parse = parser_for(expected)
            for number, line in enumerate(fixture_lines[name], start=1):
                if parse(line).metadata.get("parse_status") == "unmatched":
                    failures.append(f"{name}:{number}: {line[:120]}")
        assert not failures, "unparsed lines:\n" + "\n".join(failures[:10])

    def test_every_line_gets_an_event_timestamp(self, fixture_lines: dict[str, list[str]]) -> None:
        for name, expected in EXPECTED_FIXTURES.items():
            parse = parser_for(expected)
            for number, line in enumerate(fixture_lines[name], start=1):
                assert parse(line).timestamp_ns is not None, f"{name}:{number}"

    @pytest.mark.parametrize("name", sorted(EXPECTED_FIXTURES))
    def test_content_sniffing_agrees_with_the_key(
        self, name: str, fixture_lines: dict[str, list[str]]
    ) -> None:
        """The fallback path has to reach the same answer as the key pattern.

        The one exception is documented rather than worked around: aemaccess and
        aemhttpdaccess are byte-for-byte the same format, so content alone cannot
        tell them apart and the key is the only way.
        """
        sniffed = sniff_log_type(fixture_lines[name][0])
        expected = EXPECTED_FIXTURES[name]
        if expected is LogType.AEM_HTTPD_ACCESS:
            assert sniffed is LogType.AEM_ACCESS
        else:
            assert sniffed is expected


class TestCorpusShape:
    """Guards on the corpus keeping the variety that makes it worth having."""

    def test_the_error_log_has_more_than_one_level(
        self, fixture_lines: dict[str, list[str]]
    ) -> None:
        parse = parser_for(LogType.AEM_ERROR)
        levels = collections.Counter(
            record.level for line in fixture_lines["author_aemerror.log"] if (record := parse(line))
        )
        assert set(levels) >= {"info", "warn"}

    def test_the_access_log_has_a_failing_status(self, fixture_lines: dict[str, list[str]]) -> None:
        parse = parser_for(LogType.AEM_ACCESS)
        classes = {
            parse(line).metadata.get("status_class")
            for line in fixture_lines["author_aemaccess.log"]
        }
        assert "2xx" in classes
        assert classes & {"3xx", "4xx", "5xx"}, "no non-2xx line left in the corpus"

    def test_the_request_log_mostly_correlates_but_not_entirely(
        self, fixture_lines: dict[str, list[str]]
    ) -> None:
        """Both halves matter.

        High pairing proves the correlator works on real interleaving. A handful
        of unpaired lines proves the corpus still contains the file-boundary case,
        which is the thing the `correlated="false"` path exists for. An earlier
        stride-sampled corpus paired 6 of 137 and would have passed a
        lower-bound-only assertion by accident.
        """
        parse = parser_for(LogType.AEM_REQUEST)
        correlator = RequestCorrelator()
        for line in fixture_lines["author_aemrequest.log"]:
            correlator.process(parse(line))
        correlator.flush()
        stats = correlator.stats
        responses = stats.paired + stats.unpaired_responses
        assert responses > 50, "corpus has too few response lines to be meaningful"
        assert stats.paired / responses > 0.85
        assert stats.unpaired_responses > 0, "corpus lost the file-boundary case"

    def test_the_cdn_log_has_several_cache_outcomes_and_countries(
        self, fixture_lines: dict[str, list[str]]
    ) -> None:
        parse = parser_for(LogType.AEM_CDN)
        records = [parse(line).metadata for line in fixture_lines["author_aemcdn.log"]]
        assert len({r.get("cache_status") for r in records}) >= 2
        assert len({r.get("country") for r in records}) >= 2

    def test_the_cdn_log_is_valid_json_on_every_line(
        self, fixture_lines: dict[str, list[str]]
    ) -> None:
        """Sanitisation is a regex rewrite, so it could produce invalid JSON."""
        for number, line in enumerate(fixture_lines["author_aemcdn.log"], start=1):
            try:
                json.loads(line)
            except json.JSONDecodeError as error:
                pytest.fail(f"author_aemcdn.log:{number} is not valid JSON: {error}")


class TestCorpusLabelCardinality:
    def test_the_whole_corpus_fits_in_a_small_number_of_streams(
        self, fixture_lines: dict[str, list[str]], key_pattern: re.Pattern[str]
    ) -> None:
        """The cost claim, asserted rather than promised.

        1036 lines across seven log types and two tiers must not produce more
        than a couple of dozen streams. This is the test that fails if someone
        promotes a parsed field to a label.
        """
        streams: set[tuple[tuple[str, str], ...]] = set()
        for name, expected in EXPECTED_FIXTURES.items():
            coordinates = from_key(name, key_pattern)
            tier = tier_from_name(coordinates.get("tier", ""))
            parse = parser_for(expected)
            for line in fixture_lines[name]:
                record = parse(line)
                labels = build_labels(
                    service_name="adobe-aem",
                    log_type=expected,
                    tier=tier,
                    coordinates=coordinates,
                    level=record.level,
                    default_program_id="p12345",
                    default_env_id="e67890",
                    default_env_type="prod",
                )
                streams.add(tuple(sorted(labels.items())))
        assert len(streams) <= 25, f"{len(streams)} streams is too many for one environment"

    def test_only_levelled_log_types_carry_a_level_label(
        self, fixture_lines: dict[str, list[str]], key_pattern: re.Pattern[str]
    ) -> None:
        """A level derived from a status code must never reach the `level` label."""
        for name, expected in EXPECTED_FIXTURES.items():
            parse = parser_for(expected)
            for line in fixture_lines[name]:
                record = parse(line)
                labels = build_labels(
                    service_name="adobe-aem",
                    log_type=expected,
                    tier=Tier.AUTHOR,
                    coordinates={},
                    level=record.level,
                )
                if expected in LEVELLED_TYPES:
                    continue
                assert "level" not in labels, name

    def test_http_log_types_get_an_inferred_detected_level(
        self, fixture_lines: dict[str, list[str]]
    ) -> None:
        """Metadata, not a label, and better than the "unknown" Loki fills in."""
        parse = parser_for(LogType.AEM_CDN)
        inferred = {
            detected_level_for(dict(parse(line).metadata))
            for line in fixture_lines["author_aemcdn.log"]
        }
        assert None not in inferred
        assert inferred <= {"info", "warn", "error"}
