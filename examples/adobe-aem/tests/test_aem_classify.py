"""Key-pattern extraction and content sniffing."""

from __future__ import annotations

import re

import pytest

from adobe_aem.classify import (
    DEFAULT_KEY_PATTERN,
    ClassificationError,
    compile_key_pattern,
    from_key,
    log_type_from_name,
    sniff_log_type,
    tier_from_name,
)
from adobe_aem.logtypes import LogType, Tier


class TestCompile:
    def test_a_pattern_with_no_recognised_group_is_rejected(self) -> None:
        """It would compile, match every object, and extract nothing.

        That is indistinguishable from working, and the consequence is a month
        of logs in the `unknown` stream, so it has to fail at cold start.
        """
        with pytest.raises(ClassificationError, match="no recognised named group"):
            compile_key_pattern(r"(?P<something_else>\w+)")

    def test_an_invalid_regex_is_rejected_with_the_regex_error(self) -> None:
        with pytest.raises(ClassificationError, match="not a valid regex"):
            compile_key_pattern(r"(?P<tier>")

    def test_one_recognised_group_is_enough(self) -> None:
        assert compile_key_pattern(r"(?P<log_type>aemaccess)")

    def test_unrecognised_groups_alongside_recognised_ones_are_tolerated(self) -> None:
        """A customer may want a named group for readability; that is not an error."""
        pattern = compile_key_pattern(r"(?P<date>\d{4})/(?P<log_type>aemaccess)")
        assert from_key("2026/aemaccess", pattern) == {"log_type": "aemaccess"}


class TestDefaultPattern:
    @pytest.fixture
    def pattern(self) -> re.Pattern[str]:
        return compile_key_pattern(DEFAULT_KEY_PATTERN)

    @pytest.mark.parametrize(
        ("key", "tier", "log_type"),
        [
            ("author_aemaccess_2026-07-24.log", "author", "aemaccess"),
            # Searched rather than anchored, so any prefix is ignored. This is
            # what makes the default work on a real bucket whose layout Adobe
            # does not document.
            ("logs/2026/07/24/author_aemerror_2026-07-24.log", "author", "aemerror"),
            ("aem/publish_aemdispatcher_2026-07-24.log.gz", "publish", "aemdispatcher"),
            ("preview_aemrequest.log", "preview", "aemrequest"),
            ("publish.aemhttpderror.log", "publish", "aemhttpderror"),
            ("publish-aemhttpdaccess-2026-07-24.log", "publish", "aemhttpdaccess"),
        ],
    )
    def test_extracts_tier_and_log_type(
        self, pattern: re.Pattern[str], key: str, tier: str, log_type: str
    ) -> None:
        assert from_key(key, pattern) == {"tier": tier, "log_type": log_type}

    def test_adobes_cdn_alias_maps_to_the_documented_type(self, pattern: re.Pattern[str]) -> None:
        """Adobe's download names it `cdn`; the forwarding config calls it `aemcdn`.

        Two names for one log type would be two Loki streams for one thing.
        """
        coordinates = from_key("author_cdn_2026-07-24.log", pattern)
        assert log_type_from_name(coordinates["log_type"]) is LogType.AEM_CDN

    def test_a_key_that_matches_nothing_yields_nothing(self, pattern: re.Pattern[str]) -> None:
        assert from_key("some/unrelated/object.txt", pattern) == {}

    def test_the_default_carries_no_environment_coordinates(self, pattern: re.Pattern[str]) -> None:
        """Adobe does not put them in the filename, so they come from config."""
        coordinates = from_key("author_aemaccess_2026-07-24.log", pattern)
        assert "program_id" not in coordinates
        assert "env_id" not in coordinates


