from __future__ import annotations

import os


def runtime_mode() -> str:
    mode = (os.environ.get("APP_RUNTIME") or "flask").strip().lower()
    return mode if mode in {"flask", "legacy"} else "flask"


def is_legacy_mode() -> bool:
    return runtime_mode() == "legacy"

