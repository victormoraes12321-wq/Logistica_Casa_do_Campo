# Sincronizações ERP e Registro de Viagens

Revisão de código e consultas de leitura: 21 e 22/10/2026.

## Estrutura do sistema

O sistema operacional usa `orders`, `order_items`, `clients`, `routes`,
`route_orders`, `trip_expenses` e `order_history`. São esses registros locais
que as telas Pedidos, Faturamento, Cargas, Acerto e o aplicativo do motorista
compartilham. Os dados `erp_cache_*` são cópias auxiliares do ERP; um registro
no cache não significa que exista um pedido operacional importado.

As entradas `run.py`/Flask e `app.py` atendem os mesmos domínios. A camada
Flask encaminha as rotas legadas por `LegacyProxyRuntime`. O script Windows
`iniciar.bat` usa `tools/server_monitor.py`, que inicia `app.py`, acompanha
o healthcheck e inicia o ngrok. Deve existir apenas uma instância atendendo
a porta pública da aplicação.

## Fluxos encontrados no código

| Fluxo | Entrada | Origem | Destino e efeito |
| --- | --- | --- | --- |
| Cache completo | `erp_connector.sync_erp_cache` | Views configuradas de pedidos, clientes, vendedores e faturamento | Atualiza `erp_cache_*`; não cria pedidos em `orders`. |
| Busca de Novo Pedido | `/orders/erp-lookup` → `lookup_order` → `map_erp_to_logistica` | Cache; consulta pontual no ERP quando necessário | Retorna campos para o formulário. A criação operacional ocorre ao gravar o pedido. |
| Importação de carga | `/orders/sync-carga-erp` e worker → `sync_orders_from_carga_view` | View de carga, clientes, produtos e faturamento | Cria/atualiza `orders`, `clients` e `order_items`; publica atualização das telas. |
| Faturamento periódico | `sync_pending_invoiced_orders_to_logistica` | Consulta de NF e pedido no conector | Promove apenas pedidos locais ainda em `Venda` para `Faturado`; registra histórico. |
| Abertura de Faturamento | `App.faturamento` | `lookup_order` + mapeamento | Também verifica pedidos em `Venda` e pode promovê-los para `Faturado`. |
| Faturamento manual | POST `/orders/{id}/invoice` | Dados confirmados pelo operador | Valida e grava os dados da NF do pedido. |
| Autocomplete da viagem | GET `/api/trips/search-item` | Pedido local → mestre ERP pela conexão central; cache como alternativa de consulta | Preenche a linha sem nenhuma escrita e sem executar os jobs de sincronização. |
| Conferência da viagem | POST `/api/trips/verify` | Pedido local, cache com detalhes e consulta ERP configurada | Retorna divergências e a previsão de baixa. |
| Gravação da viagem | POST `/api/trips/save` | Dados conferidos e confirmados | Cria a carga/acerto, despesas e vínculos; altera o pedido correspondente para `Acertado` e registra histórico. |

`create_server` registra o leitor de configurações e a conexão local no
conector e inicia `_erp_invoice_sync_worker`. Portanto, o agendamento pertence
ao servidor; não depende de manter a tela de Pedidos aberta.

## Agendamento encontrado nesta instalação

Configurações de `settings` consultadas sem alteração:

- Integração e sincronização automática habilitadas, driver Oracle.
- Segunda a sábado, das 07:00 às 18:00.
- Cache completo a cada 120 minutos, janela de dados configurada em 30 dias.
- O worker verifica o horário a cada 10 segundos. Dentro da janela, tenta
  faturamento pendente e importação de carga a cada 60 segundos, além do ciclo
  completo. Esses tempos são intervalos de tentativa, não garantia de conclusão.
- View de pedidos configurada: `VW_VENDAS_LM`; coluna de pedido: `PEDIDO_`.
- View de faturamento configurada: `VW_NOTA_FISCAL`.
- A importação de carga usa a view de carga configurada ou o padrão
  `VW_PEDIDO_CARGA_CC`.

Os botões de sincronização manual e a abertura de Faturamento têm entradas
próprias. Não são todos controlados pela janela do worker.

## Preenchimento do Registro de Viagens

