from __future__ import annotations


def handle_post(handler, path: str, user) -> bool:
    if path == "/settings":
        if not handler.require_perm(user, "manage_settings", "Sem permissão para alterar configurações."):
            return True
        handler.post_settings(user)
        return True
    if path == "/settings/user":
        if not handler.require_perm(user, "manage_users", "Sem permissão para gerenciar usuários."):
            return True
        handler.post_user(user)
        return True
    if path.startswith("/settings/user/") and path.endswith("/delete"):
        if not handler.require_perm(user, "manage_users", "Sem permissão para gerenciar usuários."):
            return True
        handler.post_user_delete(user, handler.path_int(path, 3))
        return True
    if path.startswith("/settings/user/") and path.endswith("/update"):
        if not handler.require_perm(user, "manage_users", "Sem permissão para gerenciar usuários."):
            return True
        handler.post_user_update(user, handler.path_int(path, 3))
        return True
    if path.startswith("/settings/user/") and path.endswith("/reset-password"):
        if not handler.require_perm(user, "manage_users", "Sem permissão para gerenciar usuários."):
            return True
        handler.post_user_reset_password(user, handler.path_int(path, 3))
        return True
    if path.startswith("/settings/user/") and path.endswith("/purge"):
        if not handler.require_perm(user, "manage_users", "Sem permissão para gerenciar usuários."):
            return True
        handler.post_user_purge(user, handler.path_int(path, 3))
        return True
    if path == "/settings/users/default-passwords":
        if not handler.require_perm(user, "manage_users", "Sem permissão para gerenciar usuários."):
            return True
        handler.post_users_default_passwords(user)
        return True
    if path == "/settings/permissions/role":
        if not handler.require_perm(user, "manage_permissions", "Sem permissão para gerenciar permissões."):
            return True
        handler.post_role_permissions(user)
        return True
    if path.startswith("/settings/permissions/user/") and path.endswith("/update"):
        if not handler.require_perm(user, "manage_permissions", "Sem permissão para gerenciar permissões."):
            return True
        handler.post_user_permissions(user, handler.path_int(path, 4))
        return True
    if path == "/settings/profile":
        if not handler.require_perm(user, "view_settings", "Sem permissão para alterar seu perfil."):
            return True
        handler.post_profile(user)
        return True
    if path == "/sla/holiday":
        if not handler.require_perm(user, "manage_sla", "Sem permissão para gerenciar feriados do SLA."):
            return True
        handler.post_holiday(user)
        return True
    if path.startswith("/sla/holiday/") and path.endswith("/delete"):
        if not handler.require_perm(user, "manage_sla", "Sem permissão para gerenciar feriados do SLA."):
            return True
        handler.post_holiday_delete(user, handler.path_int(path, 3))
        return True
    if path == "/sla/recalculate":
        if not handler.require_perm(user, "manage_sla", "Sem permissão para recalcular SLA."):
            return True
        handler.post_sla_recalculate(user)
        return True
    return False

