# Hardening Tecnico - Base Interna

## Escopo consolidado (sem alteracao visual)

- Configuracao por ambiente centralizada em `app_core/config.py`
- Resolucao de banco e compatibilidade em `app_core/runtime_db.py`
- Runtime Flask app factory com proxy legado em `app_core/app_factory.py`
- Entrada padrao unica em `run.py`
- Compatibilidade legada preservada (`APP_RUNTIME=legacy`, `app.py`)
- Modularizacao por dominio (`app_core/domains/*_dispatch.py`)
- Servicos e repositorios centrais (`app_core/services`, `app_core/repositories`)
- Alembic ativo com migracoes ate `0003_runtime_hardening_columns_indexes`

## Banco e consistencia

- colunas de controle adicionadas/padronizadas (`updated_at`, `version`, `capacity_kg`)
- indices incrementais para consultas e concorrencia
- validacoes de integridade sem perda de dados
- compatibilidade SQLite mantida e trilha PostgreSQL pronta

## Seguranca

- senha com hash e rehash gradual de legado
- SECRET_KEY obrigatoria em producao
- DEBUG controlado por ambiente
- cookie de sessao com flags de seguranca
- bloqueio de usuario inativo e sessao invalidada
- permissao validada no backend (nao apenas UI)

## Auditoria e operacao

- logs estruturados de erro (`logs/server_errors.jsonl`)
- trilha de auditoria funcional para acoes criticas
- backup/restore SQLite e PostgreSQL com scripts dedicados
- retencao e validacao automatica de backup

## Testes e gate

- gate unico: `tools/regression_release_gate.py`
- cobre auditoria final, caos, estado zerado, UX, stress e testes unitarios de runtime

## Riscos remanescentes

1. Pasta atual sem `.git`: tag Git nao aplicavel no estado atual.
   - mitigado com baseline RC por artefatos (`releases/`).
2. Paridade PostgreSQL depende de ambiente PostgreSQL disponivel para execucao final.
3. Refatoracao total de `app.py` ainda pode evoluir por lotes, sem quebrar operacao.