1. Digitar ou colar pedido/NF agenda uma consulta após 450 ms sem digitação.
   Sair do campo também consulta imediatamente, sem repetir uma busca concluída.
2. A requisição informa qual campo foi usado. Um número de pedido não é tratado
   silenciosamente como uma NF de outro pedido. Zeros à esquerda são aceitos.
3. Se o pedido existe em `orders`, seus dados locais têm prioridade. Os produtos
   vêm de `order_items`, inclusive quantidades fracionadas. Essa consulta não
   abre conexão Oracle. Pagamento ausente pode ser complementado pelo cache.
4. Sem pedido local, consulta o mestre ERP pela configuração/conexão central.
   Não chama os jobs nem muda o contrato de `lookup_order`. Cache continua como
   alternativa de consulta, mas não autoriza gravar um novo pedido incompleto.
5. Retorna NF, cliente, pagamento, peso e produtos. Datas comerciais, cidade,
   rota, endereço e vendedor seguem em campos próprios. A data de entrega
   realizada permanece a digitada pelo operador; não recebe o prazo do pedido.
6. Ao trocar o identificador, remove os valores produzidos pela consulta anterior.
   Respostas atrasadas são canceladas/ignoradas. Edições manuais são preservadas.
7. Dados parciais recebem aviso. Falha HTTP ou de rede do formulário recebe
   mensagem de erro. A espera no formulário
   é limitada a 12 segundos; isso não muda o timeout dos jobs do ERP.
8. A conferência aguarda as consultas terminarem. Pedido e NF de registros
   diferentes bloqueiam a gravação antes de criar carga ou baixar pedidos.
9. Correção de 22/10/2026: o atualizador global de listagens substituía a
   `.table-wrap` da viagem pelo HTML inicial a cada 30 segundos, quando o foco
   estava fora da tabela. Isso apagava linhas e removia os listeners do formulário.
   A rota `/registro-viagens` e formulários com `data-realtime-refresh="off"`
   agora impedem a atualização tanto antes da consulta quanto antes de aplicar
   uma resposta pendente. As listagens continuam atualizando normalmente.
10. Despesas: almoço e diárias usam quantidade × valor unitário; os subtotais,
    total da seção e total do cabeçalho são atualizados juntos. Valores monetários
    aceitam vírgula decimal e são formatados como reais ao sair do campo.
    O payload continua numérico; o servidor normaliza e valida os valores e grava
    totais arredondados em centavos. Quantidade ou valor unitário incompletos são
    sinalizados, evitando descartar silenciosamente uma despesa ao gravar.

## Separação de responsabilidades e estados

- Consultar/preencher não deve alterar `orders.status`, `order_items`,
  `routes`, `route_orders`, `trip_expenses` ou `order_history`.
- A importação de carga atualiza pedidos em `Venda`, `Faturado` e
  `Carga Montada`; mantém `Carga Montada` quando a carga já existe. Para outros
  estados, seu caminho principal não reabre o pedido, embora possa completar NF.
- A importação evita substituir itens que já tenham estado `Entregue` ou
  `Problema`. A sincronização de faturamento periódico revalida `status='Venda'`
  antes do UPDATE.
- A baixa pelo Registro de Viagens acontece na confirmação de gravação e deve
  referenciar o mesmo pedido encontrado na consulta/conferência.

## Pontos de atenção encontrados nos fluxos compartilhados

Estes comportamentos já existiam; a correção de autocomplete não redesenhou os
jobs compartilhados:

- A view de vendas retorna linhas de produtos. O cache faz `INSERT OR REPLACE`
  por número de pedido; várias linhas podem terminar representadas apenas pela
  última. Uma linha isolada não comprova peso/valor/produtos totais. A conferência
  da viagem só usa diretamente esse cache para totais se houver itens e peso.
- `lookup_order` zera `_itens` no resultado público, mesmo quando a consulta ao
  ERP trouxe itens. Nesta tela, os detalhes são recuperados do cache existente.
- O mapeamento compartilhado permite usar orçamento/número do pedido como
  documento em algumas situações de faturamento. NF fiscal e documento alternativo
  não podem ser assumidos equivalentes em uma futura unificação.
