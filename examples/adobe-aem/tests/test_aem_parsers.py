"""Parser tests, including the regressions that matter.

Every test asserting on a specific string uses a line taken from the sanitised
fixtures, so the expectations come from real AEM output rather than from what the
regex happens to accept.
"""

from __future__ import annotations

import pytest

from adobe_aem.logtypes import LogType, normalise_level
from adobe_aem.parsers import (
    parse_access,
    parse_cdn,
    parse_clf_timestamp,
    parse_dispatcher,
    parse_error,
    parse_httpd_access,
    parse_httpd_error,
    parse_iso_timestamp,
    parse_request,
    parser_for,
    status_class,
)

ACCESS_LINE = (
    "cm-p12345-e67890-aem-author-fr8h4lctr-qs632 203.0.113.29 - "
    '24/Jul/2026:00:00:04 +0000 "HEAD /systemready HTTP/1.1" 200 - "-" "Java/21.0.11"'
)
ERROR_LINE = (
    "24.07.2026 00:00:00.002 [cm-p12345-e67890-aem-author-fr8h4lctr-6liqc] *INFO* "
    "[sling-default-1-com.adobe.cq.wcm.translation.impl.scheduler"
    ".ScheduleRepeatTranslationProject] "
    "com.adobe.cq.wcm.translation.impl.scheduler.ScheduleRepeatTranslationProject "
    "Starting sync process now"
)
CDN_LINE = (
    '{"timestamp":"2026-07-24T00:00:04+0000","ttfb":58,"ttlb":59,'
    '"cli_ip":"203.0.113.29","cli_country":"CA","cli_region":"CA-QC",'
    '"rid":"26ccf60d-0a9c-4d0f-ad9f-21bc0d95a79f","req_ua":"Java/21",'
    '"aem_envKind":"SKYLINE","aem_tenant":"examplecorp",'
    '"host":"author-p12345-e67890.adobeaemcloud.com","url":"/systemready",'
    '"method":"HEAD","res_ctype":"","cache":"SYNTH","debug":"","res_age":"",'
    '"status":404,"pop":"NYC","rules":"","alerts":"","sample":"","ddos":false}'
)


class TestAccess:
    def test_extracts_every_field(self) -> None:
        record = parse_access(ACCESS_LINE)
        assert record.metadata["node_id"] == "cm-p12345-e67890-aem-author-fr8h4lctr-qs632"
        assert record.metadata["client_ip"] == "203.0.113.29"
        assert record.metadata["method"] == "HEAD"
        assert record.metadata["path"] == "/systemready"
        assert record.metadata["protocol"] == "HTTP/1.1"
        assert record.metadata["status"] == "200"
        assert record.metadata["status_class"] == "2xx"
        assert record.metadata["user_agent"] == "Java/21.0.11"
        assert record.timestamp_ns == parse_clf_timestamp("24/Jul/2026:00:00:04 +0000")

    def test_apache_dashes_become_absent_fields(self) -> None:
        """`-` is Apache's "no value", not a value.

        Stored as a literal dash it would make `| remote_user != ""` match every
        anonymous request, which is the opposite of what it reads as.
        """
        record = parse_access(ACCESS_LINE)
        assert "remote_user" not in record.metadata
        assert "bytes_sent" not in record.metadata
        assert "referer" not in record.metadata

    def test_authenticated_user_is_kept(self) -> None:
        line = ACCESS_LINE.replace(" - 24/Jul", " user123@example.com 24/Jul")
        assert parse_access(line).metadata["remote_user"] == "user123@example.com"

    def test_query_string_stays_on_the_path(self) -> None:
        """It is the only signal separating a cache-busting request from a cacheable one."""
        line = ACCESS_LINE.replace("/systemready", "/search.html?q=widget&page=2")
        assert parse_access(line).metadata["path"] == "/search.html?q=widget&page=2"

    def test_unparseable_line_is_still_shipped(self) -> None:
        record = parse_access("this is not an access log line")
        assert record.metadata["parse_status"] == "unmatched"
        assert record.message == "this is not an access log line"
        assert record.timestamp_ns is None

    def test_httpd_access_shares_the_parser_but_reports_its_own_type(self) -> None:
        record = parse_httpd_access("nonsense")
        assert record.metadata["expected_log_type"] == str(LogType.AEM_HTTPD_ACCESS)


