# Progresso

## Fase 0 — Verificação e limpeza do que está pendente

Data: 2026-10-15. Critério de saída cumprido: portão verde, `docs/FONTES.md` com as quatro
fontes classificadas, `api_quota` migrada e testada.

### O que foi feito

- **Portão de qualidade** (ruff, ruff format, mypy --strict, pytest) verde no estado inicial
  (97 testes) e ao final (132 testes).
- **`rss_news.py` conferido e corrigido.** O lint passava, mas o coletor tinha defeitos reais:
  - casamento por substring: `"sol"` casava com "solution" e `"eth"` com "method", gerando
    catalisador falso para SOL e ETH. Agora casa palavra inteira;
  - não gravava em `source_health`; agora grava uma linha por feed (`rss.<fonte>`);
  - falha de feed (HTTP 429, XML inválido) era engolida como "sem notícia"; agora é registrada
    como falha daquele feed e não esconde os outros;
  - `collect()` abria a própria sessão, contra a regra 10; agora só `run()` abre;
  - título passava de 2000 caracteres; agora 300, como manda o plano;
  - link sem validação; agora só `http`/`https`, na leitura e de novo na gravação;
  - docstring dizia que manchete sem símbolo era mantida; ela é contada e descartada, porque
    `catalyst` exige `instrument_id`.
  - 22 testes novos: payload gravado, malformado, parcial, HTTP 429, falsos positivos do
    casamento, idempotência, manchete hostil (`<script>`, `{{ }}`, SQL, `javascript:`).
- **`docs/FONTES.md`** com o resultado literal de cada fonte.
- **`api_quota`**: modelo, migration escrita à mão (sobe, desce e sobe de novo; `alembic check`
  sem diferença), módulo `collectors/quota.py` e integração no `BrapiClient`.
  - reserva atômica num único `UPDATE ... WHERE ... RETURNING`, sem corrida entre processos;
  - acima de 80% só passa coleta de fechamento; acima de 95% nada passa;
  - cada tentativa do `tenacity` reserva de novo, porque a brapi também conta a repetição;
  - `fetch_quotes` custa um por ticker, porque o teto da brapi conta tickers;
  - o `backfill_b3` agora é sequencial (a concorrência já era 1), usa uma sessão por lote e,
    se o teto estourar, guarda o que já tinha e marca `brapi.b3` como falha em `source_health`;
  - teste obrigatório: coletor acima do teto não faz chamada nenhuma (contado no `respx`).

### O que falhou

- **DefiLlama `/emissions`: HTTP 402**, exige plano pago. Fora do projeto (regras 22 e 29).
  Consequência: não haverá `token_unlock` na agenda. A tela terá de avisar "fonte de
  desbloqueio indisponível" e nunca "nenhum desbloqueio".

### O que ficou pendente

- **`ALPHAVANTAGE_API_KEY` e `FRED_API_KEY` ausentes do `.env`** (regra 30). Os testes 2 e 4 da
  §3.3 não foram rodados. Os coletores de resultados de empresa e de séries macro US ficam
  pulados na Fase 2 até as chaves existirem.
- **Alerta de cota acima de 95%**: hoje é só log em nível `error` (`quota.refused`). O envio
  para o Telegram entra na Fase 6.
- **Compromisso conhecido da cota:** ela é gravada na mesma transação do dado, como pede o
  plano. Se a transação do lote falhar por erro de banco, as reservas daquele lote somem
  junto, e o contador fica abaixo do uso real. Falha de HTTP não causa isso (é capturada
  por ticker). Folga do orçamento (~9.200 de 15.000) cobre; reavaliar se acontecer.
- **Focus:** o coletor precisa pedir `$orderby=Data desc`; sem isso a fonte devolve 2017 primeiro.
- **`src/guardrail/web/`** (`app.py`, `page.py`, `queries.py`) e as dependências `fastapi`/
  `uvicorn` no `pyproject.toml` são um rascunho anterior, não commitado e não conferido. Não
  foi tocado na Fase 0. Será avaliado na Fase 4 contra a §5 do plano (Jinja2 com autoescape,
  CSS próprio, bind em 127.0.0.1).

## Fase 1 — Esquema da antecipação