class TestCustomPattern:
    def test_a_full_coordinate_pattern(self) -> None:
        pattern = compile_key_pattern(
            r"p(?P<program_id>\d+)/e(?P<env_id>\d+)/(?P<env_type>dev|stage|prod)/"
            r"(?P<tier>author|publish|preview)/(?P<log_type>aem[a-z]+)/"
        )
        assert from_key("p12345/e67890/prod/publish/aemcdn/2026-07-24.log", pattern) == {
            "program_id": "12345",
            "env_id": "67890",
            "env_type": "prod",
            "tier": "publish",
            "log_type": "aemcdn",
        }

    def test_an_empty_capture_is_treated_as_absent(self) -> None:
        """So an optional group that matched nothing does not set an empty label."""
        pattern = compile_key_pattern(r"(?P<tier>author)(?P<log_type>aemaccess)?")
        assert from_key("author", pattern) == {"tier": "author"}


class TestSniffing:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            (
                "24.07.2026 00:00:00.002 [node] *INFO* [thread] com.example.X msg",
                LogType.AEM_ERROR,
            ),
            (
                "24/Jul/2026:00:00:04 +0000 [17722] -> HEAD /systemready HTTP/1.1 [node]",
                LogType.AEM_REQUEST,
            ),
            (
                "24/Jul/2026:00:00:04 +0000 [17722] <- 200 application/json 2ms [node]",
                LogType.AEM_REQUEST,
            ),
            (
                '[24/Jul/2026:00:00:03 +0000] [I] [node] "GET /a.html" 200 18ms',
                LogType.AEM_DISPATCHER,
            ),
            (
                "[Fri Jul 24 00:00:01.093820 2026] [mpm_worker:notice] [pid 1] msg",
                LogType.AEM_HTTPD_ERROR,
            ),
            (
                'node 203.0.113.1 - 24/Jul/2026:00:00:04 +0000 "GET /a HTTP/1.1" 200 1',
                LogType.AEM_ACCESS,
            ),
            ('{"cli_ip":"203.0.113.1","rid":"x","pop":"NYC"}', LogType.AEM_CDN),
        ],
    )
    def test_identifies_each_format(self, line: str, expected: LogType) -> None:
        assert sniff_log_type(line) is expected

    @pytest.mark.parametrize("line", ["", "   ", "a plain sentence", "{not json", "{}", "[]"])
    def test_unrecognised_content_is_unknown_rather_than_a_guess(self, line: str) -> None:
        assert sniff_log_type(line) is LogType.UNKNOWN

    def test_unrelated_json_is_not_mistaken_for_a_cdn_line(self) -> None:
        """Two AEM CDN marker fields are required, so arbitrary JSON does not match."""
        assert sniff_log_type('{"level":"info","message":"hello"}') is LogType.UNKNOWN

    def test_one_marker_is_not_enough(self) -> None:
        assert sniff_log_type('{"cli_ip":"203.0.113.1"}') is LogType.UNKNOWN

    def test_dispatcher_is_not_confused_with_the_request_log(self) -> None:
        """Both start with a CLF timestamp; only the dispatcher brackets it."""
        assert (
            sniff_log_type('[24/Jul/2026:00:00:03 +0000] [I] [node] "GET /a" 200 1ms')
            is LogType.AEM_DISPATCHER
        )


class TestNameMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("aemaccess", LogType.AEM_ACCESS),
            ("AEMCDN", LogType.AEM_CDN),
            ("cdn", LogType.AEM_CDN),
            ("httpdaccess", LogType.AEM_HTTPD_ACCESS),
            ("nonsense", LogType.UNKNOWN),
        ],
    )
    def test_log_type_from_name(self, raw: str, expected: LogType) -> None:
        assert log_type_from_name(raw) is expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("author", Tier.AUTHOR),
            ("PUBLISH", Tier.PUBLISH),
            ("dispatcher", Tier.DISPATCHER),
            ("", Tier.UNKNOWN),
            ("something", Tier.UNKNOWN),
        ],
    )
    def test_tier_from_name(self, raw: str, expected: Tier) -> None:
        assert tier_from_name(raw) is expected
