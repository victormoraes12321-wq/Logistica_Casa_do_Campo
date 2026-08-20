# DOSSIÊ TÉCNICO E ARQUITETURAL COMPLETO - LOGÍSTICA CASA DO CAMPO
**Documento Mestre de Especificação Técnica, Funcional, Arquitetura e Engenharia de Software**

> **Objetivo deste documento**: Servir como referência completa e exaustiva sobre toda a arquitetura, regras de negócio, modelo de dados, mecanismos de segurança, engenharia de resiliência e qualidade do sistema **Logística Casa do Campo**. Este documento foi estruturado para ser utilizado diretamente ou processado por Inteligência Artificial na elaboração de estratégias de conteúdo, artigos técnicos, carrosséis de arquitetura e cases de engenharia no LinkedIn.

---

## SUMÁRIO EXECUTIVO

1. **Visão Geral do Sistema e Domínio de Negócio**
2. **Arquitetura de Software e Pilha Tecnológica**
3. **Engenharia de Banco de Dados, Esquema e Hardening**
4. **Segurança, Autenticação e Matriz de Governança (RBAC & Auditoria)**
5. **Módulos Funcionais e Ciclo de Vida da Operação**
6. **Engenharia de Resiliência, Monitoramento 24/7 e Operação**
7. **Garantia de Qualidade, Suíte de Testes e Chaos Engineering**
8. **DevOps, Governança de Releases e Política de Rollback**
9. **Guia Estratégico de Conteúdo para o LinkedIn**

---

## 1. VISÃO GERAL DO SISTEMA E DOMÍNIO DE NEGÓCIO

### 1.1 Propósito da Aplicação
O **Logística Casa do Campo** é um sistema de missão crítica projetado para a gestão integrada da cadeia logística interna, expedição, distribuição de mercadorias, montagem de cargas, controle de rotas, aplicativo/interface de campo para motoristas, acerto financeiro e governança de auditoria para atacado, distribuidoras e agronegócio.

### 1.2 Problemas Reais do Negócio Resolvidos pelo Sistema
- **Gargalos na Expedição e Faturamento**: Eliminação do descontrole na fila de pedidos faturados versus pedidos aguardando carregamento.
- **Excesso de Carga em Veículos**: Controle rigoroso da capacidade de carga em quilos (`capacity_kg`) dos veículos durante o agrupamento de entregas.
- **Risco de Fura de SLA de Entrega**: Algoritmo de cálculo de prazo de entrega com base estrita em dias úteis, descontando fins de semana e feriados cadastrados.
- **Perda de Comprovantes/Canhotos**: Registro digital de entregas com upload de comprovantes, identificação de recebedores e documentos.
- **Gestão de Exceções em Campo**: Tratamento padronizado de problemas de entrega (ex: cliente ausente, produto recusado, endereço incorreto) com fluxo de acerto.
- **Falta de Rastreabilidade e Erros de Concorrência**: Trilha de auditoria completa capturando alterações antes/depois (diff JSON) e bloqueio otimista de edições simultâneas.

### 1.3 Fluxo Operacional Ponta a Ponta
```mermaid
flowchart LR
    A[1. Entrada do Pedido\nStatus: Venda] --> B[2. Faturamento\nStatus: Faturado]
    B --> C[3. Montagem de Carga\ne Roteirização]
    C --> D[4. Expedição & Saída\nStatus: Saiu para Entrega]
    D --> E{5. Tentativa de Entrega}
    E -- Sucesso --> F[6. Entrega Confirmada]
    E -- Falha/Exceção --> G[7. Registro de Problema]
    F --> H[8. Acerto Financeiro\nStatus: Acertado]
    G --> H
```

---

## 2. ARQUITETURA DE SOFTWARE E PILHA TECNOLÓGICA