Data: 2026-10-15. Critério de saída cumprido: `alembic upgrade head` e `alembic downgrade -1`
rodam limpos nos dois sentidos; testes de schema cobrem unicidade de `identity_key` e rejeição
de `impact` inválido. Portão verde, 155 testes.

### O que foi feito

- Migration `e51a7c3d9b02` com `scheduled_event`, `macro_series`, `macro_point`, `expectation`,
  `event_impact_stat`, `news_theme`, `news_theme_item`, `news_theme_asset`, os três enums
  (`event_kind`, `event_scope`, `event_impact`) e os três índices da §4.
- Partida do `alembic revision --autogenerate`, mas editada à mão: os enums são usados por mais
  de uma tabela, então são criados uma vez e removidos explicitamente no downgrade (o
  autogenerate tentaria criá-los duas vezes e os deixaria órfãos). Confirmado: sobe, `alembic
  check` sem diferença, desce, sobe de novo (o segundo upgrade falharia se o tipo sobrasse).
- CHECK constraints escritos à mão no modelo e na migration:
  - `scheduled_event`: `identity_key` com exatamente 32 hex minúsculos; `earnings` e
    `token_unlock` exigem `instrument_id` (a chave deles é construída a partir do instrumento,
    então uma linha sem ele nunca seria reencontrada na recoleta);
  - `expectation`: desvio-padrão e respondentes não negativos, ano-horizonte entre 2000 e 2100;
  - `event_impact_stat`: `horizon` em `d1/d3/w1`, `n >= 0`, `pct_positive` entre 0 e 1,
    `p10 <= p90`, janela ordenada.
- 23 testes de schema (`tests/test_anticipation_schema.py`), com SQL cru onde o ponto é o banco
  recusar o valor: `impact` e `kind` desconhecidos, `identity_key` duplicada e malformada, mesma
  chave com outra data (adiamento atualiza, não duplica), cascata ao apagar instrumento.

### O que falhou

- Nada nesta fase.

### O que ficou pendente

- **Decisão minha, fora do texto literal do plano:** os dois CHECK de `scheduled_event`
  (formato da chave e instrumento obrigatório) e os de `expectation`/`event_impact_stat` não
  estão listados na §4; foram acrescentados porque a §1 regra 13 manda escrever os CHECK à mão.
  Se algum atrapalhar um coletor da Fase 2, a correção é na migration, não no coletor.
