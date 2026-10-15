# Guardrail — Scanner de oportunidades com gestão de risco obrigatória

> Especificação inicial do projeto. Fonte de verdade.
> Versão 0.5 — 2026-10-04
> Mudanças desde a 0.4: Finnhub sai do projeto (notícia também vem da Alpaca); Fase 1 concluída;
> acrescentadas as seções 7.1 (descontinuidades) e 12.1 (regra de sessão).
>
> **Regra de idioma deste projeto:** este documento e qualquer outro documento de raciocínio
> ficam em português, porque são para leitura e decisão. Código, nomes de funções, tabelas,
> comentários, mensagens de commit, README.md e SECURITY.md ficam em inglês.

---

## 1. O problema

Um trader discricionário operando em tempo parcial, em cripto majors e ações americanas,
tem dois problemas distintos. Só um deles é problema de informação.

**Problema de informação (secundário).** Setups assimétricos bons aparecem poucas vezes por
mês, num universo grande demais para acompanhar na mão. Exemplos encontrados manualmente no
passado: flush de desalavancagem numa major de cripto depois de uma varredura de liquidez;
uma small/mid cap americana rompendo com volume anormal e narrativa viva.

**Problema de execução (primário).** As perdas históricas não vieram de falta de informação.
Vieram de: entrar sem stop definido antes, não sair no zero a zero quando o mercado ofereceu,
adicionar alavancagem, e segurar posição perdedora além do ponto de invalidação sem data de
revisão.

Um sistema que resolve só o primeiro problema piora o segundo, porque aumenta o número de
gatilhos de entrada apresentados ao operador.

**Portanto: este sistema é um motor de disciplina de risco que por acaso inclui um scanner,
e não um scanner que por acaso registra trades.** Todas as decisões abaixo derivam dessa
ordem de prioridade.

---

## 2. Objetivos

- Mostrar um número pequeno (teto rígido: 5/dia) de setups definidos por regra, em cripto
  majors e ações americanas.
- Tornar estruturalmente impossível registrar uma posição sem stop, tamanho, tese e data de
  revisão.
- Recusar qualquer posição que ultrapasse o limite de risco por trade ou de risco total,
  calculado na moeda da conta.
- Manter um histórico completo e consultável de todo sinal, todo trade executado e todo
  post-mortem, para que a qualidade de cada setup possa ser medida em vez de lembrada.

## 3. O que este projeto NÃO é

- Não é bot de execução automática. Fases 1 a 5 são somente leitura em relação às corretoras.
- Não é plataforma de gráficos. A leitura de gráfico continua no TradingView.
- Não é serviço de venda de sinais. Ver §12.
- Não é framework de pesquisa quantitativa. O backtest existe só para barrar setups ruins.
- Sem suporte a alavancagem no motor de risco.

---

## 4. Princípio central

Três camadas. A terceira é a que resolve o problema real.

```
Camada 1  Ingestão          -> dados de mercado, derivativos e notícia normalizados no Postgres
Camada 2  Motor de regras   -> funções de setup determinísticas e versionadas -> linhas em signal
Camada 3  Contrato de trade -> portão entre um signal e uma position; aplica os limites de risco
```

Um `signal` nunca vira `position` de forma implícita. O operador precisa abrir o sinal,
preencher um `trade_contract`, e o motor precisa aceitar. Contrato recusado é registrado como
recusa, com motivo — esse log é o principal mecanismo de feedback comportamental do sistema.

---

## 5. Fontes de dados

Todas gratuitas. Custo externo total nas Fases 1 a 5: zero, fora um VPS opcional de ~5 dólares
por mês, que nem é necessário porque roda no Ubuntu local.

