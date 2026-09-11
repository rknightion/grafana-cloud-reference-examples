from __future__ import annotations

import json

import pytest

from grafana_cloud_common.aws.s3 import (
    RecordFormat,
    S3ObjectReader,
    S3ObjectRef,
    detect_format,
    is_compressed,
)
from grafana_cloud_common.errors import ParseError
from grafana_cloud_common.testing import FakeS3Client


def read(payload: bytes, key: str, *, gzipped: bool = False) -> list[str]:
    reader = S3ObjectReader(client=FakeS3Client(payload, gzipped=gzipped))  # type: ignore[arg-type]
    return [line for _, line in reader.lines(S3ObjectRef("bucket", key))]


class TestDetectFormat:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("app.log", RecordFormat.LINES),
            ("app.log.gz", RecordFormat.LINES),
            ("events.jsonl", RecordFormat.JSONL),
            ("events.ndjson.gz", RecordFormat.JSONL),
            ("export.json", RecordFormat.JSON_ARRAY),
            ("export.json.gz", RecordFormat.JSON_ARRAY),
            ("rows.csv", RecordFormat.CSV),
            ("rows.tsv.gz", RecordFormat.CSV),
        ],
    )
    def test_ignores_the_compression_suffix(self, key: str, expected: RecordFormat) -> None:
        assert detect_format(key) == expected

    @pytest.mark.parametrize(
        ("key", "expected"), [("a.gz", True), ("a.GZIP", True), ("a.log", False)]
    )
    def test_is_compressed_is_case_insensitive(self, key: str, expected: bool) -> None:
        assert is_compressed(key) == expected


class TestPlainLines:
    def test_reads_lines_and_numbers_them_from_one(self) -> None:
        reader = S3ObjectReader(client=FakeS3Client(b"first\nsecond\nthird\n"))  # type: ignore[arg-type]
        assert list(reader.lines(S3ObjectRef("b", "a.log"))) == [
            (1, "first"),
            (2, "second"),
            (3, "third"),
        ]

    def test_skips_blank_lines(self) -> None:
        assert read(b"a\n\n  \nb\n", "a.log") == ["a", "b"]

    def test_strips_crlf(self) -> None:
        assert read(b"a\r\nb\r\n", "a.log") == ["a", "b"]

    def test_reads_gzip(self) -> None:
        assert read(b"a\nb\n", "a.log.gz", gzipped=True) == ["a", "b"]

    def test_replaces_undecodable_bytes_rather_than_losing_the_file(self) -> None:
        lines = read(b"good\n\xff\xfe bad\nalso good\n", "a.log")
        assert len(lines) == 3
        assert lines[0] == "good"


class TestJsonl:
    def test_reemits_each_document_compactly(self) -> None:
        payload = b'{"a": 1,  "b": 2}\n{"c": 3}\n'
        assert read(payload, "a.jsonl") == ['{"a":1,"b":2}', '{"c":3}']

    def test_a_bad_line_names_its_line_number(self) -> None:
        with pytest.raises(ParseError, match="line 2 is not valid JSON"):
            read(b'{"a":1}\nnot json\n', "a.jsonl")


class TestJsonArray:
    def test_each_element_becomes_a_line(self) -> None:
        payload = json.dumps([{"a": 1}, {"a": 2}]).encode()
        assert read(payload, "a.json") == ['{"a":1}', '{"a":2}']

    def test_a_bare_object_becomes_one_line(self) -> None:
        assert read(b'{"a":1}', "a.json") == ['{"a":1}']

    def test_refuses_a_document_over_the_bound(self) -> None:
        reader = S3ObjectReader(
            client=FakeS3Client(json.dumps([{"a": "x" * 500}] * 20).encode()),  # type: ignore[arg-type]
            max_json_bytes=100,
        )
        with pytest.raises(ParseError, match="Re-export as JSON Lines"):
            list(reader.lines(S3ObjectRef("b", "a.json")))


class TestCsv:
    def test_each_row_becomes_a_json_object(self) -> None:
        payload = b"level,message\ninfo,started\nwarn,slow\n"
        assert read(payload, "a.csv") == [
            '{"level":"info","message":"started"}',
            '{"level":"warn","message":"slow"}',
        ]

    def test_tsv_uses_a_tab_delimiter_even_when_gzipped(self) -> None:
        payload = b"level\tmessage\ninfo\tstarted\n"
        assert read(payload, "a.tsv.gz", gzipped=True) == ['{"level":"info","message":"started"}']

    def test_a_header_only_file_yields_nothing(self) -> None:
        assert read(b"level,message\n", "a.csv") == []

    def test_refuses_a_file_with_no_header(self) -> None:
        with pytest.raises(ParseError, match="no header row"):
            read(b"", "a.csv")


class TestStreaming:
    def test_passes_the_version_id_through_to_s3(self) -> None:
        client = FakeS3Client(b"a\n")
        reader = S3ObjectReader(client=client)  # type: ignore[arg-type]
        list(reader.lines(S3ObjectRef("b", "k", version_id="v-1")))
        assert client.calls[0] == {"Bucket": "b", "Key": "k", "VersionId": "v-1"}

    def test_an_explicit_format_overrides_the_key_suffix(self) -> None:
        """The producer that writes JSON Lines into a .txt is the reason this
        override exists."""
        reader = S3ObjectReader(client=FakeS3Client(b'{"a":1}\n'))  # type: ignore[arg-type]
        lines = list(reader.lines(S3ObjectRef("b", "a.txt"), record_format=RecordFormat.JSONL))
        assert lines == [(1, '{"a":1}')]
