from __future__ import annotations

import subprocess
import sys

from docdb_connection_attribution.export import build_resource

_PRINT_ID = (
    "from docdb_connection_attribution.export import build_resource;"
    "print(build_resource('acme-docdb-cluster').attributes['service.instance.id'])"
)


def _instance_id_in_a_fresh_process() -> str:
    return subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-c", _PRINT_ID], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_the_resource_identity_is_stable_across_cold_starts() -> None:
    """Recent OTel SDKs add a random service.instance.id per process, which the Grafana Cloud
    OTLP gateway turns into an `instance` label. Each Lambda cold start would then begin a new
    set of series, and a last_over_time window spanning a cold start would count every
    connection twice. Two interpreter processes stand in for two cold starts."""
    assert _instance_id_in_a_fresh_process() == _instance_id_in_a_fresh_process()


def test_each_cluster_gets_its_own_identity() -> None:
    a = str(build_resource("a").attributes["service.instance.id"])
    b = str(build_resource("b").attributes["service.instance.id"])
    assert a != b