### 2.1 Pilha Tecnológica (Tech Stack)
- **Linguagem & Runtime Principal**: Python 3.10+ (com suporte homologado até Python 3.14).
- **Framework Web**: Flask 3.x com arquitetura modular baseada em Application Factory (`app_core/app_factory.py`).
- **Servidor WSGI de Produção**: Waitress 3.x (servidor multithreaded leve de alta performance para ambiente Windows Server e Linux).
- **Proxy de Compatibilidade Legada**: `LegacyProxyRuntime` (uma ponte WSGI personalizada que executa o servidor HTTP nativo em background e faz proxy transparente, garantindo transição sem quebra de código legado).
- **ORM & Mapeamento de Dados**: SQLAlchemy 2.0+ (padrão Declarative Mapping com typings modernos) + SQLite3 nativo / psycopg2 para PostgreSQL.
- **Versionamento DDL de Banco de Dados**: Alembic 1.13+.
- **Interface & Estilização**: Vanilla HTML5, CSS3 profissional responsivo (design system baseado em CSS variables, glassmorphism, microanimações, dark/light mode intuitivo) e Javascript Vanilla modular sem dependências pesadas de frontend.
- **Automação & Ferramental**: PowerShell 5.1/7.x, Batch Scripts Windows, Playwright (Testes E2E), ReportLab (Geração de PDFs).

### 2.2 Estrutura da Arquitetura em Camadas
A aplicação adota uma **Arquitetura Modular em Camadas (Layered & Domain-Driven Hybrid Architecture)**:

```text
Logistica_Casa_do_Campo/
├── app.py                      # Core executável standalone (ThreadingHTTPServer) & rotas legadas
├── run.py                      # Ponto de entrada WSGI (Flask + Waitress)
├── alembic.ini                 # Configuração de migrações de banco
├── iniciar.bat                 # Script mestre de inicialização do servidor com monitor 24/7
├── app_core/                   # Núcleo modular da aplicação
│   ├── app_factory.py          # Inicializador da aplicação Flask e Proxy WSGI
│   ├── config.py               # Carregador centralizado de configurações (.env e variáveis)
│   ├── runtime_db.py           # Gerenciador da conexão com o banco de dados (SQLite/PostgreSQL)
│   ├── sqlalchemy_models.py    # Mapeamento ORM SQLAlchemy de todas as entidades
│   ├── domains/                # Dispatchers de domínio isolados
│   │   ├── audit_dispatch.py
│   │   ├── backup_dispatch.py
│   │   ├── clients_dispatch.py
│   │   ├── dashboard_dispatch.py
│   │   ├── orders_dispatch.py
│   │   ├── permissions_dispatch.py
│   │   └── routes_dispatch.py
│   ├── repositories/           # Camada de Acesso a Dados (Data Access Layer)
│   └── services/               # Serviços de Regra de Negócio
├── data/                       # Armazenamento do banco SQLite (logistica_casa_do_campo.sqlite3)
├── backups/                    # Repositório de backups comprimidos com retenção automática
├── logs/                       # Logs de operação (runtime.log, server_errors.log, watchdog.log)
├── migrations/                 # Scripts de migração Alembic (versionamento DDL)
├── tools/                      # Ferramental de automação, auditoria, testes e monitoramento
│   ├── server_monitor.py       # Monitor 24/7 em Python com heartbeat em tempo real para CMD
│   ├── watchdog.ps1            # Supervisor de processo em PowerShell
│   ├── install_windows_tasks.ps1 # Instalador de tarefas agendadas do Windows
│   ├── backup_automation.py    # Robô de backup automatizado e retenção de 7 edições
│   ├── freeze_baseline.py      # Congelador de release e manifesto RC
│   ├── extreme_chaos_audit.py  # Teste de estresse e resiliência caótica
│   ├── db_integrity_audit.py   # Auditor de integridade referencial de banco
│   └── e2e_new_order_playwright.cjs # Teste E2E automatizado com Playwright
└── docs/                       # Documentação técnica, manuais, runbooks e políticas
```

### 2.3 Suporte Dual Engine: SQLite3 & PostgreSQL
O sistema foi construído para funcionar transparentemente com **dois motores de banco de dados**:
1. **SQLite3 (Modo Padrão)**:
   - Configurado com **Write-Ahead Logging (WAL)** para suporte a leitura e escrita concorrentes de alto desempenho.
   - Habilitação estrita de restrições de chave estrangeira (`PRAGMA foreign_keys = ON;`).
   - Timeout de bloqueio dinâmico (`PRAGMA busy_timeout = 15000;`) evitando erros de banco travado (`database is locked`).
