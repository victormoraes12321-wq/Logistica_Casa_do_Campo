from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app_core.config import load_config
from app_core.runtime_db import backup_sqlite_database


EXCLUDE_PARTS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "docs/audit_ui",
    "docs/audit_ui_after",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def should_hash(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    if any(part in rel for part in EXCLUDE_PARTS):
        return False
    if path.suffix.lower() in {".py", ".md", ".txt", ".ini", ".json", ".toml", ".yaml", ".yml", ".bat", ".ps1"}:
        return True
    if path.name in {".env.example", "requirements.txt", "README.md", "run.py", "app.py"}:
        return True
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Congela baseline interno de producao (RC).")
    parser.add_argument("--name", default="", help="Nome curto do release candidate (ex.: rc-interno-01).")
    parser.add_argument("--notes", default="", help="Notas de congelamento.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag_name = (args.name or f"rc_interno_{stamp}").strip().replace(" ", "_")

    releases_dir = ROOT_DIR / "releases"
    tags_dir = releases_dir / "tags"
    baseline_dir = releases_dir / tag_name
    for path in (releases_dir, tags_dir, baseline_dir):
        path.mkdir(parents=True, exist_ok=True)

    backup_info: dict[str, str] = {"backend": cfg.db_backend}
    if cfg.db_backend == "sqlite":
        backup_file = baseline_dir / f"backup_pre_refatoracao_{stamp}.sqlite3"
        backup_sqlite_database(str(cfg.sqlite_db_path), str(backup_file))
        backup_info["file"] = str(backup_file)
    else:
        backup_info["file"] = "nao_aplicavel_sqlite"
        backup_info["hint"] = "Use pg_dump para congelamento PostgreSQL."

    hashes: dict[str, str] = {}
    for file_path in ROOT_DIR.rglob("*"):
        if not file_path.is_file():
            continue
        if not should_hash(file_path, ROOT_DIR):
            continue
        rel = file_path.relative_to(ROOT_DIR).as_posix()
        hashes[rel] = sha256_file(file_path)

    manifest = {
        "tag": tag_name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "notes": args.notes,
        "database": backup_info,
        "file_count": len(hashes),
        "file_hashes": hashes,
    }

    manifest_path = baseline_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    tag_path = tags_dir / f"{tag_name}.tag"
    tag_path.write_text(
        f"TAG={tag_name}\nCREATED_AT={manifest['created_at']}\nMANIFEST={manifest_path}\n",
        encoding="utf-8",
    )

    current_rc_path = releases_dir / "current_rc.json"
    current_rc_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    version_path = ROOT_DIR / "VERSION"
    version_path.write_text(f"{tag_name}\n", encoding="utf-8")

    print(f"Baseline congelado com sucesso: {tag_name}")
    print(f"Manifesto: {manifest_path}")
    print(f"Tag interna: {tag_path}")
    if "file" in backup_info:
        print(f"Backup pre-refatoracao: {backup_info['file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

