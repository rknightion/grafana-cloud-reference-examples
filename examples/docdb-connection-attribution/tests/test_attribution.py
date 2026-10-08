from __future__ import annotations

from typing import Any

from docdb_connection_attribution.attribution import (
    ACTIVE,
    IDLE,
    UNATTRIBUTED,
    SeriesKey,
    UserRole,
    attribute,
    group_connections,
    user_roles,
)


def test_an_active_connection_uses_its_own_user_over_a_stale_audit_match() -> None:
    conn = {"client": "192.0.2.10:5000", "effectiveUsers": [{"user": "live", "db": "admin"}]}
    assert attribute(conn, "stale") == "live"


def test_an_idle_connection_takes_the_audit_match_or_is_unattributed() -> None:
    assert attribute({"client": "192.0.2.10:5000"}, "app_leaky") == "app_leaky"
    assert attribute({"client": "192.0.2.10:5000"}, None) == UNATTRIBUTED


def test_grouping_counts_by_user_app_and_state_and_strips_the_port() -> None:
    # Shapes as $currentOp returns them: only a connection running an operation is active
    # and carries effectiveUsers; an idle one has active: false and no user.
    conns: list[dict[str, Any]] = [
        {"client": "192.0.2.10:5000", "app": "batch", "active": False},
        {"client": "192.0.2.10:5001", "app": "batch", "active": False},
        {
            "client": "192.0.2.10:5002",
            "app": "batch",
            "active": True,
            "effectiveUsers": [{"user": "leaky", "db": "admin"}],
        },
        {"client": "[2001:db8::1]:6000", "app": "api", "active": False},
        {"client": "192.0.2.20:7000"},
    ]
    users = {"192.0.2.10:5000": "leaky", "192.0.2.10:5001": "leaky", "[2001:db8::1]:6000": "svc"}
    counts = group_connections("i1", conns, users.get, include_client_address=True)
    assert counts == {
        SeriesKey("i1", "leaky", "batch", IDLE, "192.0.2.10"): 2,
        SeriesKey("i1", "leaky", "batch", ACTIVE, "192.0.2.10"): 1,
        SeriesKey("i1", "svc", "api", IDLE, "2001:db8::1"): 1,
        SeriesKey("i1", UNATTRIBUTED, "", IDLE, "192.0.2.20"): 1,
    }


def test_client_address_is_left_out_unless_asked_for() -> None:
    counts = group_connections(
        "i1",
        [{"client": "192.0.2.10:5000", "app": "a"}],
        lambda _: None,
        include_client_address=False,
    )
    assert list(counts) == [SeriesKey("i1", UNATTRIBUTED, "a", IDLE, None)]


def test_user_roles_gives_one_row_per_grant_and_skips_malformed_entries() -> None:
    users: list[dict[str, Any]] = [
        {
            "user": "svc",
            "roles": [{"role": "read", "db": "app"}, {"role": "clusterMonitor", "db": "admin"}],
        },
        {"user": "norole", "roles": []},
        {"user": "broken"},
        {"roles": [{"role": "read", "db": "app"}]},
    ]
    assert user_roles(users) == [
        UserRole("svc", "read", "app"),
        UserRole("svc", "clusterMonitor", "admin"),
    ]