2. **PostgreSQL (Modo Produção Corporativa)**:
   - Suporte nativo ativado apenas alterando a variável `DATABASE_URL` no arquivo `.env` (ex: `DATABASE_URL=postgresql://user:pass@localhost:5432/logistica`).
   - **Script de Auditoria de Paridade (`tools/parity_sqlite_postgres.py`)**: Valida se todas as tabelas, colunas, tipos de dados, restrições e índices são 100% idênticos nos dois motores.

---

## 3. ENGENHARIA DE BANCO DE DADOS, ESQUEMA E HARDENING

### 3.1 DDL e Estrutura Exaustiva de Tabelas

O modelo relacional do sistema é composto por 17 entidades totalmente indexadas e normalizadas:

#### 1. `orders` (Tabela Central de Pedidos)
- `id` (INTEGER, PK, Autoincrement)
- `order_number` (TEXT, UNIQUE, NOT NULL): Número único do pedido (ex: `PED-2026-0001`).
- `external_id` (TEXT): Código do pedido no ERP externo ou sistema comercial legado.
- `client_id` (INTEGER, FK -> `clients.id` ON DELETE SET NULL)
- `seller_id` (INTEGER, FK -> `users.id` ON DELETE SET NULL)
- `seller_name` (TEXT): Nome do vendedor responsável.
- `status` (TEXT, NOT NULL): Estado atual (`Venda`, `Faturado`, `Saiu para entrega`, `Acertado`, `Problema`, `Cancelado`).
- `urgency` (TEXT): Prioridade (`Normal`, `Urgente`, `Crítico`).
- `sale_date` (TEXT): Data da venda no formato `YYYY-MM-DD`.
- `expected_delivery_date` (TEXT): Data limite de entrega calculada por SLA.
- `invoice_limit_date` (TEXT): Data limite para faturamento.
- `payment_method` (TEXT): Forma de pagamento negociada.
- `total_value` (REAL): Valor total do pedido em reais.
- `weight_kg` (REAL): Peso bruto total do pedido em kg.
- `delivery_address` (TEXT): Endereço completo de entrega.
- `location_link` (TEXT): Link do Google Maps / coordenadas GPS.
- `route_name` (TEXT): Nome da rota logística associada.
- `city` (TEXT) / `uf` (VARCHAR 8): Cidade e Estado do destino.
- `invoice_number` (TEXT) / `invoice_file_path` (TEXT) / `invoiced_at` (TEXT): Dados da Nota Fiscal emitida.
- `driver_id` (INTEGER, FK -> `drivers.id`) / `vehicle_id` (INTEGER, FK -> `vehicles.id`)
- `delivered_to` (TEXT) / `delivered_document` (TEXT) / `delivered_at` (TEXT): Dados da recepção.
- `created_at` (TEXT, NOT NULL) / `updated_at` (TEXT, NOT NULL)
- `version` (INTEGER, Default 1): Contador para **Controle Otimista de Concorrência**.

#### 2. `order_items` (Itens do Pedido)
- `id` (INTEGER, PK)
- `order_id` (INTEGER, FK -> `orders.id` ON DELETE CASCADE, NOT NULL)
- `product_code` (TEXT) / `product_name` (TEXT, NOT NULL) / `category` (TEXT)
- `quantity` (REAL) / `unit` (TEXT) / `weight_kg` (REAL)

#### 3. `order_history` (Histórico de Mudança de Status)
- `id` (INTEGER, PK)
- `order_id` (INTEGER, FK -> `orders.id` ON DELETE CASCADE, NOT NULL)
- `user_id` (INTEGER, FK -> `users.id` ON DELETE SET NULL)
- `old_status` (TEXT) / `new_status` (TEXT) / `action` (TEXT, NOT NULL) / `notes` (TEXT)
- `created_at` (TEXT, NOT NULL)

#### 4. `clients` (Cadastro de Clientes)
- `id` (INTEGER, PK), `name` (TEXT, NOT NULL), `document` (CPF/CNPJ), `phone`, `whatsapp`, `city`, `neighborhood`, `farm_name` (Nome da Propriedade/Fazenda), `address`, `reference_point`, `route_name`, `active` (INTEGER), `created_at`, `updated_at`, `version`.

#### 5. `drivers` (Motoristas)
- `id` (INTEGER, PK), `name` (TEXT, NOT NULL), `phone`, `document` (CNH), `vehicle_default`, `active`, `updated_at`, `version`.