class TestError:
    def test_extracts_every_field(self) -> None:
        record = parse_error(ERROR_LINE)
        assert record.level == "info"
        assert record.metadata["node_id"] == "cm-p12345-e67890-aem-author-fr8h4lctr-6liqc"
        assert record.metadata["logger"].endswith("ScheduleRepeatTranslationProject")
        assert record.metadata["thread"].startswith("sling-default-1-")
        assert record.timestamp_ns is not None

    @pytest.mark.parametrize(
        "thread",
        [
            # AEM's own thread names. A `[^\]]*` thread group truncated at the
            # first `]` and left 41 of 250 real lines unparsed.
            "DocumentDiscoveryLiteService-BackgroundWorker-[4]",
            "[64cae168-3930-4fe2-ad7c-4052ece1779d][projectx_webapp][examplecorp]",
            "sling-default-1-com.adobe.cq.Simple",
        ],
    )
    def test_nested_brackets_in_the_thread_name(self, thread: str) -> None:
        line = f"24.07.2026 00:00:00.002 [node-1] *WARN* [{thread}] com.example.Logger a message"
        record = parse_error(line)
        assert record.metadata.get("parse_status") != "unmatched"
        assert record.metadata["thread"] == thread
        assert record.metadata["logger"] == "com.example.Logger"

    def test_a_message_containing_a_bracket_does_not_eat_the_logger(self) -> None:
        """Why the thread group is not a greedy `.*`.

        AEM emits `ServiceEvent [Foo] REGISTERED` constantly. A greedy group
        backtracks from the end of the line, so it would take
        `node] *INFO* [thread] com.example.Logger Events [Foo` as the thread and
        `REGISTERED` as the logger.
        """
        line = (
            "24.07.2026 00:00:00.155 [node-1] *INFO* [FelixLogListener] "
            "Events.Service.org.apache.sling.event ServiceEvent [QueueMBean] REGISTERED"
        )
        record = parse_error(line)
        assert record.metadata["thread"] == "FelixLogListener"
        assert record.metadata["logger"] == "Events.Service.org.apache.sling.event"

    def test_exception_is_pulled_out_of_the_message(self) -> None:
        line = (
            "24.07.2026 01:02:03.004 [node-1] *ERROR* [thread-2] com.example.Logger "
            "failed: javax.jcr.RepositoryException: session is closed"
        )
        record = parse_error(line)
        assert record.level == "error"
        assert record.metadata["exception"] == "javax.jcr.RepositoryException"

    def test_stack_trace_continuation_is_not_counted_as_unmatched(self) -> None:
        """A trace line is part of the preceding event, not a parse failure.

        Counting it as unmatched would make the unmatched-lines dashboard panel
        useless on any log containing a stack trace, which is every error log.
        """
        record = parse_error("\tat com.day.cq.Foo.bar(Foo.java:42)")
        assert record.metadata["parse_status"] == "stacktrace"

    def test_caused_by_line_yields_its_exception(self) -> None:
        record = parse_error("Caused by: java.util.concurrent.TimeoutException: timed out")
        assert record.metadata["parse_status"] == "stacktrace"
        assert record.metadata["exception"] == "java.util.concurrent.TimeoutException"

    def test_timezone_offset_is_applied(self) -> None:
        """AEM's error log has no timezone, so the offset has to come from config."""
        utc = parse_error(ERROR_LINE).timestamp_ns
        plus_one = parse_error(ERROR_LINE, utc_offset_seconds=3600).timestamp_ns
        assert utc is not None and plus_one is not None
        assert utc - plus_one == 3600 * 1_000_000_000


