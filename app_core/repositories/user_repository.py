from __future__ import annotations

from typing import Any


def find_active_user_by_username(db: Any, username: str):
    return db.execute(
        "SELECT * FROM users WHERE LOWER(username)=LOWER(?) AND active=1",
        (str(username or "").strip(),),
    ).fetchone()


def find_user_by_id(db: Any, user_id: int):
    return db.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone()


def update_user_password_hash(db: Any, user_id: int, password_hash: str) -> None:
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (str(password_hash or ""), int(user_id)))


def update_user_last_login(db: Any, user_id: int, last_login_at: str) -> None:
    db.execute("UPDATE users SET last_login_at=? WHERE id=?", (str(last_login_at or ""), int(user_id)))