#### 6. `vehicles` (Frota de Veículos)
- `id` (INTEGER, PK), `name` (TEXT, NOT NULL), `plate` (Placa), `type` (Toco, Truck, Carreta, Van), `capacity` (Texto), `capacity_kg` (REAL - Peso limite em kg), `active`, `updated_at`, `version`.

#### 7. `routes` (Cargas e Viagens)
- `id` (INTEGER, PK), `name` (TEXT, NOT NULL), `date` (TEXT), `driver_id` (FK), `vehicle_id` (FK), `status` (`Planejada`, `Em rota`, `Acertada`, `Com problema`, `Cancelada`), `route_name`, `total_weight` (REAL), `capacity` (REAL), `created_at`, `updated_at`, `version`.

#### 8. `route_orders` (Pedidos Alocados na Rota com Sequenciamento)
- `id` (INTEGER, PK), `route_id` (FK -> `routes.id` ON DELETE CASCADE), `order_id` (FK -> `orders.id` ON DELETE CASCADE), `delivery_order` (INTEGER - Posição 1, 2, 3 na fila de entrega), `status` (`Pendente`, `Em rota`, `Entregue`, `Com problema`, `Cancelado`).

#### 9. `route_cities` (Mapeamento de Cidades por Rota)
- `id` (INTEGER, PK), `route_name`, `city`, `uf`, `delivery_order`, `active`, `version`.

#### 10. `delivery_problems` (Registro de Ocorrências e Exceções)
- `id` (INTEGER, PK), `order_id` (FK -> `orders.id` ON DELETE CASCADE), `problem_type` (Tipo de problema padronizado), `description` (Detalhamento do motorista), `created_at`.

#### 11. `audit_logs` (Trilha Global de Auditoria)
- `id` (INTEGER, PK), `created_at`, `user_id`, `user_name`, `source_ip`, `action`, `module`, `entity`, `old_value` (JSON do estado anterior), `new_value` (JSON do estado atual), `notes`.

#### 12. `users` (Usuários do Sistema)
- `id` (INTEGER, PK), `name`, `username` (UNIQUE), `password_hash`, `role`, `active`, `created_at`, `last_login_at`, `must_change_password`.

#### 13. `user_permissions` & 14. `role_permissions` (Matriz de Acesso RBAC)
- Chave composta (`user_id`/`role_name` + `perm`), `allowed` (0 ou 1), `updated_at`.

#### 15. `attachments` (Comprovantes e Arquivos Anexos)
- `id` (INTEGER, PK), `order_id` (FK), `file_path`, `file_type`, `description`, `created_at`.

#### 16. `settings` & 17. `holidays` (Configurações Gerais e Feriados de SLA)
- `settings`: Par chave/valor.
- `holidays`: Data única (`date`) e nome do feriado para suspensão de contagem de SLA.

### 3.2 Hardening de Concorrência Otimista (Optimistic Locking)
Para evitar que dois operadores (ex: dois faturadores ou dois gestores de rota) alterem o mesmo pedido ou cliente simultaneamente sobrescrevendo dados um do outro, o sistema implementa **Controle Otimista de Concorrência**:
- Todas as tabelas mutáveis possuem a coluna `version INTEGER DEFAULT 1`.
- Toda instrução de `UPDATE` valida a versão atual:
  ```sql
  UPDATE orders 
  SET status = :new_status, version = version + 1, updated_at = :now
  WHERE id = :id AND version = :expected_version;
  ```
- Se o número de linhas afetadas for `0`, o sistema detecta que o registro foi modificado por outro usuário em paralelo, aborta a transação e exibe um aviso amigável ao operador solicitando a recarga da tela.

---

## 4. SEGURANÇA, AUTENTICAÇÃO E MATRIZ DE GOVERNANÇA (RBAC & AUDITORIA)

### 4.1 Autenticação e Proteção de Senhas
- **Algoritmo de Hashing**: PBKDF2 com HMAC-SHA256 (260.000 iterações nativas com salt aleatório por usuário via módulo `hashlib`).
- **Política de Senhas Iniciais**: Usuários criados recentemente recebem a flag `must_change_password = 1`. No primeiro acesso, o sistema força o redirecionamento para a tela de alteração de senha antes de liberar qualquer funcionalidade.
- **Proteção contra Força Bruta**: Bloqueio temporário de IP/Usuário após 6 tentativas incorretas de login em uma janela de 10 minutos.

