# Politica de Rollback - Logistica Casa do Campo

## Objetivo
Garantir retorno rapido e seguro para o ultimo estado estavel em caso de falha apos release.

## Gatilhos de rollback

- aplicacao indisponivel por mais de 5 minutos
- erro critico de autenticacao/permissao
- falha de escrita em banco
- inconsistencias operacionais graves (status invalido, duplicidade de vinculacao)

## Pre-requisitos obrigatorios

1. Baseline congelado com `tools/freeze_baseline.py`
2. Backup valido antes da janela de release
3. Manifesto RC salvo em `releases/current_rc.json`
4. Responsavel tecnico designado para executar validacao pos-rollback

## Rollback SQLite (ambiente atual)

1. Parar servico/aplicacao.
2. Escolher backup valido mais recente (`backups/` ou `releases/<tag>/`).
3. Restaurar para `data/logistica_casa_do_campo.sqlite3`.
4. Subir aplicacao (`python run.py` ou servico).
5. Validar:
   - `GET /healthz`
   - login admin
   - dashboard, pedidos, rotas, backup

## Rollback PostgreSQL (trilha pronta)

1. Parar servico/aplicacao.
2. Restaurar dump com `pg_restore`.
3. Executar validacao de paridade/reconciliacao.
4. Subir aplicacao.
5. Rodar smoke funcional minimo.

## Metas de recuperacao

- RTO estimado (SQLite local): 10-20 minutos
- RPO estimado: ate o ultimo backup valido

## Pos-incidente

1. Registrar causa e horario em log tecnico.
2. Confirmar integridade operacional e permissoes.
3. Atualizar plano de acao preventiva.