- A tela Faturamento possui atualização própria além do worker. Seu UPDATE não
  repete a condição `status='Venda'` no momento da escrita, ao contrário do worker.
  Há uma possibilidade de concorrência se outro fluxo encerrar o pedido entre
  a leitura e a escrita. Corrigir isso exige alteração e teste específicos.
- Comentários antigos do conector descrevem buscas somente em cache, mas o código
  atual também consulta o ERP ao vivo. Esta análise segue o comportamento do código.

## Validação

- Testes isolados do autocomplete, layout, baixa de viagens, conector ERP e
  mapeamento. Conexões externas são simuladas nesses testes.
- Teste em navegador com dados fictícios vindos do endpoint em banco em memória:
  preenchimento sem blur, pagamento/data/NF/produtos, troca de pedido, edição
  manual, resposta atrasada, novas linhas e ausência de POSTs de gravação.
- As leituras da base operacional para auditoria usam SQLite em `mode=ro`.
- A consulta foi exercitada nos 265 pedidos locais sem conexão ERP nem gravações:
  265 retornaram cliente, peso e produtos; 260 tinham NF e 179 tinham pagamento
  disponível. Campos ausentes não foram inventados.
- Nenhum job de importação ou faturamento real é executado para testar a consulta.

Esta revisão mapeia as dependências de sincronização e a integração da viagem.
Não constitui certificação de todos os módulos, de todas as condições de rede ou
de todos os dados históricos do ERP.


## Correção de datas e metadados — 22/10/2026

- Origem dos dados: `trip_order_metadata.lookup_metadata` lê `DON_PEDIDO_VENDA`,
  nota fiscal não cancelada, cadastro de cliente, vendedor e itens/produtos.
  Respeita o schema e a view de clientes configurados no conector central.
- `sale_date`: `DON_PEDIDO_VENDA.DATA`. `DTEMISSAO_` da view de vendas representa
  faturamento e não é usado como data de venda nesta tela.
- `invoiced_at`: `DATA_FATURAMENTO_PEDIDO`, com emissão da NF como alternativa.
- `expected_delivery_date`: regra vigente de 15 dias corridos desde a venda.
  Não é a data de entrega realizada. `delivered_at` continua sendo a entrega.
- Cidade, UF, endereço, vendedor e itens vêm do ERP; rota é localizada por cidade
  exata, sem acentos, em `route_cities`. Uma cidade desconhecida não vira Teófilo
  Otoni automaticamente. Uma NF desconhecida não recebe o número do pedido.
- A gravação consulta os dados comerciais antes das escritas e reutiliza o
  resultado na transação. Não confia em metadados ocultos do navegador.
  Falhas de consulta não podem criar meia carga. Avulsos sem venda confirmada
  mantêm datas comerciais desconhecidas, em vez de usar a data da viagem.
- Pedidos existentes só recebem complementos comerciais com identidade do
  cliente confirmada. Referências avulsas não são associadas a outro cliente
  apenas por coincidência do número, especialmente 0660526.
- Na sequência da carga, `route_orders.status` e `orders.status` agora têm nomes
  distintos na consulta. O prazo usa o estado do pedido, evitando marcar cargas
  acertadas como atrasadas apenas pela passagem do tempo.
- Cargas 147 e 148 auditadas. Corrigidos 700460, 50045091 e 50044894 com os dados
  oficiais; seus produtos genéricos foram substituídos pelos itens reais. 701280
  já estava correto. 0660554 e 0660526, seus clientes e itens foram preservados
  integralmente por instrução do usuário. Datas/valores de entrega, pesos,
  vínculos, veículos, motoristas e despesas permaneceram iguais.
- Backup e comparação antes/depois: `backups/correcao_viagens_20261022_091407/`.
  A correção pode ser inspecionada em
  `tools/correct_trip_commercial_metadata_20261022.py` (consulta sem `--apply`).

- A conferência após reiniciar identificou uma normalização antiga executada a
  cada inicialização, que mudava caixa de texto, nulos e datas de atualização de
  cadastros. Ela agora é uma migração única, marcada em settings por
  `migration_business_text_normalization_v1`. Nesta instalação já havia sido
  executada, por isso foi marcada concluída e as mudanças automáticas nas cargas
  auditadas foram restauradas conforme o relatório. A inicialização completa
  foi simulada em cópia em memória e os dados foram comparados novamente após
  o segundo reinício real: nenhuma diferença no snapshot auditado.