### 4.2 Gestão Rígida de Sessões
- Cookies protegidos com flags `HttpOnly`, `SameSite=Lax` e suporte a `Secure` em HTTPS.
- Expiração absoluta de sessão configurável (`LOGISTICA_SESSION_MAX_AGE`, padrão 8 horas).
- Expiração por inatividade com limpeza automatizada em background a cada 120 segundos.

### 4.3 Matriz de Controle de Acesso Baseado em Perfis (RBAC - 8 Roles e 23 Permissões)

O sistema possui 8 perfis funcionais pré-configurados e 23 permissões granulares gerenciáveis diretamente pelo painel administrativo:

#### Perfis Padrão:
1. `GOD`: Acesso total e irrestrito a todas as rotas e funções de sistema.
2. `Admin`: Administração geral de usuários, configurações, backups e parâmetros.
3. `Gestor`: Gestão completa de pedidos, rotas, faturamento e relatórios.
4. `Faturamento`: Acesso focado na transição de pedidos de *Venda* para *Faturado* e emissão de DANFE.
5. `Expedicao`: Controle de carregamento, alocação de veículos e saída de rotas.
6. `Motorista`: Interface simplificada mobile para visualização de entregas e registro de baixas/problemas.
7. `Operador`: Inclusão e edição de pedidos de venda e clientes.
8. `Consulta`: Acesso somente leitura a painéis e relatórios.

#### As 23 Permissões Granulares:
- `view_dashboard`, `view_orders`, `create_orders`, `edit_orders`, `cancel_orders`, `invoice_orders`, `manage_routes`, `execute_routes`, `settle_routes`, `view_clients`, `edit_clients`, `view_drivers`, `edit_drivers`, `view_vehicles`, `edit_vehicles`, `view_reports`, `export_reports`, `view_audit`, `manage_users`, `manage_permissions`, `manage_backups`, `manage_settings`, `system_admin`.

### 4.4 Trilha Completa de Auditoria (Audit Log)
Todas as operações de escrita (`CREATE`, `UPDATE`, `DELETE`, `CANCEL`, `INVOICE`, `SETTLE`) disparam automaticamente um registro imutável na tabela `audit_logs` contendo:
- Data e Hora exata da operação.
- ID e Nome do Usuário responsável.
- Endereço IP de origem (`source_ip`).
- Módulo e Entidade afetada.
- **Diff JSON de Alteração**:
  - `old_value`: Estado original dos dados em formato JSON.
  - `new_value`: Estado modificado dos dados em formato JSON.
- Observações adicionais do operador.

---

## 5. MÓDULOS FUNCIONAIS E CICLO DE VIDA DA OPERAÇÃO

### 5.1 Módulo 1: Gestão de Pedidos e Cálculo Inteligente de SLA
- **Cadastro e Importação**: Permite inclusão manual com busca autocomplete de clientes ou importação massiva via arquivo Excel (`.xlsx`/`.csv`).
- **Algoritmo de Cálculo de SLA de Entrega**:
  - Calcula a data prevista de entrega (`expected_delivery_date`) ignorando sábados, domingos e feriados cadastrados na tabela `holidays`.
  - Exibe tags visuais dinâmicas no painel:
    - 🟢 **No Prazo**: Entrega dentro da janela calculada.
    - 🟡 **Alerta amarelo (SLA Próximo)**: Faltam 24h ou menos para o vencimento.
    - 🔴 **Crítico / Vencido**: Prazo estourado (exibe dias de atraso).

### 5.2 Módulo 2: Faturamento e Expedição
- **Fila de Faturamento**: Filtra pedidos aprovados na fase de *Venda*.
- **Conferência e Lançamento de Nota Fiscal**: Registro do número da DANFE, chave de acesso de 44 dígitos e upload do PDF da Nota Fiscal.
- **Transição Atômica**: Ao faturar, o pedido avança para o status `Faturado`, registrando a data/hora e o usuário faturador no histórico.

