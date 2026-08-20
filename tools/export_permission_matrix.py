# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "logistica_casa_do_campo.sqlite3"
OUT_MD = ROOT / "docs" / "matriz_permissoes_final.md"
OUT_JSON = ROOT / "docs" / "matriz_permissoes_final.json"


def yes_no(v: int) -> str:
    return "Sim" if int(v or 0) == 1 else "Não"


def main() -> int:
    if not DB_PATH.exists():
        raise SystemExit(f"Banco não encontrado: {DB_PATH}")

    with sqlite3.connect(DB_PATH, timeout=20) as db:
        db.row_factory = sqlite3.Row
        roles = [r["role_name"] for r in db.execute("SELECT DISTINCT role_name FROM role_permissions ORDER BY role_name").fetchall()]
        perms = db.execute(
            """
            SELECT DISTINCT perm
            FROM role_permissions
            ORDER BY perm
            """
        ).fetchall()
        perm_keys = [p["perm"] for p in perms]
        role_matrix = {}
        for role in roles:
            role_matrix[role] = {
                r["perm"]: int(r["allowed"] or 0)
                for r in db.execute("SELECT perm,allowed FROM role_permissions WHERE role_name=?", (role,)).fetchall()
            }

        users = db.execute(
            """
            SELECT id,name,username,role,active,must_change_password
            FROM users
            ORDER BY active DESC, role, name
            """
        ).fetchall()
        user_overrides = {}
        for u in users:
            uid = int(u["id"])
            ov = db.execute(
                "SELECT perm,allowed FROM user_permissions WHERE user_id=? ORDER BY perm",
                (uid,),
            ).fetchall()
            user_overrides[uid] = {r["perm"]: int(r["allowed"] or 0) for r in ov}

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "roles": roles,
        "permissions": perm_keys,
        "role_matrix": role_matrix,
        "users": [
            {
                "id": int(u["id"]),
                "name": u["name"],
                "username": u["username"],
                "role": u["role"],
                "active": int(u["active"] or 0),
                "must_change_password": int(u["must_change_password"] or 0),
                "overrides": user_overrides.get(int(u["id"]), {}),
            }
            for u in users
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append("# Matriz Final de Permissões")
    lines.append("")
    lines.append(f"- Congelada em: **{payload['generated_at']}**")
    lines.append(f"- Banco fonte: `{DB_PATH}`")
    lines.append(f"- Total de perfis: **{len(roles)}**")
    lines.append(f"- Total de permissões: **{len(perm_keys)}**")
    lines.append("")
    lines.append("## Matriz por perfil")
    lines.append("")
    lines.append("| Permissão | " + " | ".join(roles) + " |")
    lines.append("|---|" + "|".join(["---"] * len(roles)) + "|")
    for perm in perm_keys:
        vals = [yes_no(role_matrix.get(role, {}).get(perm, 0)) for role in roles]
        lines.append("| " + perm + " | " + " | ".join(vals) + " |")
    lines.append("")
    lines.append("## Usuários e overrides")
    lines.append("")
    lines.append("| Usuário | Login | Perfil | Ativo | Troca senha próximo login | Overrides explícitos |")
    lines.append("|---|---|---|---|---|---|")
    for u in payload["users"]:
        ov = u.get("overrides", {})
        override_count = len(ov)
        lines.append(
            f"| {u['name']} | {u['username']} | {u['role']} | {yes_no(u['active'])} | "
            f"{yes_no(u['must_change_password'])} | {override_count} |"
        )
    lines.append("")
    lines.append("### Detalhe de overrides por usuário")
    lines.append("")
    for u in payload["users"]:
        ov = u.get("overrides", {})
        if not ov:
            continue
        lines.append(f"- **{u['name']} ({u['username']})**")
        for perm in sorted(ov.keys()):
            lines.append(f"  - `{perm}`: {yes_no(ov[perm])}")
    lines.append("")
    lines.append(f"Arquivo técnico completo: `{OUT_JSON}`")

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Matriz exportada: {OUT_MD}")
    print(f"JSON exportado: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

