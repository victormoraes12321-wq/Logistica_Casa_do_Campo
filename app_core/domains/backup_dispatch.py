from __future__ import annotations


def handle_post(handler, path: str, user) -> bool:
    if path == "/backup/create":
        if not handler.require_perm(user, "create_backup", "Sem permissão para gerar backup."):
            return True
        handler.post_backup(user)
        return True
    if path == "/backup/restore":
        if not handler.require_perm(user, "restore_backup", "Sem permissão para restaurar backup."):
            return True
        handler.post_backup_restore(user)
        return True
    return False

