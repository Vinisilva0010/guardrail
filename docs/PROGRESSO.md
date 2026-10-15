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

## Fase 4 — A tela

Data: 2026-10-15. Critério de saída cumprido. Portão verde.

### O que foi feito

- `uv run guardrail-web` sobe em `127.0.0.1:8787`. Recusa qualquer outro `--host` (teste).
- **Oito páginas**, não sete: `/`, `/cripto`, `/eua`, `/brasil`, `/macro`, `/mundo`, `/agenda`,
  `/radar`. O plano diz "sete rotas" e "sete links" mas lista oito; implementei as oito.
- FastAPI + Jinja2 (`autoescape=True`, `StrictUndefined`) + um `app.css` com as fichas da §5.2
  (todas as cores em variáveis, nenhuma fora do `:root`, verificado por teste) + `app.js` de
  10 linhas (recarrega a cada 60 s e guarda a rolagem). A página funciona sem o JS.
- `sparkline.py` (30 fechamentos viram um `<polyline>`; valor fixo calculado à mão no teste),
  `queries.py` (uma função por bloco, com teto de linhas), `health.py`, `clock.py`,
  `format.py` (números, datas e idades em pt-BR, com sinal de menos tipográfico).
- **Liquidez aplicada na data do dado**: mediana do giro dos 90 pregões até a última barra, piso
  de US$ 20 M (EUA) e R$ 5 M (Brasil). Teste: papel ilíquido e papel com barra velha ficam fora.
- **Cabeçalho em toda página:** navegação, relógio dos mercados, um ponto por grupo de fonte
  (verde / âmbar / vermelho / vazado para "sem coletor"), contagem em texto (a cor não é o único
  portador) e a linha de limites de risco.
- **Nenhum bloco mudo:** cada bloco sem dado imprime faixa de aviso com motivo e a hora da
  consulta. Teste de rota percorre as 8 páginas, com banco cheio **e** vazio, e falha se houver
  seção sem card e sem aviso.
- **Segurança:** CSP `default-src 'none'; style-src 'self'; script-src 'self'`, `nosniff`,
  `no-store`, `/docs` e `/openapi.json` desligados, nenhuma rota de escrita (POST/PUT/DELETE
  dão 405), sem CORS. Link só vira `href` se for `http`/`https` (`rel="noopener noreferrer"`).
  Testes com manchete, título de evento e símbolo hostis (`<script>`, `{{ 7*7 }}`, SQL,
  `javascript:`). Validei por **mutação**: desligar o autoescape e remover o filtro de esquema
  derrubam testes.
- Snapshot do card de ativo nos três estados (completo, sem evento, fonte falhada), com os
  arquivos em `tests/snapshots/` lidos à mão.
- **Agrupamento de temas** (`analysis/themes.py`), que a §4 descreve mas nenhuma fase listava:
  sem ele `/mundo` ficaria vazio para sempre. Termos em comum (≥ 2 compartilhados e ≥ 40%),
  janela de 48 h, rótulo pelos três termos mais frequentes. Contei **histórias** (veículo +
  manchete), não linhas de `catalyst` (uma notícia marcada em 4 ações viraria 4 notícias). O teste
  caiu num bug meu real: `Counter.update(dict)` soma os valores, não as chaves.

### Decisões minhas, para revisão

1. **O cabeçalho de risco mostra só os limites**, porque `account_state`, `position` e
   `locked_capital` não existem (SPEC Fase 3 e §9). Mostra "capital livre, risco aberto e
   posições do mês: ainda sem registro" em vez de um número que não tem. As constantes da §8
   são lidas direto do `config.py`, onde já estavam (uma primeira versão minha duplicou
   as constantes num módulo novo; removi para não haver duas fontes de verdade).
2. **`/radar` não cumpre o plano inteiro**: sem as tabelas acima, não há posição, capital travado
   nem data de revisão para destacar. A página diz isso numa faixa vermelha e lista os limites.
   Fica dependente da Fase 3 da SPEC.
3. **Relógio dos mercados:** NYSE com horário (09:30–16:00 ET, e o meio-pregão vem do calendário
   da Alpaca); **B3 só diz se negocia hoje**, sem horário. O horário da B3 muda com o horário de
   verão americano e eu não ia afirmar um minuto que não verifiquei.