| Domínio | Fonte | Endpoint / observação | Custo |
|---|---|---|---|
| Candles cripto | Binance REST público | `/api/v3/klines`, sem chave | Grátis |
| Candles cripto (conferência) | Bybit REST público | `/v5/market/kline` | Grátis |
| Funding e open interest | Hyperliquid Info endpoint | `POST /info`, tipos `metaAndAssetCtxs` e `fundingHistory`. Público, sem chave de API, sem assinatura de carteira. Traz todos os perpétuos numa chamada. | Grátis |
| Open interest histórico | Binance Vision (`data.binance.vision`) | Dumps diários em `data/futures/um/daily/metrics/<SYMBOL>/`, sem autenticação. 288 linhas por dia, uma a cada 5 minutos. Traz `sum_open_interest`, `sum_open_interest_value` e três razões de posicionamento. BTC desde 2020-09, ETH e SOL desde 2021-12. | Grátis |
| Open interest recente | Binance REST | `/futures/data/openInterestHist`. Só os últimos 30 dias; cobre o intervalo entre o último dump publicado e agora. | Grátis |
| Barras diárias de ações US | Alpaca Market Data v2 | `data.alpaca.markets/v2/stocks/bars` com `feed=sip`, o volume consolidado de todas as bolsas. Chave gratuita, sem cartão. Verificado: 5 anos de histórico, centenas de símbolos por chamada, universo inteiro varrido em menos de 2 minutos. | Grátis |
| Lista de ativos negociáveis | Alpaca `paper-api.alpaca.markets/v2/assets` | 13.509 papéis negociáveis, com bolsa, nome e situação de empréstimo. É a base do universo por regra da §9.1. | Grátis |
| Notícia de ações US | Alpaca `/v1beta1/news` | 3 anos de histórico, mesma chave das barras. Usada apenas como flag booleana, nunca como pontuação. Itens com mais de 3 símbolos são descartados: são panoramas de mercado, não notícia sobre a empresa. | Grátis |
| Eventos societários | Alpaca `/v1/corporate-actions` | Desdobramento, agrupamento, cisão e fusão. Lidos da fonte, nunca inferidos do gráfico. | Grátis |

### Excluídos de propósito

- **Heatmap de liquidação da Coinglass.** É o melhor dado do mercado, mas os endpoints
  `heatmap/model1` e `model2` só existem nos planos Professional e Enterprise. Cerca de 70%
  do sinal é reconstruível de graça cruzando a variação de open interest do Hyperliquid com o
  fluxo de liquidação da Binance. Só reavaliar se a Fase 2 mostrar que essa aproximação é o
  fator limitante.
- **Polygon.io free tier.** 5 requisições por minuto inviabiliza varredura de universo.
- **Dado SIP recente.** A conta gratuita responde 403 com a mensagem "subscription does not permit
  querying recent SIP data" ao pedir até o dia corrente; até ontem funciona. Sem impacto: o setup
  de ações usa fechamento diário, média de 20 dias e máxima de 20 dias, tudo calculado depois do
  pregão. A varredura roda de manhã sobre o fechamento do dia anterior.
- **Feed `iex` da Alpaca.** Descartado para medir liquidez. O IEX é uma bolsa só e registra entre
  2% e 6% do volume real: a AAPL aparece com 1,2 milhão de ações por dia contra 42 milhões no feed
  consolidado. Um piso de liquidez calculado sobre ele erraria por um fator de até cinquenta.
- **Stooq.** Descartado. Publica CSV de barras diárias sem chave, mas o site passou a exigir um
  desafio de prova de trabalho executado em navegador. Contornar isso seria burlar proteção
  antirrobô.
- **Finnhub.** Fora do projeto por completo. Foi cogitada para barras e depois só para notícia,
  mas a Alpaca entrega as duas coisas com a chave que já existe e com 3 anos de histórico de
  notícia, o que torna a condição de catalisador testável no backtest. Uma conta a menos.
- **Qualquer API de sentimento ou social.** Dado de atenção é a coisa mais fácil de sobreajustar
  que existe. Fica fora até haver uma linha de base medida para comparar.
- **Fluxo de liquidação.** Fora do projeto por impossibilidade, não por escolha. A Binance parou
  de publicar o dump histórico `liquidationSnapshot` e o diretório está vazio para todos os
  símbolos. O stream ao vivo `!forceOrder@arr` existe, mas exige um processo no ar 24 horas por
  dia: o ambiente é WSL sem systemd, que não mantém serviço rodando, e liquidação é o único dado
  deste projeto que não pode ser buscado depois. Reavaliar só se houver máquina sempre ligada.

---

## 6. Stack

Escolhida para rodar inteiramente dentro da pasta do projeto, sem tocar em nenhum
toolchain global do Ubuntu.

