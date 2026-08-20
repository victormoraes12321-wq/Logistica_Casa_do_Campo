from __future__ import annotations

import sqlite3


def find_order_by_id(db: sqlite3.Connection, order_id: int):
    return db.execute("SELECT * FROM orders WHERE id=?", (int(order_id),)).fetchone()


def find_order_by_number(db: sqlite3.Connection, order_number: str):
    return db.execute("SELECT * FROM orders WHERE LOWER(order_number)=LOWER(?)", (str(order_number or "").strip(),)).fetchone()


def update_order_status(db: sqlite3.Connection, order_id: int, status: str, updated_at: str) -> None:
    db.execute(
        "UPDATE orders SET status=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",
        (status, updated_at, int(order_id)),
    )

