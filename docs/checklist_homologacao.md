# Checklist de Homologação Operacional

## 1) Vendas e faturamento
- Criar pedido novo com cliente existente.
- Criar pedido novo com cliente novo.
- Verificar cálculo de prazo útil (15 dias).
- Faturar pedido com NF e data.
- Confirmar bloqueio para status inválido.

## 2) Cargas e rotas
- Criar carga com pedidos faturados.
- Validar bloqueio de carga acima da capacidade.
- Marcar saída da carga.
- Alterar sequência de entrega em carga ativa.
- Confirmar bloqueio de edição em carga finalizada.

## 3) Acerto de carga
- Abrir acerto de carga em rota.
- Concluir todos como entregue.
- Registrar problema em pelo menos um pedido.
- Validar obrigatoriedade de observação para problema.
- Confirmar bloqueio de conclusão sem checklist completo.

## 4) Reabertura controlada
- Reabrir pedido finalizado com motivo obrigatório.
- Reabrir carga finalizada com motivo obrigatório.
- Confirmar auditoria de reabertura no histórico.
- Confirmar bloqueio da reabertura sem permissão.

## 5) Pendências e qualidade de dados
- Abrir tela `/pendencias`.
- Verificar cards de sem NF, sem carga, sem acerto e SLA crítico.
- Verificar bloco de qualidade de dados e duplicidades.
- Corrigir um cadastro e confirmar redução da pendência.

## 6) Relatórios e exportação
- Filtrar relatório por período.
- Filtrar por rota e por status.
- Validar relatório por motorista com peso, valor e dias.
- Exportar CSV e confirmar colunas de método de pagamento e dias de entrega.

## 7) Segurança e resiliência
- Testar envio de payload inválido.
- Testar tentativa por URL direta em ação proibida.
- Testar dupla submissão em formulários críticos.
- Rodar `python tools/stress_smoke.py`.

## 8) Go-live
- Gerar backup antes da entrada em produção.
- Validar restauração de backup em ambiente limpo.
- Treinar equipe com fluxo assistido 1-2-3.
- Publicar contatos de suporte e procedimento de incidente.