**Backend e motor — Python**
- Python 3.12+, gerenciado por `uv` (instala em `~/.local`, não mexe no Python do sistema)
- `httpx` — clientes REST assíncronos
- `pydantic` v2 — todo payload externo vira modelo tipado na fronteira do sistema
- `sqlalchemy` 2.0 + `alembic` — ORM e migrations
- `polars` — cálculo de candles e indicadores
- `apscheduler` — agendamento dentro do processo worker
- `fastapi` + `uvicorn` — API interna consumida pelo dashboard
- `pytest`, `respx`, `hypothesis` — testes, incluindo teste baseado em propriedade
  na calculadora de risco

**Armazenamento**
- PostgreSQL 16 local, sem extensões, sem Docker (o ambiente é WSL sem systemd).
- **Decisão: sem TimescaleDB por enquanto.** O volume deste projeto não justifica. Índice
  composto em `(instrument_id, timeframe, ts)` resolve com folga no Postgres puro, e a extensão
  adiciona uma imagem de terceiro, um caminho de upgrade próprio e um modo de falha a mais em
  troca de ganho não mensurável nessa escala.
- O maior volume vem de `derivative_stat`, com uma linha a cada 5 minutos por símbolo.
- Critério para revisitar: qualquer tabela passando de 50 milhões de linhas, ou uma query
  agregada de série temporal levando mais de 500 ms com índice adequado.

**Frontend — dashboard**
- Texto preto, peso forte, contraste alto. Sem cinza sobre fundo claro.
- O padrão é não ter gráfico.
- (Esta seção foi alterada pelo `PLANO_ANTECIPACAO.md` §5.1: FastAPI + Jinja2 + CSS próprio,
  sem Node.)

**Alertas**
- Telegram Bot API chamada direto via `httpx`. Sem SDK.

**Ferramentas**
- `ruff` (lint e formatação), `mypy --strict` cobrindo `src` e `tests`
- `pre-commit` rodando ruff, mypy e um scanner de segredos
- GitHub Actions: lint, type-check e testes a cada push

---

## 7. Modelo de dados

Esquema mínimo viável. Todo valor monetário guardado como `NUMERIC`, nunca float.

```
instrument        id, symbol, venue, asset_class(crypto|equity), is_active, created_at
candle            instrument_id, timeframe, ts, o, h, l, c, v   [pk (instrument_id, timeframe, ts)]
derivative_stat   instrument_id, ts, funding_rate, open_interest, open_interest_value,
                  mark_price,
                  toptrader_long_short_account_ratio,
                  toptrader_long_short_position_ratio,
                  taker_long_short_volume_ratio                 [pk (instrument_id, ts)]
catalyst          instrument_id, ts, source, headline, url, dedupe_key
corporate_action  instrument_id, kind, effective_on, payload
universe_membership  instrument_id, entered_on, exited_on
source_health     source, last_success_at, last_error_at, last_error, consecutive_failures

setup             id, owner_id, key, version, params_json, is_enabled, backtest_report_id
signal            id, setup_id, instrument_id, fired_at, trigger_price,
                  context_json, status(new|contracted|rejected|expired)

trade_contract    id, signal_id, entry, stop, target, size, thesis,
                  review_by, risk_amount, risk_pct, decision(accepted|rejected),
                  rejection_reason
position          id, contract_id, opened_at, closed_at, avg_entry, exit_price,
                  realized_pnl, max_adverse_excursion, stop_moved_count
post_mortem       position_id, thesis_held(bool), followed_plan(bool), note

account_state     ts, free_capital, open_risk
locked_capital    id, label, notional, avg_entry, review_by, note
```

Restrições de integridade já aplicadas em `candle`: `timeframe IN ('1h','4h','1d')`,
`high >= low`, `volume >= 0`, `open BETWEEN low AND high`, `close BETWEEN low AND high`,
`low > 0`. Índice `ix_candle_timeframe_ts` em `(timeframe, ts)` para varredura entre
instrumentos.

Três campos existem especificamente para pegar modos de falha conhecidos.
`stop_moved_count`: afastar o stop da entrada é o comportamento que produziu a maior perda
histórica. `review_by`: posição sem data de revisão é como um trade vira bag.
A tabela `locked_capital` existe pelo mesmo motivo — ver §9.

O campo `owner_id` em `setup` custa nada agora e evita uma reescrita caso um dia o sistema
saia do uso pessoal. É a única concessão de arquitetura multiusuário neste projeto.

### 7.1 Descontinuidades: janelas que o motor ignora