- Validação final: 62 testes isolados aprovados, teste de navegador aprovado,
  consultas ao ERP real com gravação somente em memória para quatro pedidos,
  integridade/FKs e renderização das cargas conferidas. Healthchecks local e
  pelo ngrok responderam normalmente após o reinício.


## Consulta por NF repetida no histórico — 22/10/2026

A NF 15928 aparece em oito pedidos de anos diferentes (2018 a 2026). A busca
tratava qualquer repetição como ambiguidade e convertia a exceção em erro 503.
A seleção agora prioriza a NF fiscal efetiva, depois a referência fiscal do
cabeçalho e, por último, documento alternativo de pedido faturado. Dentro das
correspondências fiscais, usa a emissão mais recente, com faturamento como
alternativa se a emissão não veio. Não usa a data de venda para desempatar NFs.
Se persistirem pedidos diferentes na mesma data mais recente, ou houver datas
insuficientes para distinguir, pede explicitamente o número do pedido via 409.
O formulário mostra essa mensagem e conserva os demais dados digitados.

A regra vale para todas as consultas de NF no Registro de Viagens, inclusive
conferência e gravação. NF 15928 confirmada como pedido 50045358, faturamento
06/10/2026. Consultas diretas adicionais: 15815 → 50045091; 310376 → 700460;
906794 → 50044894; 310695 → 701280. Todas foram somente leitura.
Validação: 68 testes isolados e teste em navegador com mensagem de ambiguidade,
novas linhas, campos manuais e despesas preservados.


## Histórico original por pedido e viagem — 22/10/2026

- `trip_audit.ensure_schema` cria `trip_registry_audits` e
  `trip_registry_audit_orders` na migração normal. Não preenche históricos antigos
  com valores presumidos.
- Toda gravação do Registro de Viagens preserva o formulário original, valores
  normalizados de despesas, carga, motorista, ajudante, veículo, cliente, itens,
  pedido antes/depois da baixa e a resposta do ERP consultada naquele momento.
  KM/horário e observações de cada linha ficam no registro original.
- Por pedido, há colunas próprias para peso digitado, peso salvo no cadastro,
  peso ERP, diferença digitado menos ERP, presença de diferença e alerta da
  conferência (5 kg). Falta de consulta ERP é nula e tem motivo explícito.
  Uma referência de outro cliente é registrada como candidata incompatível,
  sem usar seu peso na comparação.
- Nome abreviado é aceito para conferência quando o código ERP do cliente coincide
  e não é apenas o ID local atribuído automaticamente. Os dois avulsos antigos
  0660554/0660526 continuam preservados.
- A cópia original é gravada na mesma transação da carga. Se falhar, a carga e
  a baixa são revertidas. O conteúdo recebe SHA-256, e gatilhos impedem atualizar
  ou apagar o histórico. Ele não depende da existência futura de cadastros/rotas.
- A conferência agora prefere o peso atual do ERP também nos pedidos já locais.
  Não executa jobs de importação nem muda contratos das demais sincronizações.
- A ficha PDF da viagem lê a cópia original quando disponível e mostra a
  comparação de peso salva, equipe, dados digitados e despesas daquela gravação.
  Alterações posteriores no cadastro não reescrevem a ficha original.
- Auditoria de dados anteriores arquivada em
  `data/auditorias_peso/20261022_121714/`, com dados de consulta, PDF e hashes.
  Relatório final solicitado: somente outubro/2026, cargas 152,149,151,153,158
  em ordem de data, 19 entregas, 7 pedidos divergentes, 5 páginas. Carga 154
  não foi incluída porque seu desvio agregado local não comprova divergência ERP
  com a conferência parcial disponível.
- Backup anterior à nova migração:
  `backups/historico_viagens_20261022_121714/antes.sqlite3`.
- Validação: testes unitários do histórico original, reversão integral em falha,
  proteção contra edição, ERP indisponível, cliente incompatível, conferência e
  impressão. Migração completa e gravação com ERP real lido em cópia da base em
  memória; nenhum lançamento de teste criado na base operacional.
