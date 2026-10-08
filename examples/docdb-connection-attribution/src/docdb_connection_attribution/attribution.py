"""The join: open connections from $currentOp, users from the audit log.

Pure functions, so the part with room to be wrong is testable without AWS or a cluster.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple

UNATTRIBUTED = "<unattributed>"
ACTIVE = "active"
IDLE = "idle"


class SeriesKey(NamedTuple):
    instance: str
    user: str
    app: str
    state: str
    client_address: str | None


class UserRole(NamedTuple):
    user: str
    role: str
    db: str


def attribute(conn: Mapping[str, Any], audited_user: str | None) -> str:
    """Pick the user for one open connection.

    An active connection's own effectiveUsers is authoritative, so it wins over the audit
    match, which is stale if a client port has been reused since. An idle connection carries
    no user, so it has only the audit match. Anything left is almost always a driver's
    unauthenticated heartbeat connection, or one opened before the audit history reaches.
    """
    effective = conn.get("effectiveUsers")
    if isinstance(effective, list) and effective:
        user = effective[0].get("user") if isinstance(effective[0], Mapping) else None
        if user:
            return str(user)
    return audited_user or UNATTRIBUTED


def connection_state(conn: Mapping[str, Any]) -> str:
    """active while an operation is running on the connection at the moment of the snapshot."""
    return ACTIVE if conn.get("active") is True else IDLE


def user_roles(users: Iterable[Mapping[str, Any]]) -> list[UserRole]:
    """Flatten usersInfo output to one (user, role, role db) row per granted role."""
    out: list[UserRole] = []
    for u in users:
        name = u.get("user")
        roles = u.get("roles")
        if not name or not isinstance(roles, list):
            continue
        for r in roles:
            if isinstance(r, Mapping) and r.get("role"):
                out.append(UserRole(str(name), str(r["role"]), str(r.get("db") or "")))
    return out


def client_host(client: str) -> str:
    """ip:port -> ip. IPv6 clients arrive as [addr]:port."""
    host = client.rsplit(":", 1)[0] if ":" in client else client
    return host.strip("[]")


def group_connections(
    instance: str,
    connections: Iterable[Mapping[str, Any]],
    lookup: Callable[[str], str | None],
    *,
    include_client_address: bool,
) -> Counter[SeriesKey]:
    """Count one instance's open connections by user, app, state and optionally client host."""
    counts: Counter[SeriesKey] = Counter()
    for conn in connections:
        client = str(conn.get("client") or "")
        user = attribute(conn, lookup(client) if client else None)
        app = str(conn.get("app") or "")
        counts[
            SeriesKey(
                instance,
                user,
                app,
                connection_state(conn),
                client_host(client) if include_client_address else None,
            )
        ] += 1
    return counts
