# -*- coding: utf-8 -*-
"""Reset seguro para preparar o banco para produção.

Fluxo:
1) gera backup consistente antes de qualquer alteração;
2) limpa dados operacionais (pedidos, cargas, histórico e logs);
3) opcionalmente remove cadastros-base (flag --wipe-master-data);
4) preserva/recria admin, permissões e configurações padrão essenciais;
5) emite relatório antes/depois.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "logistica_casa_do_campo.sqlite3"
DEFAULT_BACKUP_DIR = ROOT / "backups"
DEFAULT_REPORT_PATH = ROOT / "logs" / "reset_producao_report.txt"

ROLES = ["GOD", "Admin", "Gestor", "Faturamento", "Expedicao", "Motorista", "Operador", "Consulta"]
PERMISSION_KEYS = [
    "view_dashboard",
    "view_orders",
    "create_orders",
    "edit_orders",
    "cancel_orders",
    "invoice_orders",
    "register_delivery_problem",
    "view_clients",
    "manage_clients",
    "view_drivers",
    "manage_drivers",
    "view_vehicles",
    "manage_vehicles",
    "view_route_catalog",
    "manage_route_catalog",
    "view_routes",
    "create_routes",
    "edit_routes",
    "cancel_routes",
    "settle_routes",
    "view_sla",
    "manage_sla",
    "view_reports",
    "export_reports",
    "view_settings",
    "manage_settings",
    "manage_users",
    "manage_permissions",
    "view_backup",
    "create_backup",
    "restore_backup",
]

ROLE_DEFAULT_PERMISSIONS = {
    "GOD": set(PERMISSION_KEYS),
    "Admin": set(PERMISSION_KEYS) - {"manage_permissions", "restore_backup"},
    "Gestor": {
        "view_dashboard",
        "view_orders",
        "create_orders",
        "edit_orders",
        "cancel_orders",
        "invoice_orders",
        "register_delivery_problem",
        "view_clients",
        "manage_clients",
        "view_drivers",
        "manage_drivers",
        "view_vehicles",
        "manage_vehicles",
        "view_route_catalog",
        "manage_route_catalog",
        "view_routes",
        "create_routes",
        "edit_routes",
        "cancel_routes",
        "settle_routes",
        "view_sla",
        "view_reports",
        "export_reports",
        "view_settings",
    },
    "Faturamento": {
        "view_dashboard",
        "view_orders",
        "edit_orders",
        "invoice_orders",
        "view_clients",
        "view_reports",
        "view_sla",
    },
    "Expedicao": {
        "view_dashboard",
        "view_orders",
        "edit_orders",
        "register_delivery_problem",
        "view_drivers",
        "view_vehicles",
        "view_route_catalog",
        "view_routes",
        "create_routes",
        "edit_routes",
        "settle_routes",
        "view_reports",
    },
    "Motorista": {
        "view_dashboard",
        "view_orders",
        "view_routes",
        "settle_routes",
        "register_delivery_problem",
    },
    "Operador": {
        "view_dashboard",
        "view_orders",
        "create_orders",
        "edit_orders",
        "cancel_orders",
        "invoice_orders",
        "register_delivery_problem",
        "view_clients",
        "manage_clients",
        "view_drivers",
        "view_vehicles",
        "view_route_catalog",
        "view_routes",
        "create_routes",
        "edit_routes",
        "settle_routes",
        "view_reports",
        "view_sla",
    },
    "Consulta": {
        "view_dashboard",
        "view_orders",
        "view_clients",
        "view_drivers",
        "view_vehicles",
        "view_route_catalog",
        "view_routes",
        "view_reports",
        "view_sla",
        "view_settings",
    },
}

SETTINGS_DEFAULTS = {
    "system_name": "Logística Casa do Campo",
    "company_name": "Casa do Campo",
    "company_subtitle": "Sua melhor opção",
    "primary_color": "#d90429",
    "secondary_color": "#ffbf1f",
    "accent_color": "#174f2a",
    "background_color": "#f6f7f2",
    "load_capacity_kg": "11000",
    "sla_limit_days": "15",
    "sla_ideal_days": "10",
    "logo_file": "/static/logo.png",
}

OPERATIONAL_TABLES = [
    "route_orders",
    "delivery_problems",
    "attachments",
    "order_history",
    "order_items",
    "orders",
    "routes",
    "audit_logs",
]

MASTER_TABLES = ["clients", "drivers", "vehicles", "route_cities", "holidays"]

TEST_PATTERNS = [
    "%qa%",
    "%teste%",
    "%test%",
    "%smk%",
    "%smoke%",
    "%ux%",
    "%caos%",
    "%dummy%",
    "%demo%",
    "%tmp%",
    "%invalida%",
    "%valida%",
]


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def hash_password(raw_password: str) -> str:
    pwd = str(raw_password or "")
    iterations = 120_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def db_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def safe_backup(db_path: Path, backup_dir: Path) -> Path:
    ensure_dir(backup_dir)
    backup_path = backup_dir / f"backup_pre_reset_producao_{stamp()}.sqlite3"
    src = db_connect(db_path)
    dst = sqlite3.connect(str(backup_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup_path


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone()
    return bool(row)


def count_tables(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    result: dict[str, int] = {}
    for r in rows:
        name = str(r["name"])
        result[name] = int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
    return result


def reset_sequences(conn: sqlite3.Connection, tables: list[str]) -> None:
    if not table_exists(conn, "sqlite_sequence"):
        return
    for t in tables:
        conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (t,))


def cleanup_operational(conn: sqlite3.Connection) -> dict[str, int]:
    removed: dict[str, int] = {}
    for table in OPERATIONAL_TABLES:
        if not table_exists(conn, table):
            removed[table] = 0
            continue
        removed[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        conn.execute(f'DELETE FROM "{table}"')
    reset_sequences(conn, OPERATIONAL_TABLES)
    return removed


def cleanup_master_test_rows(conn: sqlite3.Connection) -> dict[str, int]:
    removed: dict[str, int] = {"clients": 0, "drivers": 0, "vehicles": 0, "route_cities": 0}
    pattern_params = tuple(TEST_PATTERNS)

    # Clientes com padrão de teste.
    if table_exists(conn, "clients"):
        q = " OR ".join(["LOWER(COALESCE(name,'')) LIKE LOWER(?)"] * len(TEST_PATTERNS))
        sql = f"SELECT id FROM clients WHERE {q}"
        ids = [int(r["id"]) for r in conn.execute(sql, pattern_params).fetchall()]
        for cid in ids:
            conn.execute("DELETE FROM clients WHERE id=?", (cid,))
        removed["clients"] = len(ids)

    # Motoristas com padrão de teste.
    if table_exists(conn, "drivers"):
        q = " OR ".join(["LOWER(COALESCE(name,'')) LIKE LOWER(?)"] * len(TEST_PATTERNS))
        sql = f"SELECT id FROM drivers WHERE {q}"
        ids = [int(r["id"]) for r in conn.execute(sql, pattern_params).fetchall()]
        for did in ids:
            conn.execute("DELETE FROM drivers WHERE id=?", (did,))
        removed["drivers"] = len(ids)

    # Veículos com padrão de teste.
    if table_exists(conn, "vehicles"):
        q = " OR ".join(["LOWER(COALESCE(name,'')) LIKE LOWER(?) OR LOWER(COALESCE(type,'')) LIKE LOWER(?)"] * len(TEST_PATTERNS))
        params = []
        for p in TEST_PATTERNS:
            params.extend([p, p])
        sql = f"SELECT id FROM vehicles WHERE {q}"
        ids = [int(r["id"]) for r in conn.execute(sql, tuple(params)).fetchall()]
        for vid in ids:
            conn.execute("DELETE FROM vehicles WHERE id=?", (vid,))
        removed["vehicles"] = len(ids)

    # Cidades/rotas-base com padrão de teste.
    if table_exists(conn, "route_cities"):
        q = " OR ".join(
            [
                "LOWER(COALESCE(route_name,'')) LIKE LOWER(?) OR LOWER(COALESCE(city,'')) LIKE LOWER(?) OR LOWER(COALESCE(notes,'')) LIKE LOWER(?)"
            ]
            * len(TEST_PATTERNS)
        )
        params = []
        for p in TEST_PATTERNS:
            params.extend([p, p, p])
        sql = f"SELECT id FROM route_cities WHERE {q}"
        ids = [int(r["id"]) for r in conn.execute(sql, tuple(params)).fetchall()]
        for rid in ids:
            conn.execute("DELETE FROM route_cities WHERE id=?", (rid,))
        removed["route_cities"] = len(ids)

    return removed


def wipe_master_data(conn: sqlite3.Connection) -> dict[str, int]:
    removed: dict[str, int] = {}
    for table in MASTER_TABLES:
        if not table_exists(conn, table):
            removed[table] = 0
            continue
        removed[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        conn.execute(f'DELETE FROM "{table}"')
    reset_sequences(conn, MASTER_TABLES)
    return removed


def ensure_default_settings(conn: sqlite3.Connection) -> None:
    for key, value in SETTINGS_DEFAULTS.items():
        row = conn.execute("SELECT key FROM settings WHERE key=?", (key,)).fetchone()
        if row:
            continue
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)",
            (key, str(value), now_iso()),
        )


def normalize_role(role: str) -> str:
    raw = str(role or "").strip().lower()
    for r in ROLES:
        if r.lower() == raw:
            return r
    return "Operador"


def default_initial_password(username: str) -> str:
    return f"{str(username or '').strip()}123"


def ensure_admin_and_permissions(conn: sqlite3.Connection, admin_password: str) -> dict[str, str]:
    report = {}
    # Normaliza roles existentes para valores suportados.
    users = conn.execute("SELECT id, role FROM users").fetchall()
    for u in users:
        canon = normalize_role(u["role"])
        if canon != (u["role"] or ""):
            conn.execute("UPDATE users SET role=? WHERE id=?", (canon, int(u["id"])))

    # Garante usuário admin GOD ativo.
    admin = conn.execute(
        "SELECT id,username,active,role FROM users WHERE LOWER(username)=LOWER('admin') ORDER BY id LIMIT 1"
    ).fetchone()
    if admin:
        conn.execute(
            "UPDATE users SET role='GOD', active=1 WHERE id=?",
            (int(admin["id"]),),
        )
        report["admin_action"] = f"admin_preservado_id_{int(admin['id'])}"
    else:
        cur = conn.execute(
            "INSERT INTO users(name,username,password_hash,role,active,created_at) VALUES(?,?,?,?,1,?)",
            ("Administrador GOD", "admin", hash_password(admin_password), "GOD", now_iso()),
        )
        report["admin_action"] = f"admin_criado_id_{int(cur.lastrowid)}"

    # Se admin já existia, opcionalmente reaplica senha via env.
    if os.environ.get("LOGISTICA_FORCE_ADMIN_PASSWORD_RESET", "").strip() == "1":
        conn.execute(
            "UPDATE users SET password_hash=? WHERE LOWER(username)=LOWER('admin')",
            (hash_password(admin_password),),
        )
        report["admin_password"] = "senha_redefinida_via_env"
    else:
        report["admin_password"] = "senha_preservada"

    # Garante matriz role_permissions completa.
    now = now_iso()
    for role in ROLES:
        allowed_set = ROLE_DEFAULT_PERMISSIONS.get(role, set())
        for perm in PERMISSION_KEYS:
            allowed = 1 if perm in allowed_set else 0
            row = conn.execute(
                "SELECT allowed FROM role_permissions WHERE role_name=? AND perm=?",
                (role, perm),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO role_permissions(role_name,perm,allowed,updated_at) VALUES(?,?,?,?)",
                    (role, perm, allowed, now),
                )

    # Remove permissões órfãs de usuário.
    conn.execute(
        "DELETE FROM user_permissions WHERE user_id NOT IN (SELECT id FROM users)"
    )
    report["permissions_action"] = "role_permissions_conferidas_e_orfas_removidas"
    return report


def apply_first_access_passwords(conn: sqlite3.Connection, include_inactive: bool = False) -> dict[str, int]:
    where_clause = "" if include_inactive else " WHERE active=1"
    rows = conn.execute(f"SELECT id, username FROM users{where_clause}").fetchall()
    changed = 0
    for row in rows:
        username = str(row["username"] or "").strip()
        if not username:
            continue
        conn.execute(
            "UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
            (hash_password(default_initial_password(username)), int(row["id"])),
        )
        changed += 1
    return {"changed_users": changed, "target_scope": len(rows)}


def clear_generated_artifacts(root: Path) -> dict[str, str]:
    actions: dict[str, str] = {}
    csv_report = root / "data" / "relatorio_pedidos.csv"
    if csv_report.exists():
        csv_report.unlink()
        actions["relatorio_csv"] = "removido"
    else:
        actions["relatorio_csv"] = "ausente"

    for rel in ["logs/runtime.log", "logs/runtime.err.log", "logs/server_errors.log"]:
        fp = root / rel
        if fp.exists():
            fp.write_text("", encoding="utf-8")
            actions[rel] = "limpo"
        else:
            actions[rel] = "ausente"
    return actions


def write_report(path: Path, lines: list[str]) -> None:
    ensure_dir(path.parent)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset seguro para produção do Logística Casa do Campo.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Caminho do banco SQLite.")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR), help="Diretório para backups.")
    parser.add_argument(
        "--wipe-master-data",
        action="store_true",
        help="Também zera clientes/motoristas/veículos/cidades-rotas/feriados.",
    )
    parser.add_argument(
        "--admin-password",
        default=os.environ.get("LOGISTICA_ADMIN_PASSWORD", "admin123"),
        help="Senha do admin caso seja necessário recriar/resetar.",
    )
    parser.add_argument(
        "--force-default-passwords",
        action="store_true",
        help="Aplica senha padrão usuario123 para usuários ativos e exige troca no próximo login.",
    )
    parser.add_argument(
        "--include-inactive-on-default-passwords",
        action="store_true",
        help="Inclui usuários inativos ao aplicar --force-default-passwords.",
    )
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH), help="Arquivo de relatório final.")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    backup_dir = Path(args.backup_dir).resolve()
    report_path = Path(args.report).resolve()

    if not db_path.exists():
        print(f"ERRO: banco não encontrado em {db_path}")
        return 1

    backup_path = safe_backup(db_path, backup_dir)
    report_lines = [
        "RESET PRODUÇÃO - LOGÍSTICA CASA DO CAMPO",
        f"Data: {now_iso()}",
        f"Banco: {db_path}",
        f"Backup gerado: {backup_path}",
        "",
    ]

    conn = db_connect(db_path)
    try:
        before_counts = count_tables(conn)
        report_lines.append("Contagens antes:")
        for k in sorted(before_counts):
            report_lines.append(f"- {k}: {before_counts[k]}")
        report_lines.append("")

        conn.execute("BEGIN")
        removed_oper = cleanup_operational(conn)
        removed_test_master = cleanup_master_test_rows(conn)
        removed_master = {}
        if args.wipe_master_data:
            removed_master = wipe_master_data(conn)
        admin_report = ensure_admin_and_permissions(conn, args.admin_password)
        first_access_report = {}
        if args.force_default_passwords:
            first_access_report = apply_first_access_passwords(
                conn,
                include_inactive=bool(args.include_inactive_on_default_passwords),
            )
        ensure_default_settings(conn)
        conn.commit()

        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")

        after_counts = count_tables(conn)

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    artifact_actions = clear_generated_artifacts(ROOT)

    report_lines.append("Remoções operacionais:")
    for k in OPERATIONAL_TABLES:
        report_lines.append(f"- {k}: {removed_oper.get(k, 0)}")
    report_lines.append("")

    report_lines.append("Remoções de cadastros com padrão de teste:")
    for k in ["clients", "drivers", "vehicles", "route_cities"]:
        report_lines.append(f"- {k}: {removed_test_master.get(k, 0)}")
    if args.wipe_master_data:
        report_lines.append("")
        report_lines.append("Remoções de cadastros-base (wipe-master-data):")
        for k in MASTER_TABLES:
            report_lines.append(f"- {k}: {removed_master.get(k, 0)}")
    report_lines.append("")

    report_lines.append("Admin/permissões:")
    for k, v in admin_report.items():
        report_lines.append(f"- {k}: {v}")
    report_lines.append("")

    if args.force_default_passwords:
        report_lines.append("Primeiro acesso:")
        report_lines.append("- senha_padrao: usuario123 (username + 123)")
        report_lines.append("- troca_obrigatoria: sim")
        report_lines.append(f"- usuarios_alvo: {first_access_report.get('target_scope', 0)}")
        report_lines.append(f"- usuarios_atualizados: {first_access_report.get('changed_users', 0)}")
        report_lines.append("")

    report_lines.append("Artefatos de teste:")
    for k, v in artifact_actions.items():
        report_lines.append(f"- {k}: {v}")
    report_lines.append("")

    report_lines.append("Contagens depois:")
    for k in sorted(after_counts):
        report_lines.append(f"- {k}: {after_counts[k]}")
    report_lines.append("")
    report_lines.append("Reset finalizado com sucesso.")

    write_report(report_path, report_lines)
    print("\n".join(report_lines))
    print(f"\nRelatório salvo em: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