### 5.3 Módulo 3: Montagem de Cargas e Roteirização Inteligente
- **Agrupamento por Rota e Cidade**: O sistema agrupa pedidos faturados por nome de rota e cidade de destino.
- **Cálculo de Capacidade de Carga**:
  - Soma o peso total dos pedidos selecionados (`total_weight`).
  - Compara em tempo real com o limite do veículo (`capacity_kg`).
  - Impede ou alerta o operador em caso de sobrecarga do caminhão.
- **Sequenciamento de Entregas**: Permite reordenar a sequência de parada do motorista (`delivery_order`: 1ª entrega, 2ª entrega, etc.).

### 5.4 Módulo 4: Aplicativo e Interface do Motorista
- **Visão Mobile Simplificada**: Projetada para uso em smartphones e tablets no caminhão.
- **Funcionalidades de Campo**:
  - Visualização dos pedidos da rota na ordem exata de parada.
  - Link direto para navegação no Google Maps / Waze com o endereço do cliente.
  - **Confirmação de Entrega**: Registro do nome do recebedor, RG/CPF, data/hora e foto do canhoto assinado.
  - **Registro de Problema**: Seleção padronizada de motivos (`Cliente ausente`, `Endereço incorreto`, `Produto recusado`, `Estrada sem acesso`) com descrição do ocorrido.

### 5.5 Módulo 5: Acerto Financeiro e Fechamento de Viagem
- **Painel de Acerto**: Conferência final entre a expedição e o motorista no retorno do veículo.
- **Validação de Formas de Pagamento**: Verificação dos valores recebidos (Dinheiro, Pix, Cheque, Boleto assinado).
- **Conclusão da Rota**: Quando todos os pedidos da viagem são confirmados ou justificados, a rota passa para o status `Acertada` e os pedidos finalizam no status `Acertado`.

---

## 6. ENGENHARIA DE RESILIÊNCIA, MONITORAMENTO 24/7 E OPERAÇÃO

### 6.1 Monitor de Servidor 24/7 no CMD (`tools/server_monitor.py` & `iniciar.bat`)
Para garantir alta disponibilidade em servidores Windows sem necessidade de interfaces complexas de terceiros, o sistema conta com um **Monitor / Watchdog nativo em Python**:
- **Execução sem Fechamento de Janela**: O arquivo `iniciar.bat` chama o `server_monitor.py`, mantendo o terminal CMD aberto no servidor.
- **Painel de Status em Tempo Real**:
  ```text
  ===========================================================================
          LOGÍSTICA CASA DO CAMPO - SERVIDOR EM EXECUÇÃO 24/7
  ===========================================================================
   Executável Python:  C:\Users\Administrator\... \python.exe
   Modo de Rede:       Bind 0.0.0.0:3000
   Acesso neste PC:    http://localhost:3000
   Acesso na Rede:     http://192.168.0.35:3000
   Arquivo de Log:     logs/runtime.log
   Log de Erros:       logs/server_errors.log
  ---------------------------------------------------------------------------
   [07:51:06] [STATUS OK] Servidor Ativo | HTTP 200 OK | Uptime: 01h 15m 30s
  ```
- **Checagem de Saúde Ativa (Heartbeat HTTP)**: A cada 15 segundos, o monitor faz um teste interno no endpoint `/healthz`.
- **Auto-Recuperação em Caso de Crash**: Se a aplicação `app.py` for interrompida por uma exceção não tratada ou queda de energia/processo, o monitor captura o traceback, registra o erro em `logs/server_errors.log` e **reinicia o servidor automaticamente em 5 segundos**.
- **Tratamento de Encodamento UTF-8**: Função `safe_print` customizada para evitar que caracteres acentuados estourem erros de encoding no CMD do Windows.

### 6.2 Automação com Tarefas Agendadas do Windows (`tools/install_windows_tasks.ps1`)
Permite registrar o sistema no Agendador de Tarefas do Windows com elevação de privilégios (`NT AUTHORITY\SYSTEM`):
1. `LogisticaCasaDoCampo-Watchdog`: Inicializa o servidor automaticamente no boot do Windows (sem necessidade de login de usuário).
2. `LogisticaCasaDoCampo-BackupDaily`: Executa o robô de backup diário às 02:00.
3. `LogisticaCasaDoCampo-BackupVerifyWeekly`: Executa o teste de verificação e simulação de restore aos domingos às 03:00.