Uma série de preço pode quebrar sem que nada de errado tenha acontecido com os dados. Três
situações foram encontradas nos 1,57 milhão de barras carregadas, e todas produzem no gráfico
exatamente o formato que o setup procura — salto de preço com volume anormal.

1. **Evento societário.** Um agrupamento de 120 para 1 aparece como alta de 1.830% em um dia.
   Detectado com exatidão a partir dos eventos publicados pela corretora, na tabela
   `corporate_action`. Foram 403 eventos em 3 anos no universo.
2. **Reaproveitamento de código de negociação.** Um papel que para de negociar por mais de 180
   dias e volta quase sempre é outra empresa usando o mesmo código. A série emenda duas
   companhias e a emenda vira um salto. Dois casos encontrados, ambos com mais de 600 dias
   parados.
3. **Descontinuidade sem explicação conhecida.** Variação acima de 8 vezes entre dois
   fechamentos sem evento societário registrado. Três casos, dois deles sendo o item 2 visto
   por outro ângulo.

O motor de regras ignora qualquer sinal cuja janela de cálculo contenha uma dessas três. Não
apaga dado: marca a janela.

**O limiar de 8 é deliberadamente alto.** Uma versão anterior usava 3 e marcava como suspeita a
queda de 82% de uma empresa de biotecnologia cujo estudo clínico falhou — movimento real, com
volume real, e exatamente o tipo de evento que o setup existe para encontrar. Movimento de 200%
ou queda de 70% em um dia acontece de verdade em papel pequeno.

**Nenhuma dessas três é detectada por limiar de preço e volume**, e a tentativa de fazer isso
foi descartada depois de três rodadas de ajuste em que cada limiar funcionava nos casos
examinados e errava no seguinte. Evento societário e dormência são fatos registrados; quando o
trabalho vira "achar o número que separa A de B", falta dado, não calibragem.

---

## 8. Motor de risco — regras rígidas

São constantes no código, não configuração. Mudar exige um commit e uma revisão, não um
impulso às onze da noite.

| Regra | Valor | Motivo |
|---|---|---|
| Risco máximo por trade | 1,5% do capital livre | Com R$5.000 livres, R$75 de risco por posição |
| Risco aberto simultâneo máximo | 4% do capital livre | Limita drawdown correlacionado |
| Máximo de posições novas por mês | 6 | A estratégia declarada é oportunista, não diária |
| Alavancagem | Não suportada | Contrato com alavancagem maior que 1 é recusado |
| Stop obrigatório | Sempre | Contrato sem stop é recusado, sem exceção |
| Afastar o stop | Bloqueado | Mover o stop para longe da entrada é recusado; apertar é permitido |
| Data de revisão obrigatória | Sim, no máximo 30 dias | Posição vencida aparece em vermelho no topo do dashboard |
| Cooldown após 2 perdas seguidas | 7 dias sem contratos novos | Interrompe o ciclo de revenge trade |

O caminho da recusa é tão importante quanto o da aceitação. Toda recusa é persistida com o
motivo, e o dashboard mostra a contagem de recusas por motivo nos últimos 90 dias.

**Calibração honesta, escrita aqui de propósito:** com R$2.000 a R$8.000 de capital livre e
1,5% de risco por trade, um mês bom produz algumas centenas de reais. Este sistema é um
instrumento de disciplina. Ele não resolve instabilidade de renda, e este roadmap não finge
que resolve.

---

## 9. Capital travado

Existe capital parado numa posição alavancada antiga, fora do capital operacional. Os dois são
bolsos separados e o sistema os trata assim.

**Capital operacional (`account_state.free_capital`).** Cresce apenas pelo resultado realizado
das operações registradas no sistema. É a única base usada para calcular os limites da §8.

**Capital travado (`locked_capital`).** Linha somente leitura no topo do dashboard. Não entra
em nenhum cálculo de risco enquanto estiver travado. Se um dia for liberado, no todo ou em
parte, o valor liberado é lançado em `free_capital` por um evento explícito, e todos os limites
da §8 passam a ser calculados sobre a base nova automaticamente. Nenhum ajuste manual de
limite é necessário nem permitido.

O campo `review_by` nesta tabela não existe para forçar uma saída. Existe para que a posição
tenha alguma regra de saída, em qualquer direção. Uma revisão válida pode perfeitamente ser
"manter, reavaliar em 30 dias, motivo: X" — o que o sistema não aceita é a posição atravessar
meses sem nenhuma linha escrita sobre ela.

