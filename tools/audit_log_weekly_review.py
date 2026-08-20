# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "logistica_casa_do_campo.sqlite3"
DEFAULT_OUT = ROOT / "logs" / "audit_reviews"

CRITICAL_KEYWORDS = (
    "apagou",
    "excluiu",
    "cancelou",
    "reabriu",
    "restaurou backup",
    "gerou backup",
    "permiss",
    "usuario",
    "usuário",
    "senha",
    "inativ",
    "desativ",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Revisão semanal dos logs de auditoria (ações críticas/permissões).")
    p.add_argument("--days", type=int, default=7, help="Janela de análise em dias (padrão: 7).")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT), help="Diretório de saída dos relatórios.")
    return p.parse_args()


def ensure_out_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def is_critical_action(action: str, module: str, notes: str) -> bool:
    text = f"{action or ''} {module or ''} {notes or ''}".lower()
    return any(k in text for k in CRITICAL_KEYWORDS)


def main() -> int:
    args = parse_args()
    days = max(1, int(args.days or 7))
    out_dir = Path(args.out_dir).resolve()
    ensure_out_dir(out_dir)

    if not DB_PATH.exists():
        raise SystemExit(f"Banco não encontrado: {DB_PATH}")

    cutoff = datetime.now() - timedelta(days=days)
    cutoff_iso = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB_PATH, timeout=20) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT id,created_at,user_id,user_name,source_ip,action,module,entity,old_value,new_value,notes
            FROM audit_logs
            WHERE created_at >= ?
            ORDER BY created_at DESC, id DESC
            """,
            (cutoff_iso,),
        ).fetchall()

    entries = [row_to_dict(r) for r in rows]
    critical_entries = [e for e in entries if is_critical_action(e.get("action", ""), e.get("module", ""), e.get("notes", ""))]

    by_action = Counter((e.get("action") or "Sem ação").strip() for e in entries)
    by_module = Counter((e.get("module") or "Sem módulo").strip() for e in entries)
    by_user = Counter((e.get("user_name") or "Sem usuário").strip() for e in entries)
    by_action_critical = Counter((e.get("action") or "Sem ação").strip() for e in critical_entries)
    by_user_critical = Counter((e.get("user_name") or "Sem usuário").strip() for e in critical_entries)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = out_dir / f"weekly_audit_review_{stamp}.md"
    json_path = out_dir / f"weekly_audit_review_{stamp}.json"

    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window_days": days,
        "window_start": cutoff_iso,
        "total_events": len(entries),
        "critical_events": len(critical_entries),
        "top_actions": by_action.most_common(15),
        "top_modules": by_module.most_common(15),
        "top_users": by_user.most_common(15),
        "top_critical_actions": by_action_critical.most_common(15),
        "top_critical_users": by_user_critical.most_common(15),
        "critical_events_sample": critical_entries[:120],
    }

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    lines: list[str] = []
    lines.append("# Revisão Semanal de Auditoria")
    lines.append("")
    lines.append(f"- Gerado em: **{summary['generated_at']}**")
    lines.append(f"- Janela analisada: **últimos {days} dias** (desde {cutoff_iso})")
    lines.append(f"- Total de eventos: **{len(entries)}**")
    lines.append(f"- Eventos críticos (ações sensíveis/permissões): **{len(critical_entries)}**")
    lines.append("")
    lines.append("## Top ações")
    for action, qtd in by_action.most_common(15):
        lines.append(f"- {action}: {qtd}")
    lines.append("")
    lines.append("## Top módulos")
    for module, qtd in by_module.most_common(15):
        lines.append(f"- {module}: {qtd}")
    lines.append("")
    lines.append("## Top usuários")
    for user, qtd in by_user.most_common(15):
        lines.append(f"- {user}: {qtd}")
    lines.append("")
    lines.append("## Eventos críticos (amostra)")
    if not critical_entries:
        lines.append("- Nenhum evento crítico na janela analisada.")
    else:
        for e in critical_entries[:120]:
            lines.append(
                f"- [{e.get('created_at','')}] {e.get('user_name','Sem usuário')} | "
                f"{e.get('action','Sem ação')} | {e.get('module','Sem módulo')} | {e.get('entity','')} | "
                f"IP {e.get('source_ip') or 'N/D'}"
            )
    lines.append("")
    lines.append(f"Arquivo JSON técnico: `{json_path}`")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Relatório semanal gerado: {md_path}")
    print(f"Resumo técnico JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

