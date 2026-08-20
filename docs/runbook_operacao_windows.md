# Runbook - Operacao Windows (Servidor Local)

## 1) Subida inicial

1. Instalar Python 3.10+
2. Instalar dependencias:
   - `python -m pip install -r requirements.txt`
3. Aplicar migrations:
   - `python -m alembic upgrade head`
4. Rodar gate tecnico:
   - `python tools/regression_release_gate.py`
5. Preparar senhas iniciais:
   - `python tools/reset_producao.py --force-default-passwords`

## 2) Execucao

- Padrao: `python run.py`
- Producão local: `waitress-serve --host=0.0.0.0 --port=3000 run:app`
- Healthcheck: `http://127.0.0.1:3000/healthz`

## 3) Inicio automatico

Opcoes:

1. `iniciar.bat` opcao `4` (Task Scheduler)
2. NSSM:
   - `powershell -ExecutionPolicy Bypass -File tools\\install_nssm_service.ps1`

## 4) Monitoramento

- Uptime monitor:
  - `powershell -ExecutionPolicy Bypass -File tools\\uptime_monitor.ps1`
- Logs:
  - `logs/watchdog.log`
  - `logs/runtime.log`
  - `logs/runtime.err.log`
  - `logs/server_errors.log`
  - `logs/server_errors.jsonl`

## 5) Backup diario

- Manual: tela `Backup`
- Automatico:
  - `python tools/backup_automation.py --mode backup --keep-max 7`
  - `python tools/backup_automation.py --mode verify`
- Retenção:
  - manter no máximo **7 backups**
  - ao gerar o 8º, o mais antigo é removido automaticamente

## 5.1 Revisão semanal de auditoria

- Gerar relatório de ações críticas/permissões:
  - `python tools/audit_log_weekly_review.py --days 7`
- Saída padrão:
  - `logs/audit_reviews/weekly_audit_review_*.md`

## 6) Checklist diario rapido

1. `/healthz` responde 200
2. Login admin funcionando
3. Ultimo backup existente e valido
4. Sem erro critico novo em logs
5. Operacao de pedidos e cargas sem fila travada

## 7) Checklist 1-clique

- Go-live completo:
  - `powershell -ExecutionPolicy Bypass -File tools\run_checklists.ps1 -Mode go-live -FullGate`
- Pós-reboot:
  - `powershell -ExecutionPolicy Bypass -File tools\run_checklists.ps1 -Mode post-reboot`