Qual decisão tomar na data é do operador. Este documento não recomenda comprar nem vender nada.

---

## 10. Setups

Cada setup é uma função pura, `(market_snapshot, params) -> Signal | None`, versionada, com
parâmetros no banco. Nenhum setup entra em produção sem passar pela §11.

**`crypto_deleverage_flush`** — o método que o operador já usa, com um filtro adicionado.

Dispara quando tudo abaixo é verdade numa major (BTC, ETH, SOL):
1. O drawdown desde a máxima de N dias cruza `X%` (padrão N=7, X=10)
2. O funding virou negativo ou está no decil inferior da faixa de 30 dias
3. O open interest caiu pelo menos `Y%` nas últimas 24h (padrão Y=8)

As condições 2 e 3 são a adição. Elas separam um flush que já limpou os comprados alavancados
de uma queda que ainda tem posicionamento preso acima. Uma ordem limitada fixa em −10% sem
esse filtro é exatamente o trade que é atropelado.

**`equity_volume_breakout`** — rompimento com volume anormal e catalisador.

Dispara quando tudo abaixo é verdade num papel do universo da §9.1:
1. Volume relativo acima de 3x a média de 20 dias
2. Fechamento acima da máxima de 20 dias
3. Existe uma linha em `catalyst` nas últimas 72 horas

O catalisador é um portão booleano, não uma pontuação. Sem peso de sentimento.

### 9.1 Universo de ações

O universo é montado por regra e recalculado periodicamente. Nenhum papel é escrito à mão em
lugar nenhum do projeto: lista fixa congela no que existia no dia em que foi escrita e perde o
que surgir depois, que é justamente o que o sistema precisa encontrar.

A regra tem três filtros, aplicados nesta ordem:

1. **Negociabilidade.** Dos 13.509 ativos negociáveis da Alpaca, ficam apenas NASDAQ, NYSE e
   AMEX, o que dá 8.798. ARCA e BATS são majoritariamente ETFs; OTC é balcão, onde o volume é
   irregular e um rompimento não se sustenta.
2. **Liquidez.** Volume financeiro diário mediano dos últimos 90 dias igual ou superior a
   **US$ 20 milhões**, medido no feed consolidado. Sobram cerca de 2.400 papéis.
3. **Dados suficientes.** O papel precisa ter barras bastantes para a média de 20 dias e a
   máxima de 20 dias serem calculáveis.

O piso de US$ 20 milhões foi escolhido sobre a distribuição real medida, não por estimativa.
A mediana do universo é US$ 1,25 milhão por dia. Um piso de US$ 1 milhão deixaria passar cerca
de 4.500 papéis, muitos deles em faixa onde uma ordem de alguns milhares move o preço: o
rompimento aparece no gráfico mas não é executável. Um piso de US$ 50 milhões deixaria 1.600 e
cortaria small e mid cap, que é onde movimento de 20% a 30% acontece — exatamente o que o setup
procura. US$ 20 milhões mantém essa faixa com folga de liquidez muito acima do capital em jogo.

O piso é parâmetro no banco, não constante no código, e cada `backtest_report` registra qual
valor estava vigente quando foi gerado.

---

## 11. Portão de aprovação de setup

Um setup só pode ser habilitado em produção depois de produzir um `backtest_report` guardado
que satisfaça todos os critérios:

- No mínimo 40 disparos na janela in-sample
- Walk-forward: parâmetros ajustados estritamente em dados anteriores à janela de avaliação
- Expectativa positiva depois de descontar o custo real da corretora usada, declarado por
  mercado na §17 — não um número genérico. Custo errado aqui invalida o portão inteiro.
- Reportado ao lado de uma linha de base de entrada aleatória, na mesma janela e com o mesmo
  tempo de permanência

Se um setup não bate entrada aleatória com o mesmo tempo de permanência, ele é ruído com
passos extras e não entra. Esse portão é a principal defesa contra construir um gerador de
alertas caro.

---

## 12. Escopo: uso pessoal

O sistema é para uso próprio. Não há plano de dar acesso a terceiros neste roadmap.