class TestRequest:
    def test_request_line(self) -> None:
        record = parse_request(
            "24/Jul/2026:00:00:04 +0000 [17722] -> HEAD /systemready HTTP/1.1 [node-a]"
        )
        assert record.metadata["direction"] == "request"
        assert record.metadata["request_id"] == "17722"
        assert record.metadata["method"] == "HEAD"
        assert record.metadata["path"] == "/systemready"
        assert record.metadata["node_id"] == "node-a"

    def test_response_line(self) -> None:
        record = parse_request(
            "24/Jul/2026:00:00:04 +0000 [17722] <- 200 application/json 2ms [node-a]"
        )
        assert record.metadata["direction"] == "response"
        assert record.metadata["status"] == "200"
        assert record.metadata["status_class"] == "2xx"
        assert record.metadata["content_type"] == "application/json"
        assert record.metadata["duration_ms"] == "2"

    def test_content_type_charset_is_dropped(self) -> None:
        """`text/html;charset=utf-8` and `text/html` are the same thing to a panel."""
        record = parse_request(
            "24/Jul/2026:00:00:11 +0000 [17910] <- 200 text/html;charset=utf-8 8ms [node-a]"
        )
        assert record.metadata["content_type"] == "text/html"

    def test_response_in_an_unexpected_shape_still_ships_with_its_direction(self) -> None:
        record = parse_request("24/Jul/2026:00:00:11 +0000 [17910] <- something odd [node-a]")
        assert record.metadata["direction"] == "response"
        assert record.metadata["parse_status"] == "response-unmatched"


class TestDispatcher:
    LINE = (
        "[24/Jul/2026:00:00:03 +0000] [I] [node-a] "
        '"GET /content/examplecorp/en/home.html" 200 18ms [publish-farm] [HIT] "example.com"'
    )

    def test_extracts_every_field(self) -> None:
        record = parse_dispatcher(self.LINE)
        assert record.level == "info"
        assert record.metadata["method"] == "GET"
        assert record.metadata["path"] == "/content/examplecorp/en/home.html"
        assert record.metadata["status"] == "200"
        assert record.metadata["duration_ms"] == "18"
        assert record.metadata["farm"] == "publish-farm"
        assert record.metadata["cache_status"] == "HIT"
        assert record.metadata["host"] == "example.com"

    @pytest.mark.parametrize(
        ("letter", "expected"),
        [("E", "error"), ("W", "warn"), ("I", "info"), ("D", "debug"), ("T", "trace")],
    )
    def test_single_letter_levels(self, letter: str, expected: str) -> None:
        record = parse_dispatcher(self.LINE.replace("[I]", f"[{letter}]"))
        assert record.level == expected

    def test_trailing_fields_are_optional(self) -> None:
        """Dispatcher builds differ in which trailing fields they emit."""
        record = parse_dispatcher(
            '[24/Jul/2026:00:00:29 +0000] [I] [node-a] "GET /a.html" 200 22ms'
        )
        assert record.metadata["status"] == "200"
        assert "farm" not in record.metadata

    def test_diagnostic_line_with_no_request_is_not_a_parse_failure(self) -> None:
        record = parse_dispatcher(
            "[24/Jul/2026:00:00:24 +0000] [D] [node-a] cache invalidation received for /content"
        )
        assert record.metadata["parse_status"] == "message-only"
        assert record.level == "debug"
        assert record.timestamp_ns is not None


class TestHttpdError:
    LINE = (
        "[Fri Jul 24 00:00:11.552104 2026] [proxy:error] [pid 42:tid 140272098711296] "
        "[node-a] AH00898: Error reading from remote server"
    )

    def test_extracts_every_field(self) -> None:
        record = parse_httpd_error(self.LINE)
        assert record.level == "error"
        assert record.metadata["module"] == "proxy"
        assert record.metadata["pid"] == "42"
        assert record.metadata["tid"] == "140272098711296"
        assert record.metadata["node_id"] == "node-a"
        assert record.timestamp_ns is not None

    def test_apache_notice_maps_to_info(self) -> None:
        """`notice` has no Loki equivalent, so it is mapped rather than dropped."""
        record = parse_httpd_error(self.LINE.replace("proxy:error", "mpm_worker:notice"))
        assert record.level == "info"


