from __future__ import annotations

from typing import Any


def has_permission(
    db: Any,
    *,
    user: Any,
    permission_key: str,
    valid_permissions: set[str],
    normalize_role,
    default_permissions_for_role,
) -> bool:
    if not user or permission_key not in valid_permissions:
        return False
    role_name = normalize_role(user["role"])
    if role_name == "GOD":
        return True
    direct = db.execute(
        "SELECT allowed FROM user_permissions WHERE user_id=? AND perm=?",
        (user["id"], permission_key),
    ).fetchone()
    if direct is not None:
        return bool(int(direct["allowed"] or 0))
    role_row = db.execute(
        "SELECT allowed FROM role_permissions WHERE role_name=? AND perm=?",
        (role_name, permission_key),
    ).fetchone()
    if role_row is not None:
        return bool(int(role_row["allowed"] or 0))
    return permission_key in set(default_permissions_for_role(role_name))