O motivo é de custo, não de capacidade técnica. Abrir para outros exige aquisição de usuários,
suporte e marketing, além de cuidado regulatório: no Brasil, recomendar valores mobiliários
profissionalmente cai nas regras da CVM para analista de valores mobiliários (Resolução CVM
20); nos Estados Unidos, publicar conselho de investimento mediante pagamento levanta questões
sob o Investment Advisers Act. Regimes diferentes, mesma conclusão: vender sinal é atividade
regulada, vender ferramenta self-hosted em geral não é. Isto não é orientação jurídica, e a
recomendação é consultar um advogado antes de cobrar de qualquer pessoa.

Consequência prática para agora: construir com qualidade de produção, mas sem gastar energia
em multi-tenancy, billing, onboarding ou landing page.

### 12.1 Regra de sessão de banco

Toda função que escreve no banco recebe a sessão de quem a chama. Nenhuma abre a própria.
Apenas o ponto de entrada de cada comando abre uma sessão e a repassa.

O motivo é de teste, e é concreto. Os testes rodam dentro de uma transação que é desfeita ao
final, então nada que eles escrevem permanece. Uma função que abre a própria conexão escapa
dessa transação: o que ela grava é definitivo. Isso já aconteceu — um teste do construtor de
universo fechou a filiação dos 2.184 papéis reais, o rollback não desfez nada, e os testes
passaram. O dado foi restaurado manualmente.

Além do teste, é a regra que permite compor operações: duas escritas que precisam acontecer
juntas só podem compartilhar uma transação se ambas aceitarem a sessão de fora.

---

## 13. Verificação de ambiente

Nada é instalado antes de conhecer o ambiente real. Rodar e devolver a saída:

```bash
echo "--- OS ---"; lsb_release -d 2>/dev/null || head -2 /etc/os-release
echo "--- python ---"; python3 --version; which python3
echo "--- uv ---"; uv --version 2>/dev/null || echo "uv: not installed"
echo "--- postgres client ---"; psql --version 2>/dev/null || echo "psql: not installed"
echo "--- git ---"; git --version
echo "--- disk ---"; df -h / | tail -1
```

---

## 14. Roadmap

Cada fase tem um critério de saída. Não começar a próxima antes de cumprir o anterior.

### Fase 1 — Ingestão e armazenamento — CONCLUÍDA

**Infraestrutura.** PostgreSQL 16 local (sem Docker: o ambiente é WSL sem systemd), migrations
Alembic reversíveis nos dois sentidos, camada de sessão com fuso fixado em UTC, lint, formatação
e verificação de tipo cobrindo código e testes.

**Dados carregados.**

| Fonte | Volume | Período |
|---|---|---|
| Velas de cripto (BTC, ETH, SOL; 1h, 4h, 1d) | 101.826 | 3 anos |
| Open interest e posicionamento, 5 em 5 minutos | 949.886 | 3 anos |
| Funding (3.285 liquidações por símbolo) | incluído acima | 3 anos |
| Barras diárias de ações (2.184 papéis) | 1.567.077 | 3 anos |
| Notícia de empresa | 451.630 | 3 anos |
| Eventos societários | 403 | 3 anos |
| Barras diárias B3 (600 tickers) | 35.890 | 1 ano |

**Qualidade.** Auditoria automatizada (`guardrail.quality.completeness`) sem nenhum erro. Três
descontinuidades suspeitas em 1,57 milhão de barras, todas identificadas e tratadas pela §7.1.
97 testes.

**Critério de saída: cumprido.** Todos os backfills rodam até o fim, são retomáveis e idempotentes
— executar de novo insere zero linhas e não levanta erro. A auditoria reporta zero erros.

**Defeitos encontrados e corrigidos nesta fase.** Nenhum deles gerava erro; todos produziriam um
backtest com aparência válida:

- Open interest publicado como zero em falha de registro, que seria lido como queda de 100%
- Fuso do servidor deslocando o agrupamento por dia em 3 horas, misturando velas de dias vizinhos
- Contagem de linhas inseridas sempre retornando valor inválido, escondendo buracos
- Paginação que parava em erro enganoso ao receber página repetida
- Retomada de backfill que, após qualquer execução curta, nunca mais buscava o histórico antigo
- Barras com volume zero gerando um salto de 449 vezes na primeira negociação real do papel
- Eventos societários gravados no papel adquirido em vez do adquirente
- Código de negociação reaproveitado emendando duas empresas na mesma série de preço

