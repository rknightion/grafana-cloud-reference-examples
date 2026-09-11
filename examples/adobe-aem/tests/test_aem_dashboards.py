"""The shipped dashboards: schema shape, and the LogQL mistake they must not make.

Every panel query here was executed against a live Grafana Cloud Loki tenant
holding real parsed AEM data before being committed - 27 of 28 returned data, the
28th being the unparsed-lines health panel, which is correctly zero when nothing
failed to parse. These tests are what stops that verification silently rotting:
they cannot re-run the queries, but they can hold the structure and the one
syntactic rule that produced an empty panel with no error.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

DASHBOARDS = Path(__file__).resolve().parent.parent / "dashboards"

# Fields this example puts in structured metadata. A metadata field inside a
# `{}` stream selector matches nothing - no error, no warning, just an empty
# panel - so a query naming one there is always a bug.
METADATA_FIELDS = frozenset(
    {
        "bytes_sent",
        "cache_status",
        "client_ip",
        "content_type",
        "correlated",
        "country",
        "ddos",
        "detected_level",
        "direction",
        "duration_ms",
        "exception",
        "farm",
        "host",
        "logger",
        "method",
        "module",
        "node_id",
        "object_key",
        "parse_status",
        "path",
        "pid",
        "pop",
        "protocol",
        "record",
        "referer",
        "region",
        "remote_user",
        "request_id",
        "status",
        "status_class",
        "thread",
        "tid",
        "ttfb_ms",
        "ttlb_ms",
        "user_agent",
    }
)

# The seven labels the example actually sets, plus nothing else.
STREAM_LABELS = frozenset(
    {
        "service_name",
        "log_type",
        "aem_tier",
        "aem_program_id",
        "aem_env_id",
        "aem_env_type",
        "level",
    }
)

SELECTOR = re.compile(r"\{([^}]*)\}")
MATCHER = re.compile(r"([a-z_][a-z0-9_]*)\s*(?:=~|!~|=|!=)")


def dashboard_files() -> list[Path]:
    return sorted(DASHBOARDS.glob("*.json"))


def load(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def queries(spec: dict[str, Any]) -> list[tuple[str, str]]:
    """Every (panel title, expr) in a dashboard."""
    found = []
    for element in spec["elements"].values():
        title = element["spec"]["title"]
        for query in element["spec"]["data"]["spec"]["queries"]:
            found.append((title, query["spec"]["query"]["spec"]["expr"]))
    return found


def test_both_dashboards_are_committed() -> None:
    assert {path.name for path in dashboard_files()} == {
        "traffic-and-performance.json",
        "errors-and-operations.json",
    }


def test_the_committed_json_matches_the_generator() -> None:
    """Otherwise a hand-edit to the JSON is lost on the next regeneration."""
    import sys

    sys.path.insert(0, str(DASHBOARDS.parent / "dev"))
    import build_dashboards

    assert build_dashboards.main(["--check"]) == 0, "run `just aem-dashboards`"


@pytest.mark.parametrize("path", dashboard_files(), ids=lambda p: p.name)
class TestSchema:
    def test_declares_schema_v2(self, path: Path) -> None:
        document = load(path)
        assert document["apiVersion"] == "dashboard.grafana.app/v2"
        assert document["kind"] == "Dashboard"
        assert document["metadata"]["name"]

    def test_every_element_is_placed_and_every_placement_resolves(self, path: Path) -> None:
        """A v2 dashboard separates elements from layout, so the two can disagree.

        An unreferenced element is an invisible panel; a reference to a missing
        element fails to render the whole dashboard.
        """
        spec = load(path)["spec"]
        placed = [item["spec"]["element"]["name"] for item in spec["layout"]["spec"]["items"]]
        assert sorted(placed) == sorted(spec["elements"])
        assert len(placed) == len(set(placed)), "a panel is placed twice"

    def test_panels_do_not_overlap_or_exceed_the_grid(self, path: Path) -> None:
        occupied: set[tuple[int, int]] = set()
        for item in load(path)["spec"]["layout"]["spec"]["items"]:
            box = item["spec"]
            assert box["x"] >= 0
            assert box["x"] + box["width"] <= 24, "a panel runs off the 24-column grid"
            cells = {
                (x, y)
                for x in range(box["x"], box["x"] + box["width"])
                for y in range(box["y"], box["y"] + box["height"])
            }
            assert not cells & occupied, f"{item['spec']['element']['name']} overlaps another panel"
            occupied |= cells

    def test_panel_ids_are_unique(self, path: Path) -> None:
        ids = [element["spec"]["id"] for element in load(path)["spec"]["elements"].values()]
        assert len(ids) == len(set(ids))

    def test_refids_are_unique_within_a_panel(self, path: Path) -> None:
        for element in load(path)["spec"]["elements"].values():
            refs = [q["spec"]["refId"] for q in element["spec"]["data"]["spec"]["queries"]]
            assert len(refs) == len(set(refs)), element["spec"]["title"]

    def test_every_panel_has_a_title_and_at_least_one_query(self, path: Path) -> None:
        for element in load(path)["spec"]["elements"].values():
            assert element["spec"]["title"].strip()
            assert element["spec"]["data"]["spec"]["queries"]

    def test_every_query_targets_the_datasource_variable(self, path: Path) -> None:
        """A hardcoded datasource uid makes the dashboard un-importable elsewhere."""
        for element in load(path)["spec"]["elements"].values():
            for query in element["spec"]["data"]["spec"]["queries"]:
                inner = query["spec"]["query"]
                assert inner["group"] == "loki"
                assert inner["datasource"]["name"] == "$datasource"

    def test_variables_are_declared_and_used(self, path: Path) -> None:
        spec = load(path)["spec"]
        names = {variable["spec"]["name"] for variable in spec["variables"]}
        assert {"datasource", "service", "env_id", "tier"} <= names
        joined = " ".join(expr for _title, expr in queries(spec))
        for name in ("service", "env_id", "tier"):
            assert f"${name}" in joined, f"${name} is declared but never used"

    def test_the_all_value_requires_the_label_to_be_present(self, path: Path) -> None:
        """`.*` also matches a stream missing the label entirely.

        That silently mixes environments that set `aem_env_id` with ones that do
        not, which is how three log types went missing from every panel during
        development.
        """
        for variable in load(path)["spec"]["variables"]:
            if variable["spec"].get("includeAll"):
                assert variable["spec"]["allValue"] == ".+", variable["spec"]["name"]


@pytest.mark.parametrize("path", dashboard_files(), ids=lambda p: p.name)
class TestQueries:
    def test_no_metadata_field_appears_inside_a_stream_selector(self, path: Path) -> None:
        """The mistake that costs an hour: `{log_type="aemcdn", cache_status="HIT"}`.

        It returns an empty result with no error, because `{}` matches labels
        only. The working form is `{log_type="aemcdn"} | cache_status="HIT"`.
        """
        for title, expr in queries(load(path)["spec"]):
            for selector in SELECTOR.findall(expr):
                used = set(MATCHER.findall(selector))
                offenders = used & METADATA_FIELDS
                assert not offenders, (
                    f"{path.name}: panel {title!r} puts {sorted(offenders)} inside a "
                    f"stream selector, which matches nothing. Move it after a `|`."
                )

    def test_stream_selectors_only_use_known_labels(self, path: Path) -> None:
        for title, expr in queries(load(path)["spec"]):
            for selector in SELECTOR.findall(expr):
                unknown = set(MATCHER.findall(selector)) - STREAM_LABELS
                assert not unknown, f"{path.name}: panel {title!r} selects on {sorted(unknown)}"

    def test_every_unwrap_is_inside_an_aggregation(self, path: Path) -> None:
        """An unwrap with no grouping returns one series per log entry.

        Verified live: the bare form came back with a series per line, each
        carrying its full metadata set as labels.
        """
        for title, expr in queries(load(path)["spec"]):
            if "unwrap" not in expr:
                continue
            aggregated = re.search(r"\b(sum|avg|min|max|count|topk|quantile_over_time)\b", expr)
            assert aggregated, f"{path.name}: panel {title!r} unwraps without aggregating"
            if "quantile_over_time" in expr and "by (" not in expr:
                assert re.search(r"\b(max|min|avg|sum|topk)\s*\(", expr), (
                    f"{path.name}: panel {title!r} has a bare quantile_over_time with no "
                    f"`by (...)` and no outer aggregation, so it returns a series per entry"
                )

    def test_no_parser_stage_is_needed(self, path: Path) -> None:
        """The point of extracting fields at write time.

        A `| json` or `| logfmt` in a shipped panel would mean the parsing work
        was wasted and the panel pays to re-parse on every load.
        """
        for title, expr in queries(load(path)["spec"]):
            for stage in ("| json", "| logfmt", "| regexp", "| pattern"):
                assert stage not in expr, f"{path.name}: panel {title!r} uses {stage}"
