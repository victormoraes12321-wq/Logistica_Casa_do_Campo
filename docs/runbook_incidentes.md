# Runbook - Incidentes e Recuperacao

## 1) Sistema fora do ar

1. Validar processo Python/servico ativo.
2. Validar porta 3000 em escuta.
3. Consultar `logs/runtime.err.log` e `logs/server_errors.log`.
4. Reiniciar servico (NSSM/Task Scheduler) ou `python run.py`.

## 2) Falha de banco

1. Conferir permissao de escrita em `data/`.
2. Rodar auditoria basica:
   - `python tools/db_integrity_audit.py`
3. Se necessario, restaurar ultimo backup valido.

## 3) Restauracao (SQLite)

1. Parar aplicacao.
2. Restaurar backup aprovado para `data/logistica_casa_do_campo.sqlite3`.
3. Subir aplicacao.
4. Validar:
   - login
   - dashboard
   - pedidos
   - cargas/rotas
   - backup

## 4) Restauracao (PostgreSQL)

1. Parar aplicacao.
2. Executar:
   - `python tools/postgres_backup_restore.py --mode restore --database-url postgresql://... --file <dump> --confirm-restore RESTAURAR`
3. Subir aplicacao.
4. Validar paridade/reconciliacao.

## 5) Incidente de seguranca/permissao

1. Inativar usuario comprometido em `Configuracoes > Usuarios`.
2. Forcar reset de senha.
3. Revisar trilha em auditoria tecnica.
4. Validar que acessos indevidos foram bloqueados.

## 6) Escalonamento imediato

Escalar quando houver:

- ausencia de backup valido
- falha recorrente de banco
- suspeita de acesso indevido
- risco de perda de dados operacionais
