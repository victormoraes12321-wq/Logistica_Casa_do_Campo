# -*- coding: utf-8 -*-
"""
app_core/services/alert_service.py
==================================
Serviço Centralizado de Notificações, Registro de Incidentes e Alertas Críticos.
Captura falhas de background (sync ERP, conexões de banco) e envia notificações.
"""
from __future__ import annotations

import os
import sys
import json
import time
import traceback
import urllib.request
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("logistica.alert")


class AlertService:
    def __init__(self, log_dir: str | None = None):
        self.log_dir = log_dir or os.path.join(os.getcwd(), "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_file = os.path.join(self.log_dir, "server_errors.jsonl")

    def record_error(
        self,
        category: str,
        error: Exception | str,
        context: dict[str, Any] | None = None,
        notify_webhook: bool = True
    ) -> dict[str, Any]:
        """
        Registra uma exceção ou falha de infraestrutura em arquivo JSONL estruturado
        e opcionalmente despacha notificação via Webhook caso ALERT_WEBHOOK_URL esteja configurado.
        """
        now_iso = datetime.now().isoformat()
        err_msg = str(error)
        stack_trace = traceback.format_exc() if isinstance(error, Exception) else ""

        record = {
            "timestamp": now_iso,
            "category": category,
            "error_message": err_msg,
            "stack_trace": stack_trace,
            "context": context or {},
            "python_version": sys.version,
            "pid": os.getpid()
        }

        # 1. Escreve em logs/server_errors.jsonl
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.error("Falha ao gravar registro em server_errors.jsonl: %s", exc)

        # 2. Despacha Webhook se ativado e configurado
        webhook_url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
        if notify_webhook and webhook_url:
            self._send_webhook(webhook_url, record)

        return record

    def _send_webhook(self, webhook_url: str, record: dict[str, Any]) -> None:
        """Envia alerta formatado para Webhook configurado (Slack, Teams, Telegram, Discord, etc)."""
        try:
            payload = json.dumps({
                "text": f"🚨 *[LOGÍSTICA ALERT]* Categoria: `{record['category']}`\n"
                        f"*Erro*: {record['error_message']}\n"
                        f"*Horário*: {record['timestamp']}\n"
                        f"*Contexto*: ```{json.dumps(record['context'], ensure_ascii=False)}```"
            }).encode("utf-8")

            req = urllib.request.Request(
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "Logistica-AlertService/2.6"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
        except Exception as exc:
            logger.warning("Falha ao despachar webhook para %s: %s", webhook_url, exc)


# Instância global do serviço de alertas
ALERT_SERVICE = AlertService()
