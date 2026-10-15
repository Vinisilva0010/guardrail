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