### Fase 2 — Motor de regras e portão de backtest
- Registro de setups, parâmetros versionados, funções puras
- Backtester de laço de eventos sobre os candles guardados (próprio, ~300 linhas)
- `crypto_deleverage_flush` implementado e testado conforme §11
- Harness de linha de base com entrada aleatória

**Critério de saída:** `crypto_deleverage_flush` passa na §11, ou é descartado e substituído.
Descartar é um resultado aceitável desta fase.

> **Resultado medido:** os dois setups falharam no portão. `crypto_deleverage_flush` com as três
> condições deu 1 disparo em 3 anos (o portão exige 40). A exploração de 14 condições em
> meia-metade não bateu a linha de base de entrada aleatória em nenhum caso.
> `equity_volume_breakout` com liquidez avaliada na data do sinal dá média de +2,15% e mediana de
> +0,53%, com os 5% melhores respondendo por 115% do retorno total e 2 de 7 semestres negativos.
> Nenhuma regra de entrada automatizável foi encontrada nestes dados. O trabalho seguiu para a
> camada de antecipação — ver `PLANO_ANTECIPACAO.md`.

### Fase 3 — Motor de risco e contrato de trade
- Validação de contrato com todas as regras da §8
- Ciclo de vida da posição, rastreio de movimentação de stop, post-mortem obrigatório
- Testes baseados em propriedade na calculadora de risco (ver §15)
- Alertas no Telegram para: sinal novo, revisão vencida, tentativa de afastar stop, cooldown ativo

**Critério de saída:** cobertura de 100% dos ramos na calculadora de risco e no validador de
contrato. Esses dois módulos são o produto; nada mais precisa desse nível.

### Fase 4 — Dashboard
- Ver `PLANO_ANTECIPACAO.md` §5, que substitui esta fase.
- Autenticação: usuário único. Bind em `127.0.0.1`. Nunca exposto na internet aberta.

### Fase 5 — Setup de ações
- `equity_volume_breakout` pelo portão da §11
- Definição do universo sai da lista hardcoded e vai para uma tabela versionada

### Fase 6 — Adiada
Abertura para terceiros. Fora do escopo deste roadmap.

---

## 15. Estratégia de testes

**Unitários.** Toda função de setup contra fixtures de candle fixas, incluindo os casos de
borda: exatamente no limiar, um tick abaixo, dado de funding ausente, linha de open interest
velha.

**Baseados em propriedade (`hypothesis`).** Na calculadora de risco, os invariantes são:
- o `risk_amount` calculado nunca ultrapassa `max_risk_pct * free_capital` num contrato aceito
- `size * abs(entry - stop)` é igual a `risk_amount` dentro da tolerância de arredondamento
- contrato com `stop` do lado errado da `entry` é sempre recusado
- o risco aberto total, depois de aceitar qualquer contrato, nunca ultrapassa o teto da carteira
- aceitar e depois recusar um contrato deixa `account_state` inalterado

**Integração.** Coletores contra fixtures HTTP gravadas (`respx`), incluindo payload malformado,
resposta parcial e HTTP 429.

**Entradas adversariais.** Todo payload externo passa por Pydantic na fronteira, e os testes
incluem: preço negativo, volume zero, funding `null`, timestamp no futuro, candle duplicado, e
manchete contendo payload de SQL e de injeção de template. Nada vindo de API externa é
interpolado em query ou em prompt.

---

## 16. Segurança

Contexto financeiro, então isto não é opcional.

**Credenciais.** Nenhuma chave de corretora é necessária nas Fases 1 a 5, porque tudo é leitura
pública. Quando uma chave for adicionada, ela precisa ser somente leitura e trade, com permissão
de saque desligada e allowlist de IP habilitada na corretora. Chaves ficam no `.env`, que está
no `.gitignore` desde o primeiro commit, mais um scanner de segredos no `pre-commit`. Nunca uma
carteira quente com saldo relevante conectada a qualquer coisa automatizada.

**Superfície de ataque.**
- *Exposição do dashboard.* Ele mostra tamanho de posição e capital livre. Exposto na internet
  aberta, é um documento de alvo. Bind em localhost.
- *Envenenamento de dados.* Uma manchete ou nome de símbolo vindo de API externa, renderizado
  sem escape no dashboard, é XSS armazenado. Escape por padrão; nunca renderizar HTML cru de
  campo ingerido.
