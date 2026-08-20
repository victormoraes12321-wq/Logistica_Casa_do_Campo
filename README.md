# Logistica Casa do Campo

Sistema interno para operacao de pedidos, faturamento, cargas/rotas, acerto, SLA, permissoes, auditoria e backup.

## 1) Estado atual da arquitetura

- Runtime padrao: `run.py` com `APP_RUNTIME=flask`.
- Camada Flask (app factory): `app_core/app_factory.py`.
- Compatibilidade legada: `app.py` (ThreadingHTTPServer) preservada.
- Rotas e templates: mantidos, sem mudanca de layout visual.
- Modularizacao incremental por dominio:
  - `app_core/domains/*_dispatch.py`
  - `app_core/repositories/*`
  - `app_core/services/*`
- Banco versionado com Alembic em `migrations/`.

## 2) Requisitos

- Python 3.10+
- Windows Server/local (principal) ou Linux
- Permissao de escrita em `data/`, `backups/`, `logs/`

Instalacao:

```bash
python -m pip install -r requirements.txt
```

## 3) Configuracao de ambiente

Copie `.env.example` para `.env` e ajuste:

```env
FLASK_ENV=production
DEBUG=false
SECRET_KEY=troque_esta_chave_forte
APP_RUNTIME=flask
APP_HOST=0.0.0.0
APP_PORT=3000
DATABASE_URL=sqlite:///data/logistica_casa_do_campo.sqlite3
LOGISTICA_LEGACY_PROXY_PORT=4000
LOGISTICA_SECURE_COOKIE=0
```

Observacoes:

- `SECRET_KEY` e obrigatoria em producao.
- `DEBUG` em producao so liga com `LOGISTICA_ALLOW_PROD_DEBUG=1`.
- `APP_*` tem prioridade sobre `LOGISTICA_*` (compatibilidade).

## 4) Execucao

### 4.1 Entrada padrao

```bash
python run.py
```

### 4.2 Waitress (producao local)

```bash
waitress-serve --host=0.0.0.0 --port=3000 run:app
```

### 4.3 Fallback legado

```bash
set APP_RUNTIME=legacy
python run.py
```

ou direto:

```bash
python app.py
```

## 5) Operacao Windows (local server)

### 5.1 Inicio manual

```bash
iniciar.bat
```

Menu:

- `1` Local
- `2` Network
- `3` Network + watchdog
- `4` Instalar tarefas do Windows
- `5` Ver status das tarefas
- `6` Remover tarefas

### 5.2 Servico NSSM