### 6.3 Estratégia e Retenção Automática de Backups (`tools/backup_automation.py`)
- **Política de Retenção Rolante (7 Edições)**: O sistema mantém exatamente os **7 backups mais recentes**. Ao gerar o 8º backup, o arquivo mais antigo é identificado e expurgado automaticamente.
- **Simulação de Desastre (`tools/simular_restauracao_desastre.py`)**: Script automatizado que cria um banco temporário em memória/sandbox, restaura o último arquivo `.sqlite3` comprimido e executa consultas de integridade para provar que o backup é 100% funcional.

---

## 7. GARANTIA DE QUALIDADE, SUÍTE DE TESTES E CHAOS ENGINEERING

### 7.1 Release Gate Automatizado (`tools/regression_release_gate.py`)
Nenhuma versão do sistema é liberada para produção sem passar pelo **Release Gate**, uma suíte integrada de testes que executa sequencialmente 7 etapas de validação:

```mermaid
graph TD
    A[Início do Release Gate] --> B[1. final_audit_check2.py\nAuditoria Estática/Rotas]
    B --> C[2. extreme_chaos_audit.py\nChaos & Stress Engineering]
    C --> D[3. zero_state_check.py\nValidação em Banco Virgem]
    D --> E[4. ux_selection_audit.py\nAuditoria de UI/Seletores]
    E --> F[5. Playwright E2E\ne2e_new_order_playwright.cjs]
    F --> G[6. stress_smoke.py\nTeste de Carga & Concorrência]
    G --> H[7. db_integrity_audit.py\nIntegridade Referencial]
    H --> I[APROVADO PARA PRODUÇÃO / RELEASE]
```

### 7.2 Detalhamento das Ferramentas de Teste
- **`extreme_chaos_audit.py` (Chaos Engineering)**: Injeta dados com payloads extremos (caracteres Unicode nulos, strings de 10.000 caracteres, tentativas de SQL Injection/XSS simuladas, requisições concorrentes desordenadas) para garantir que o sistema responda com mensagens amigáveis sem derrubar o servidor HTTP.
- **`zero_state_check.py`**: Valida a inicialização da aplicação a partir de um banco de dados totalmente limpo (zero registros), garantindo que seeds iniciais e migrações criem todas as tabelas e permissões sem dependência de dados legados.
- **`e2e_new_order_playwright.cjs` (Teste End-to-End)**: Executa um navegador Chromium headless via Playwright que simula a navegação real de um usuário: faz login, navega até a tela de pedidos, preenche o formulário de novo pedido, realiza busca de cliente por autocomplete, salva o pedido e verifica sua presença no grid.

---

## 8. DEVOPS, GOVERNAÇA DE RELEASES E POLÍTICA DE ROLLBACK

### 8.1 Congelamento de Baseline e Versionamento (`tools/freeze_baseline.py`)
Para criar uma versão estável (Release Candidate):
- O script gera um manifesto `manifest.json` com a hash de todos os arquivos de código, versão do banco e timestamp.
- Grava os arquivos em `releases/rc_interno_YYYYMMDD/`.
- Atualiza o arquivo de versão raiz `VERSION`.

### 8.2 Política Tridimensional de Rollback (`docs/politica_rollback.md`)
Em caso de anomalia crítica após um deploy em produção, a equipe aplica o plano de reversão em 3 camadas:
1. **Rollback de Aplicação/Código**: Reversão da pasta de código para a versão da release anterior gravada em `releases/`.
2. **Rollback de Banco de Dados**:
   - Via Alembic: `python -m alembic downgrade -1`.
   - Via Snapshot: Restauração do backup pré-release gerado automaticamente pelo `freeze_baseline.py`.
3. **Rollback de Serviço e Validação**: Reinicialização do monitor 24/7 e checagem imediata via `http://localhost:3000/healthz`.

---

## 9. GUIA ESTRATÉGICO DE CONTEÚDO PARA O LINKEDIN

Seu objetivo no LinkedIn é demonstrar **Senioridade Técnica, Visão de Arquitetura, Preocupação com o Negócio e Resiliência de Software**. Abaixo estão 10 ideias de posts e carrosséis estruturados prontos para serem desenvolvidos:

