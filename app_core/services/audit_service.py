from __future__ import annotations

from typing import Any


def record_audit(
    db: Any,
    *,
    created_at: str,
    user_id: int | None,
    user_name: str,
    action: str,
    module: str,
    entity: str = "",
    old_value: str = "",
    new_value: str = "",
    notes: str = "",
    source_ip: str = "",
) -> None:
    db.execute(
        "INSERT INTO audit_logs(created_at,user_id,user_name,source_ip,action,module,entity,old_value,new_value,notes) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (created_at, user_id, user_name, source_ip, action, module, entity, old_value, new_value, notes),
    )