Script:

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_nssm_service.ps1
```

### 5.3 Monitoramento de uptime

Script:

```powershell
powershell -ExecutionPolicy Bypass -File tools\uptime_monitor.ps1
```

Log gerado em `logs/uptime_monitor.log`.

### 5.4 Acesso por nome (sem IP)

Para usar o nome `logisticacasadocampo`:

```powershell
powershell -ExecutionPolicy Bypass -File tools\configurar_alias_hosts.ps1 -Alias logisticacasadocampo -ServerIp 192.168.0.36
```

Depois acesse:

```text
http://logisticacasadocampo:3000
```

Importante:

- Em rede local sem DNS corporativo, o alias precisa existir no arquivo `hosts` de cada computador cliente.
- Se houver DNS interno no roteador/servidor, prefira criar um registro DNS `logisticacasadocampo -> IP do servidor`.

### 5.5 Higiene antes de empacotar

Para gerar pacote limpo sem artefatos transitórios do SQLite:

```powershell
powershell -ExecutionPolicy Bypass -File tools\pre_package_cleanup.ps1
```

Para limpar e já religar watchdog/monitor:

```powershell
powershell -ExecutionPolicy Bypass -File tools\pre_package_cleanup.ps1 -RestartAfter
```

## 6) Banco e migrations

Migrations atuais:

- `0001_baseline_schema`
- `0002_audit_log_user_ip`
- `0003_runtime_hardening_columns_indexes`

Aplicar:

```bash
python -m alembic upgrade head
```

Ver versao:

```bash
python -m alembic current
```

Bootstrap em banco ja existente:

```bash
python tools/alembic_bootstrap.py
```

## 7) Hardening de dados e concorrencia

Implementado:

- colunas criticas padronizadas (`updated_at`, `version`, `capacity_kg` etc)
- controle otimista de concorrencia em edicoes criticas
- validacoes de duplicidade e integridade
- incremento de `version` em atualizacoes sensiveis
- indices incrementais para consultas operacionais

## 8) Suporte PostgreSQL

- Runtime preparado por `DATABASE_URL`.
- Troca SQLite/PostgreSQL sem alterar regra de negocio.

Exemplo:

```env
DATABASE_URL=postgresql://usuario:senha@host:5432/logistica
```

### 8.1 Paridade SQLite x PostgreSQL

```bash
python tools/parity_sqlite_postgres.py --postgres-url postgresql://usuario:senha@host:5432/logistica --strict
```

### 8.2 Backup/restore PostgreSQL

```bash
python tools/postgres_backup_restore.py --mode backup --database-url postgresql://... 
python tools/postgres_backup_restore.py --mode restore --database-url postgresql://... --file backups\arquivo.dump --confirm-restore RESTAURAR
```

## 9) Backup e restauracao (operacao)

### 9.1 Via painel

Tela `Backup`:

- gerar backup manual
- restaurar com confirmacao forte

### 9.2 Automacao e retencao

```bash
python tools/backup_automation.py --mode backup --keep-max 7
python tools/backup_automation.py --mode verify
python tools/backup_automation.py --mode all
```

Regras atuais:

- limite de retenção: **7 backups mais recentes**
- ao gerar o 8º backup, o mais antigo é removido automaticamente

## 10) Release baseline, RC interno e rollback

### 10.1 Congelar baseline + backup pre-release

```bash
python tools/freeze_baseline.py --name rc_interno_YYYYMMDD --notes "descricao"
```

Saida:

- `releases/<tag>/manifest.json`
- `releases/tags/<tag>.tag`
- `releases/current_rc.json`
- `VERSION`

### 10.2 Politica de rollback

Ver documento:

- `docs/politica_rollback.md`

## 11) Seguranca e permissoes

Implementado:

- autenticacao com hash de senha
- sessao com expiracao e bloqueio de usuario inativo
- validacao CSRF e origem
- matriz de permissao em backend
- auditoria com user, IP, antes/depois e acao
- mensagens amigaveis para usuario (sem erro tecnico cru)

## 12) Testes e gate de release

### 12.1 Suite automatizada

```bash
python tools/regression_release_gate.py
```

Inclui:

- `final_audit_check2.py`
- `extreme_chaos_audit.py`
- `zero_state_check.py`
- `ux_selection_audit.py`
- `e2e_new_order_playwright.cjs` (Playwright: fluxo completo de Novo Pedido + busca de cliente)
- `stress_smoke.py`
- `tests.test_core_runtime`

### 12.2 Auditoria de integridade de banco

```bash
python tools/db_integrity_audit.py
```

## 13) Login inicial e primeiro acesso

- Usuario admin padrao: `admin`
- Senha inicial prevista: `admin123`

Usuarios novos:

- senha inicial: `nomeusuario123`
- troca obrigatoria no primeiro login

Forcar reset seguro de senhas padrao:

```bash
python tools/reset_producao.py --force-default-passwords
```

## 14) Documentacao operacional

- Operacao diaria: `docs/runbook_operacao_windows.md`
- Incidentes e recuperacao: `docs/runbook_incidentes.md`
- Politica de rollback: `docs/politica_rollback.md`
- Notas de hardening: `docs/technical_hardening_notes.md`
- Matriz final de permissões: `docs/matriz_permissoes_final.md`
- Manual de usuario (PDF): `Manual_de_Uso_Logistica_Casa_do_Campo.pdf`

## 15) Checklist pre-go-live

1. `python -m pip install -r requirements.txt`
2. `python -m alembic upgrade head`
3. `powershell -ExecutionPolicy Bypass -File tools\run_checklists.ps1 -Mode go-live -FullGate`
4. `python tools/reset_producao.py --force-default-passwords`
5. Configurar inicio automatico (NSSM ou Task Scheduler)
6. Validar `http://<ip-servidor>:3000/healthz`
7. Confirmar backup e restore em ambiente de teste
8. Rodar checklist pós-reboot: `powershell -ExecutionPolicy Bypass -File tools\run_checklists.ps1 -Mode post-reboot`

## 16) Riscos conhecidos

- Sem repositorio Git inicializado na pasta atual, tag Git nativa nao foi criada.
  - Mitigacao aplicada: RC interno por artefatos em `releases/`.
- Validacao de paridade com PostgreSQL depende de instancia PostgreSQL disponivel.
  - Scripts e trilha tecnica ja estao prontos; executar no ambiente alvo.