### 💡 Ideia 1: "Como Construí um Sistema de Logística 24/7 sem Depender de Infraestruturas Complexas de Nuvem"
- **Foco**: Arquitetura pragmática, baixo custo operacional, alta resiliência no Windows Server local.
- **Pontos Chave**: Uso de Python 3.10+, Waitress WSGI, SQLite em modo WAL e Watchdog customizado com heartbeat HTTP.

### 💡 Ideia 2: "SQLite em Produção Corporativa? Como Garantir Concorrência sem Travar o Banco"
- **Foco**: Engenharia de Banco de Dados.
- **Pontos Chave**: Explicação prática sobre modo WAL (Write-Ahead Logging), ajuste de `busy_timeout=15000`, restrições de FK ativas e o padrão de **Controle Otimista de Concorrência** via coluna `version`.

### 💡 Ideia 3: "Dual-Engine Database: Alternando entre SQLite e PostgreSQL com 1 Linha de Configuração"
- **Foco**: Flexibilidade arquitetural e ORM com SQLAlchemy 2.0 + Alembic.
- **Pontos Chave**: Como abstrair o banco de dados para que a aplicação rode em SQLite em pequenas filiais e em PostgreSQL na matriz, incluindo o script de validação de paridade de schema.

### 💡 Ideia 4: "Engenharia de Resiliência: Criando um Watchdog em Python com Auto-Healing para Servidores"
- **Foco**: DevOps, Resiliência e Automação.
- **Pontos Chave**: Como o `server_monitor.py` faz a supervisão do processo principal, executa healthchecks a cada 15s via HTTP, trata erros de encoding UTF-8 no Windows CMD e realiza o auto-restart em 5s.

### 💡 Ideia 5: "Segurança e Governança: Matriz de Acesso RBAC com Diffs JSON de Auditoria"
- **Foco**: Segurança da Informação e Compliance.
- **Pontos Chave**: 8 perfis funcionais, 23 permissões granulares e trilha de auditoria completa capturando `user_id`, `source_ip` e diffs JSON do estado anterior versus novo estado de cada entidade.

### 💡 Ideia 6: "Do Pedido ao Acerto de Viagem: Como a Tecnologia Otimizou a Expedição de Cargas"
- **Foco**: Visão de Produto e Domínio de Negócio (Business Value).
- **Pontos Chave**: Algoritmo de SLA descartando feriados/finais de semana, controle de peso limite por veículo (`capacity_kg`) e aplicativo de campo para o motorista registrar fotos de canhotos ou ocorrências.

### 💡 Ideia 7: "Backup Não Testado Não É Backup: Como Automatizamos a Simulação de Desastre"
- **Foco**: Confiabilidade e SRE.
- **Pontos Chave**: Política de retenção de 7 backups rolantes e o script de simulação que semanalmente restaura o banco em um ambiente sandbox e valida consultas para garantir a integridade dos dados.

### 8 Ideia 8: "Release Gate & Chaos Engineering: Testando Sistemas com Injeção de Dados Caóticos"
- **Foco**: Qualidade de Software e Testes Automatizados.
- **Pontos Chave**: Apresentação da suíte de teste que combina auditoria estática, injeção de payloads de caos (`extreme_chaos_audit.py`), validação em banco zero e testes E2E com Playwright em headless browser.

### 💡 Ideia 9: "Transição Arquitetural Sem Quebrar Produção: O Padrão Legacy Proxy Runtime"
- **Foco**: Engenharia de Software e Refatoração Limpa.
- **Pontos Chave**: Como migrar um sistema legado baseado em servidor HTTP nativo para uma arquitetura Flask/WSGI sem reescrever todas as rotas de uma vez, utilizando um proxy transparente em memória.

### 💡 Ideia 10: "Arquitetura Pragmática vs Over-Engineering: Entregando Valor Real com Zero Bloatware"
- **Foco**: Carreira, Liderança Técnica e Filosofia de Desenvolvimento.
- **Pontos Chave**: Discussão sobre a escolha consciente de não colocar Kubernetes, Microserviços ou Frameworks de Frontend pesados para um problema que exigia leveza, velocidade de resposta, fácil manutenção e custo zero de infraestrutura.

---

*Documento gerado e validado para a suíte técnica do sistema **Logística Casa do Campo**.*