4. **Bloco 3 do card de ativo** é "Posicionamento" para cripto e "Volume e liquidez" para ação,
   porque posicionamento só existe para perpétuos de cripto. Os cinco blocos seguem na mesma ordem.
5. **"Movimento anormal"** = volume ≥ 2× a média de 20 dias (corte meu, o plano só manda
   ordenar). A faixa de aviso do card é a de 3× do plano.
6. **Evento do card** usa os mesmos escopos da amostra estatística (`us`/`br`); a agenda da
   página de cripto também lista eventos globais, a do card não.
7. **O nome do ativo não aparece** ("Solana · Binance"): `instrument` não tem coluna de nome. O
   card mostra o símbolo e a praça. Precisaria de uma coluna nova e de preenchê-la no coletor.
8. **Leitura de posicionamento** em uma linha: quem paga (sinal do funding) e se o open interest
   cresce ou cai (corte de ±2%). Descreve; não diz o que o preço vai fazer.

### O que a tela mostra hoje, com os dados reais (e por quê)

Praticamente tudo com aviso. Não é defeito da tela, é o estado do banco: velas diárias até
**02–03/10**, posicionamento até **04/10**, notícias até **05/10**. Os coletores não rodam
sozinhos até a Fase 5. A tela diz isso em cada bloco ("sem dado atual · última vela de cripto é
de 03/10, esperada 14/10; coletor de barras parado") e no cabeçalho (6 fontes vermelhas).

### O que ficou pendente

- Fase 5 (agendamento) resolve a defasagem dos dados.
- Tabelas de capital, posição e contrato (SPEC Fase 3) para `/radar` e o cabeçalho.
- Coluna `name` em `instrument`.
- Eventos próprios de ativo (resultados, desbloqueios) seguem sem fonte: aviso permanente nos cards.

## Fase 5 — Agendamento

Data: 2026-10-15. Critério de saída: **parcialmente verificado** (ver "Não verificado"). Portão verde.

### O que foi feito

- `uv run guardrail-scheduler` (APScheduler dentro de um processo). `--list` mostra a agenda,
  `--once [JOB ...]` roda tarefas agora e sai, `--skip JOB ...` deixa tarefas de fora.
- **20 tarefas**, todas em UTC: batimento (60 s), calendário macro (6 h), notícia Alpaca e RSS
  (30 min), temas (30 min), open interest recente (15 min), velas cripto 1h/4h/1d, funding (4 h),
  dumps da Vision (diário), barras dos EUA (diário), eventos societários, feriados dos EUA,
  vencimentos e feriados da B3, BCB, fechamento da B3 (terça e sexta), estatística de eventos
  (noite), auditoria (noite) e alertas (5 min). Os horários de disparo estão conferidos por teste.
- **Política de falha** (funções puras, testadas com tempo fixo e gerador semeado): backoff
  exponencial `30 s × 2^(n-1)` com teto de 15 min e jitter de 50–100%; **disjuntor** que abre
  na 5ª falha seguida por 1 h e reabre na hora se a tentativa "meio-aberta" falhar. A tarefa do
  calendário macro (fonte que responde 429) e a da B3 têm backoff próprio mais longo.
- **Falha não derruba o processo nem as outras tarefas**: exceção, falha reportada, ou falha ao
  gravar a saúde viram resultado; só `KeyboardInterrupt` passa.
- Cada tarefa grava `job.<nome>` em `source_health`, e os coletores seguem gravando a própria
  fonte. Um batimento `scheduler.heartbeat` mostra no cabeçalho se o agendador morreu.
- **Uma instância só** (advisory lock do Postgres), **sem sobreposição** (`max_instances=1`,
  `coalesce`), desligamento limpo em SIGTERM/SIGINT.
- **Reiniciar é seguro e barato**: na partida só roda o que está velho (fonte mais antiga que
  1,5 × a cadência). Em teste real, com tudo em dia, o único catch-up foi a auditoria.
- Novo coletor `oi_recent.py`: open interest dos últimos 30 dias pela REST da Binance. A SPEC §5 o
  lista e `store_metrics` já previa a convivência com a Vision, mas ele não existia; sem ele o
  posicionamento nunca tinha menos de um dia. Preserva as razões que a Vision grava (testado).
- Coletor de notícia ganhou `live=True` (lê até agora; o histórico continua parando em ontem).
- Agrupamento de temas: vocabulário de nota de analista e corretoras nas stopwords. Em janela
  real de 1.278 manchetes isso reduziu o maior "tema" de 218 notas sem relação para 28.

### Incidentes desta fase (todos achados rodando de verdade)

1. **Fechamento da B3 gastou 1.081 das 15.000 requisições do mês em minutos** e criou 489
   instrumentos. O adaptador chamou `backfill_b3(limit=None)`, que percorre toda a listagem
   `/available` da brapi. Interrompi à mão, apaguei os 489 instrumentos criados por essa rodada
   (973 velas, sem nenhuma outra dependência; os 600 originais intactos) e reescrevi a tarefa:
   agora só visita os tickers já em `instrument`, com teto de 700 requisições por rodada, parada
   limpa quando a cota recusa, e testes que travam "uma requisição por ticker do universo e
   nenhuma fora dele". A cota usada neste mês é **1.081 / 15.000** (7%); não dá para devolver.
2. **Barras dos EUA falhariam toda noite**: a Alpaca mede "dado recente" pela data de Nova York,
   e eu usava a de UTC. Corrigido, e a tarefa foi para 05:30 UTC. A tela usa a mesma noção de
   "esperado" (e, para a B3, a de coleta só às terças e sextas).
3. **A auditoria se contava como fonte quebrada** e nunca se recuperava. Passou a ignorar as
   próprias linhas, e o resultado dela vai para `quality.completeness` em vez de falhar a tarefa.
4. **A trava de instância única não era liberada** ao fechar a conexão (volta ao pool ainda
   segurando o lock da sessão). Adicionei `release_lock`.
5. Duas constantes de risco duplicadas (em `config.py` e num módulo meu): removi a minha.

### Não verificado

- **"Roda 24 horas sem vazar memória"**: não consigo verificar numa sessão. O que medi está em
  "Teste de resistência" abaixo, e é só uma amostra curta.
- **A coleta intradiária da B3** (watchlist de 15 tickers a cada 30 min, §3.5) **não foi
  implementada**: não há onde gravar cotação intradiária (o `B3Quote` não tem tabela) nem lista de
  15 tickers por mercado. As 600 ações têm fechamento duas vezes por semana.
- Tarefas de Alpha Vantage, DefiLlama e FRED não existem (sem coletor; ver Fase 2).
- Reconstrução do universo da Alpaca (`universe.py`) não está agendada: o plano não a lista.

## Fase 6 — Telegram

Data: 2026-10-15. Critério de saída: **cumprido com a API simulada; nunca enviou uma mensagem
real**, porque `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` não existem no `.env` (regra 30).

### O que foi feito

- `alerts/telegram.py`: `httpx` direto, sem SDK, **só envia** (nenhuma rota recebe nada, não há
  webhook). Texto puro, sem `parse_mode`, para manchete de terceiro não injetar formatação. O
  token vai no caminho da URL, então todo erro é reconstruído só com fatos que não o contêm
  (código HTTP, descrição do Telegram), sem encadear a exceção do `httpx` cuja mensagem traz a URL.
- **Os quatro alertas, e só eles** (`alerts/rules.py`, funções puras): evento de alto impacto a 24 h
  e a 1 h; volume ≥ 3× em ativo da watchlist; revisão de posição vencida; fonte parada há mais de
  6 h. Cada alerta tem uma chave que diz sobre o quê é, gravada em `alert_sent` **só depois de o
  Telegram aceitar**; falha de entrega interrompe a passada e o resto sai na seguinte.
- `uv run guardrail-alerts --dry-run` mostra o que sairia sem enviar nem gravar nada.
  `uv run guardrail-watch add|remove|list` gerencia a watchlist (a tela é só leitura).
- **Redação de segredos** (plano §7, que ainda não existia): `redact.py` mascara `apikey`,
  `api_key`, `token`, `secret`, o token do bot no caminho da URL e valores `Bearer`, em todo
  campo de log, em traceback e no texto de erro. Fica ligado em todo comando, porque o pacote
  `guardrail` configura o `structlog` ao ser importado.
- Teste exigido: os quatro disparam com a API simulada; nenhum segredo em log, em erro nem em
  cadeia de exceção; token escondido no `repr` do cliente.

### Decisões minhas, para revisão

1. "Ativos do operador" não existia: criei a tabela `watchlist` e o comando. O alerta de evento
   cobre evento de mercado inteiro (alto impacto, `us`/`br`, os mesmos escopos da amostra
   estatística) mais evento de ativo da watchlist.
2. Evento visto pela primeira vez a menos de 1 h recebe só a mensagem de 1 h (a de 24 h é marcada
   como superada). Evento só com data recebe uma mensagem, no dia anterior ou no próprio dia.
3. Fonte parada: uma mensagem por interrupção (a chave inclui a última coleta boa), no máximo 10
   por passada. As linhas `job.*` e o batimento ficam de fora; um agendador morto não consegue
   avisar que morreu, só o ponto "Agendador" do cabeçalho.
4. Quando o token falta, a tarefa não faz nada e o cabeçalho mostra "Alertas Telegram: sem
   coletor". Quando o chat id falta, idem.

### O que ficou pendente

- **Chaves do Telegram no `.env`** e um envio real de teste.
- **Alerta 3 (revisão vencida) só funciona com as tabelas de posição da SPEC Fase 3**, que não
  existem. A interface `ReviewSource` está pronta e testada com uma fonte falsa; a real devolve
  "não monitorado", e a passada diz isso, em vez de devolver lista vazia, que pareceria "nada
  vencido".
- O plano pede um padrão novo no scanner de segredos do `pre-commit` para o formato das chaves da
  Alpha Vantage e da FRED. **Não existe `.pre-commit-config.yaml`** (nem GitHub Actions) neste
  repositório, então não há scanner para estender. Nenhuma fixture com chave foi gravada (os
  coletores dessas duas fontes não existem).

## Fase 7 — Medição

Data: 2026-10-15. **Não executada. Isso é um resultado, não um contorno.**

Duas razões independentes, com números:

1. **O código dos setups e do backtester não está no repositório.** Não há uma linha de
   `crypto_deleverage_flush`, de `equity_volume_breakout` nem de laço de backtest em `src/` ou
   `tests/`. A SPEC §14 descreve o resultado medido (1 disparo em 3 anos; +2,15% de média e
   +0,53% de mediana para o setup de ações), mas o código que o produziu não foi commitado. Rodar o
   portão da §11 exige reescrevê-los, e reescrever de memória um teste cujo veredicto já foi
   "falhou" seria produzir um número sem lastro.
2. **Não há evento histórico para medir.** `scheduled_event` guarda 53 eventos já vencidos, de
   12 a 15/10/2026 (4 dias), e **um único dia de alto impacto vencido** (o CPI de 14/10, 4
   linhas do mesmo instante). O portão pede 40 disparos mínimos, em meia-metade, contra uma linha
   de base de entrada aleatória. Com dado de um dia isso não tem como fechar.

Quando houver as duas coisas (setups no repositório e meses de agenda acumulada ou uma fonte de
histórico, como a FRED), a medição é executável. Até lá, **nenhuma regra é habilitada** e o sistema
segue como tela de contexto e disciplina, que é o resultado aceitável descrito no plano.

## Teste de resistência do agendador (amostra curta, não as 24 h)

Daemon real por cerca de 17 minutos (sem a B3): memória constante em 78 MB, batimento a cada
minuto, `open_interest` disparou sozinho às 02:55 UTC (180 linhas), o disjuntor abriu na 5ª falha
da versão antiga da auditoria sem derrubar o processo, e SIGTERM encerrou limpo. Não cheguei a
observar o cron das 03:02 nem a reinício comparando contagens; as 24 h seguem **não verificadas**.
Os testes de reinício e de idempotência dos coletores existem por coletor, não pelo processo.

### Reinício do agendador (verificado depois, 2026-10-15 04:11 UTC)

Daemon real subido duas vezes com SIGTERM entre as subidas. Na primeira partida, passada mais de
uma hora, o catch-up rodou só o que estava velho (notícias, temas, open interest, velas 1h, alertas,
auditoria) e todas as tarefas terminaram `ok`; a auditoria agora conclui e aponta apenas
`brapi.b3` (última coleta 05/10). Na segunda partida o catch-up foi só `klines_1h`, `alerts` e
`audit`; contagens antes e depois do reinício iguais, `identity_key` duplicada = 0, cota da brapi
parada em 1.081. O teste de 24 h continua não verificado.
