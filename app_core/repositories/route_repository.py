from __future__ import annotations

import sqlite3


def find_route_by_id(db: sqlite3.Connection, route_id: int):
    return db.execute("SELECT * FROM routes WHERE id=?", (int(route_id),)).fetchone()


def touch_route(db: sqlite3.Connection, route_id: int, updated_at: str) -> None:
    db.execute(
        "UPDATE routes SET updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",
        (updated_at, int(route_id)),
    )


def update_route_status(db: sqlite3.Connection, route_id: int, status: str, updated_at: str) -> None:
    db.execute(
        "UPDATE routes SET status=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",
        (status, updated_at, int(route_id)),
    )