class TestCdn:
    def test_fields_are_renamed_to_the_shared_vocabulary(self) -> None:
        """One panel must be able to span the CDN and the AEM access log."""
        record = parse_cdn(CDN_LINE)
        assert record.metadata["client_ip"] == "203.0.113.29"
        assert record.metadata["country"] == "CA"
        assert record.metadata["path"] == "/systemready"
        assert record.metadata["user_agent"] == "Java/21"
        assert record.metadata["cache_status"] == "SYNTH"
        assert record.metadata["request_id"] == "26ccf60d-0a9c-4d0f-ad9f-21bc0d95a79f"

    def test_latency_fields_carry_their_unit(self) -> None:
        record = parse_cdn(CDN_LINE)
        assert record.metadata["ttfb_ms"] == "58"
        assert record.metadata["ttlb_ms"] == "59"
        assert "ttfb" not in record.metadata

    def test_empty_fields_are_dropped(self) -> None:
        """Six of the CDN log's 25 fields are `""` on a typical line."""
        record = parse_cdn(CDN_LINE)
        for absent in ("waf_rules", "waf_alerts", "content_type", "response_age_s"):
            assert absent not in record.metadata

    def test_false_ddos_is_kept(self) -> None:
        """The one field where False means something, so its absence cannot."""
        assert parse_cdn(CDN_LINE).metadata["ddos"] == "false"

    def test_status_and_class(self) -> None:
        record = parse_cdn(CDN_LINE)
        assert record.metadata["status"] == "404"
        assert record.metadata["status_class"] == "4xx"

    def test_raw_json_is_the_message(self) -> None:
        """So `| json` still reaches a field a future Adobe release adds."""
        assert parse_cdn(CDN_LINE).message == CDN_LINE

    def test_non_json_is_unmatched_not_an_exception(self) -> None:
        assert parse_cdn("not json at all").metadata["parse_status"] == "unmatched"

    def test_json_that_is_not_an_object(self) -> None:
        assert parse_cdn("[1, 2, 3]").metadata["parse_status"] == "unmatched"


class TestTimestamps:
    def test_clf_is_utc(self) -> None:
        # 2026-07-24T00:00:04Z
        assert parse_clf_timestamp("24/Jul/2026:00:00:04 +0000") == 1784851204 * 10**9

    def test_clf_honours_a_non_zero_offset(self) -> None:
        plus_two = parse_clf_timestamp("24/Jul/2026:02:00:04 +0200")
        assert plus_two == parse_clf_timestamp("24/Jul/2026:00:00:04 +0000")

    def test_iso_with_a_colonless_offset(self) -> None:
        """Adobe writes `+0000`, not `+00:00`."""
        assert parse_iso_timestamp("2026-07-24T00:00:04+0000") == 1784851204 * 10**9

    @pytest.mark.parametrize(
        "bad", ["", "not a timestamp", "99/Xxx/2026:00:00:00 +0000", "24/Jul/abcd:00:00:04 +0000"]
    )
    def test_unparseable_returns_none_rather_than_raising(self, bad: str) -> None:
        """One malformed timestamp must not sink the object it is in."""
        assert parse_clf_timestamp(bad) is None


class TestHelpers:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [("200", "2xx"), ("301", "3xx"), ("404", "4xx"), ("500", "5xx"), ("100", "1xx")],
    )
    def test_status_class(self, status: str, expected: str) -> None:
        assert status_class(status) == expected

    @pytest.mark.parametrize("bad", ["", "-", "20", "6000", "abc", "999"])
    def test_status_class_rejects_a_non_status(self, bad: str) -> None:
        assert status_class(bad) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("*INFO*", "info"),
            ("WARN", "warn"),
            ("WARNING", "warn"),
            ("notice", "info"),
            ("SEVERE", "error"),
            ("finest", "debug"),
        ],
    )
    def test_level_normalisation(self, raw: str, expected: str) -> None:
        assert normalise_level(raw) == expected

    def test_unrecognised_level_is_dropped_not_passed_through(self) -> None:
        """`level` is a label, so one typo would create a permanent stream."""
        assert normalise_level("BANANA") is None

    def test_unknown_log_type_gets_a_passthrough_parser(self) -> None:
        record = parser_for(LogType.UNKNOWN)("anything at all")
        assert record.message == "anything at all"
        assert record.metadata == {}
