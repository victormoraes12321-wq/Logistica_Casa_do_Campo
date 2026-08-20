from __future__ import annotations


def handle_get_public(handler, path: str) -> bool:
    if path == "/login":
        handler.send_html(handler.login_page())
        return True
    return False


def handle_get_authenticated(handler, path: str, user) -> bool:
    if path == "/force-password":
        if not handler.must_change_password(user):
            handler.redirect("/dashboard")
            return True
        handler.force_password_page(user)
        return True
    return False


def should_force_password_redirect(handler, user, path: str) -> bool:
    if path == "/force-password":
        return False
    return handler.must_change_password(user)


def handle_post_public(handler, path: str) -> bool:
    if path == "/login":
        handler.post_login()
        return True
    return False


def handle_post_authenticated(handler, path: str, user) -> bool:
    if path == "/force-password":
        handler.post_force_password(user)
        return True
    return False