- *Abuso de rate limit e ban.* Um laço de retry sem backoff derruba a chave no meio da coleta.
  Token bucket por fonte, backoff exponencial com jitter, circuit breaker depois de N falhas
  seguidas.
- *Falha silenciosa de ingestão.* O pior modo de falha é um coletor morrer calado e o motor
  disparar sinal em dado velho. Toda avaliação de setup precisa checar frescor do dado e se
  recusar a disparar em dado mais antigo que o timeframe. Heartbeat no Telegram se alguma fonte
  passar do limite de atraso.
- *Race condition na aceitação de contrato.* Dois contratos aceitos ao mesmo tempo podem passar
  cada um pela checagem de risco da carteira. Serializar a aceitação com transação em isolamento
  `SERIALIZABLE`, ou advisory lock de linha na conta.
- *Precisão numérica.* Float em cálculo de tamanho de posição gera erro grosseiro. `NUMERIC` no
  Postgres, `Decimal` no Python, nunca `float`, em nenhum lugar onde haja dinheiro.

**Cadeia de suprimentos.** Lockfile fixado, `uv.lock` commitado. `uv pip audit` no CI.

---

## 17. Corretoras, custos e tributação

**Moeda da conta: USD.** Todo cálculo de risco da §8 é feito em dólar. A conversão para BRL
só aparece na apuração fiscal, com a cotação do dia de cada operação guardada junto ao
registro.

### Cripto

O capital operacional deve ficar em exchange brasileira autorizada pelo Banco Central, e não
em exchange estrangeira. Três motivos, em ordem de peso:

1. **Tributário.** A isenção de ganho de capital para vendas até R$ 35.000 por mês só vale
   para exchanges com sede no Brasil. Desde janeiro de 2024, operações em exchanges
   estrangeiras não contam para esse limite e caem no regime da Lei 14.754/2023, com alíquota
   fixa de 15% sobre o ganho, apurado anualmente. No tamanho de capital deste projeto, as
   vendas mensais ficam bem abaixo de R$ 35.000, o que significa imposto zero em exchange
   brasileira contra 15% em exchange estrangeira. Essa diferença é maior que qualquer
   economia de taxa de corretagem.
2. **Regulatório.** As Resoluções BCB 519, 520 e 521/2025 entraram em vigor em 2 de fevereiro
   de 2026. Exchanges estrangeiras têm até 30 de outubro de 2026 para protocolar o pedido de
   autorização ou transferir operações para uma entidade brasileira autorizada.
3. **Reporte.** O Brasil aderiu ao Crypto-Asset Reporting Framework a partir de 1º de janeiro
   de 2026. Além disso, a IN RFB 1888/2019 já obriga o próprio contribuinte a reportar
   operações mensais acima de R$ 30.000 feitas em exchanges estrangeiras, P2P ou DEX.

Custo de corretagem entra no portão da §11 como parâmetro por mercado, medido na corretora
efetivamente usada. Não usar número genérico.

### Ações americanas

O custo dominante para o operador brasileiro não é a corretagem, que hoje é zero na maioria
das plataformas voltadas ao Brasil, e sim o spread cambial na conversão de BRL para USD, que
fica na faixa de 1% a 2% por conversão dependendo do plano.

Consequência direta para a estratégia: converter a cada operação destrói o edge. Um giro de
ida e volta em câmbio consome de 2% a 4%, contra um alvo de movimento de 10% a 20%. A regra
operacional é converter uma vez, manter o saldo em dólar na conta, e girar dentro do dólar.
O câmbio vira custo de entrada único, não custo por trade.

O sistema registra o custo de câmbio como evento separado em `account_state`, e nunca como
custo de trade, para não contaminar a medição de expectativa dos setups.

---

## 18. Questões em aberto

1. Qual corretora de cripto brasileira será usada, e qual a taxa spot maker/taker dela? O
   número entra no portão da §11.
2. A corretora de ações suporta ordem stop nativa? Se não suportar, o stop vira alerta manual
   no Telegram e isso precisa estar explícito no fluxo da Fase 3.
3. A Alpaca permanece como fonte de dados de ações (leitura apenas). Nenhuma ordem é enviada
   por ela neste roadmap.

> Nada nesta seção é orientação jurídica ou contábil. Confirmar com um contador antes da
> primeira apuração.