- A tabela de ativos de referência da Fase 3 (BTC, ETH, SOL, SPY, QQQ, IWM, IBOV, USDBRL) não
  existe ainda; entra com a migration da Fase 3, como manda a §4 ("em uma tabela, não no
  código").
- `event_impact_stat.pct_positive` e os quantis usam `NUMERIC(18,8)`; a unidade (fração, não
  percentual) será fixada no cálculo da Fase 3 e testada com valor fixo.

## Fase 2 — Coletores de evento

Data: 2026-10-15. Critério de saída cumprido, com uma ressalva (abaixo). Portão verde.

### O que foi feito

| Item | Resultado |
|---|---|
| 1. ForexFactory | Coletor feito (`forexfactory.py`). Roda e é idempotente no banco real. |
| 2. Alpha Vantage | **Sem coletor**: `ALPHAVANTAGE_API_KEY` ausente (regra 30). |
| 3. DefiLlama | **Sem coletor**: HTTP 402, fonte paga (regras 22 e 29). Fora do projeto. |
| 4. BCB | SGS (Selic meta, IPCA) e Focus (IPCA, Selic, PIB, Câmbio) feitos (`bcb.py`). **Datas do Copom: indisponível** (ver `FONTES.md`). |
| 5. FRED | **Sem coletor**: `FRED_API_KEY` ausente (regra 30). |
| 6. Vencimentos | Opção (3ª sexta, recuada se for feriado dos EUA) e futuro (último dia útil), em `calendar_rules.py`, fonte `manual`. |
| 7. Feriados | EUA derivados do `/v2/calendar` da Alpaca (`us_calendar.py`, inclui meio-pregão); B3 por tabela versionada (`reference/b3_holidays.json`), conferida na página oficial da B3 em 2026-10-15. |

**Verificação no banco real** (`scheduled_event`, eventos futuros): 3 fontes distintas
(`alpaca` 8, `forexfactory` 56, `manual` 17), até 167 dias à frente, 0 `identity_key`
duplicada. Cada coletor rodou duas vezes seguidas: a segunda insere 0 linha.

Infraestrutura nova: `collectors/events.py` (chaves, upsert com COALESCE, reconciliação),
tabela `event_kind_pattern` semeada por migration (o tipo do evento sai de padrões no banco,
não de `if`), migration `a93d5e7f1c46`.

### Desvios do plano (decisões minhas, para revisão)

1. **`identity_key` de calendário.** A fórmula da §4 (`fonte|tipo|título|mês`) perdia evento em
   silêncio. Corrigi: o país/moeda entra no texto da chave (`CPI m/m` de USD e de EUR são
   eventos diferentes); a granularidade é **mês** só para `rate_decision`, `inflation_print` e
   `gdp_print`; **minuto** para `central_bank_speech` (a Lagarde falou duas vezes no mesmo dia
   no feed real); **dia** para o resto (pedidos de seguro-desemprego e estoques de petróleo
   são semanais). Efeito colateral: evento de chave por dia que é adiado vira linha nova, então
   `drop_vanished` apaga do banco o evento futuro da mesma fonte que sumiu do feed, mas só quando
   o documento veio íntegro (nenhuma linha ilegível). Feed vazio ou parcial nunca apaga nada.
2. **Feriados do ForexFactory são descartados** (a hora vem em outro fuso e não dá uma data
   confiável); feriados vêm dos itens 7.
3. **Tabela B3:** o plano pede tabela versionada; usei a lista da B3 conferida por busca (URL e
   data no próprio JSON). Ela só cobre 2026. O coletor marca `calendar.rules` como falha em
   `source_health` quando falta um ano a menos de 60 dias de começar (hoje isso acontece em
   2026-11-02 para 2027) — é um lembrete deliberado, não um defeito.
4. **Impacto dos vencimentos** (`medium` nos meses de trimestre, `low` nos outros): regra
   minha, o plano não define.
5. **Ponto SGS com data futura é descartado** (a série 432 traz semanas à frente).

### O que falhou / bugs achados no caminho

- **Colisão real de chave** achada rodando o coletor no feed vivo: duas falas da Lagarde no mesmo
  dia colapsavam numa linha. Corrigido (chave por minuto) com teste. Como a chave mudou depois da
  primeira carga, ficaram 17 pares de falas **passadas** duplicadas (a reconciliação só toca
  futuro). Apaguei à mão a linha de chave antiga de cada par (17 linhas, re-coletáveis) e conferi
  0 duplicata restante. Foi um artefato de uma troca de formato no meio da fase; não se repete.
- **Testes quebraram quando o banco real ganhou dados** (contavam "todas as linhas"): agora cada
  teste de evento usa uma `SOURCE` privada e filtra por ela.
- ForexFactory respondeu **429** depois de 5 requisições minhas em ~10 min (ver `FONTES.md`).

### O que ficou pendente

- **Ressalva do critério de saída:** os "60 dias de eventos futuros" valem para as três fontes
  somadas. O ForexFactory cobre 1 semana; o que passa de ~3 dias é só feriado e vencimento.
  **Não há data futura de Fed, CPI ou resultado de empresa além de uma semana.** Para ter, faltam
  FRED, Alpha Vantage (chaves) e o calendário do Copom (sem API). A tela da Fase 4 tem de
  mostrar isso e não "nenhum evento".
- **Histórico para a Fase 3:** `event_impact_stat` precisa de reuniões passadas (o exemplo do
  plano usa n=11 do Fed). Nenhuma fonte desta fase traz histórico de evento: o ForexFactory
  é só a semana corrente. A tabela só acumula daqui para a frente, então a maior parte das
  estatísticas ficará com `n < 8` ("histórico insuficiente") por meses. Isso é o comportamento
  honesto, mas precisa de decisão do operador sobre uma fonte de histórico (ex.: FRED, quando
  houver chave).
- Meio-pregão da B3 (ex.: quarta-feira de cinzas, abertura às 13h) não é coletado.
- A tabela da cota de requisição (`api_quota`) não é usada por estes coletores porque nenhuma
  dessas fontes tem teto declarado; o agendador da Fase 5 é quem limita a cadência.

## Fase 3 — Distribuição histórica

Data: 2026-10-15. Critério de saída: **cumprido no código, vazio nos dados reais** (explicado
abaixo). Portão verde.

### O que foi feito

- `guardrail.analysis.event_impact`: `measure` (um evento), `summarize` (estatísticas) e
  `recompute` (reconstrói `event_impact_stat` inteira na sessão de quem chama; idempotente).
  Comando: `uv run python -m guardrail.analysis.event_impact`.
- Tabela `event_reference_asset` (migration `b27c40d8e915`, sobe e desce, semeada com a lista do
  plano): os ativos de referência ficam em tabela, não no código.
- **Teste de valor fixo** (`tests/test_event_impact.py`), com mediana, p10, p90, mediana absoluta
  e % positivo calculados à mão no próprio teste, nos três horizontes (d1, d3 = 2×, w1 = 3×).
  Validei o teste por **mutação**: deslocar a fórmula do quantil e trocar `>` por `>=` no limiar
  de 8× fazem testes falharem.
- `n < 8`: a linha é gravada com o `n` verdadeiro e `is_displayable(n)` é o único ponto que a
  tela deve consultar (constante `MIN_SAMPLE = 8`).
- Descontinuidade da SPEC §7.1 descarta a amostra: ação societária registrada na janela (inclusive
  no dia-base, de propósito, o lado conservador), salto acima de 8× entre fechamentos
  consecutivos (inclui a barra do dia do evento), código dormente há mais de 180 dias. Janela com
  buraco de mais de 6 dias entre barras também sai, porque um retorno medido sobre uma semana
  sem dados não é um retorno de 1 dia. Os motivos são contados no relatório.

### Decisões minhas, para revisão

1. **Uma amostra por (tipo, ativo, dia).** CPI e Core CPI saem no mesmo instante; contar os dois
   dobraria uma única reação e inflaria o `n` que a tela mostra.
2. **Escopo da amostra.** Evento sem ativo entra só se for de alto impacto e de escopo `us` ou
   `br`. Misturar "CPI" dos EUA com índice de preços da Alemanha sob o mesmo tipo mediria nada.
   Evento ligado a ativo (resultado de empresa) entra com qualquer impacto. `holiday` e `other`
   ficam de fora (não são evento de preço).
3. **Retorno como fração** (0,012 = 1,2%); `pct_positive` também (0 a 1). A tela formata.
4. **Definição de horizonte:** base = último fechamento antes do dia do evento; alvo = 1º, 3º ou
   5º fechamento depois do dia. Em ação, são pregões; em cripto, dias.
5. **Quantil:** interpolação linear (tipo 7), em `Decimal`, arredondado a 8 casas.

### O que falhou / está vazio

- **`event_impact_stat` está vazia no banco real, e o motivo está nos dados, não no código.**
  (a) Só há 4 eventos reais de alto impacto já vencidos (CPI de 14/10) porque nenhuma fonte traz
  histórico de evento (ver pendência da Fase 2). (b) As velas diárias no banco acabam em
  **03/10/2026**: os backfills não foram reexecutados desde então, então nenhum evento de
  12–14/10 tem fechamento de referência. Ambas as causas se resolvem com tempo e com a Fase 5.
- **Ativos de referência ausentes:** SPY, IWM, IBOV e USDBRL **não têm velas** (o universo da
  Alpaca exclui ARCA, onde o SPY negocia; não há coletor de câmbio nem de índice). QQQ, BTC, ETH
  e SOL existem. A recomputação lista os ausentes no relatório; não substituí nenhum (BOVA11 não
  entra no lugar do IBOV por decisão minha: se quiser, é uma linha na tabela).

### O que ficou pendente

- Fonte de histórico de eventos (Fed, CPI, payroll, Copom) para sair de `n < 8`. Nesta data só
  há uma, a FRED, e depende de chave.
- Decidir se SPY/IWM entram por outro caminho de coleta (a Alpaca serve ETF, o filtro de universo
  é que os exclui) e se o IBOV/USDBRL valem uma fonte.
