# Guardrail — Camada de Antecipação e Interface

> Plano de execução para rodar no Claude Code sem supervisão.
> Versão 1.0 — 2026-10-14
> Complementa e, em dois pontos, **altera** a `SPEC.md` (ver §11).
>
> **Regra de idioma:** este documento e qualquer documento de decisão ficam em português.
> Código, nomes de tabela, comentários, commits, README e SECURITY ficam em inglês.

---

## PROMPT INICIAL — cole isto no Claude Code

```
Leia PLANO_ANTECIPACAO.md e SPEC.md por inteiro antes de escrever qualquer linha.

Execute as fases na ordem, começando pela Fase 0. Não comece uma fase antes de
cumprir o critério de saída da anterior.

Obedeça a seção "§1 Regras do loop" sem exceção. Em especial: nunca instale nada
fora do projeto, nunca altere as constantes de risco, e se uma fonte de dados
falhar na verificação, registre a falha em docs/FONTES.md e siga para o próximo
item — não invente substituto e não deixe a tela renderizar bloco vazio.

Faça commit a cada critério de saída cumprido, com mensagem em inglês.
Ao final de cada fase, escreva em docs/PROGRESSO.md o que foi feito, o que falhou
e o que ficou pendente, em português.
```

---

## §0 O que você precisa fazer com as próprias mãos

São os únicos passos humanos do plano inteiro. Tudo gratuito, nenhum pede cartão.
Depois disso o Claude Code segue sozinho.

| # | O que | Link direto | Onde guardar |
|---|---|---|---|
| 1 | Chave Alpha Vantage (calendário de resultados das ações US) | https://www.alphavantage.co/support/#api-key | `ALPHAVANTAGE_API_KEY` no `.env` |
| 2 | Chave FRED (séries macro americanas) | https://fredaccount.stlouisfed.org/apikeys | `FRED_API_KEY` no `.env` |
| 3 | Token do bot do Telegram — mande `/newbot` no `@BotFather` | https://t.me/BotFather | `TELEGRAM_BOT_TOKEN` no `.env` |
| 4 | Seu chat id — mande qualquer coisa para o `@userinfobot` | https://t.me/userinfobot | `TELEGRAM_CHAT_ID` no `.env` |

Comando para gravar sem colar chave no chat (roda uma vez, por chave):

```bash
cd ~/guardrail && read -p "Cole a chave e tecle Enter: " K && \
  echo "ALPHAVANTAGE_API_KEY=$K" >> .env && unset K && \
  chmod 600 .env && tail -1 .env | sed 's/=.*/=<gravado>/'
```

Os itens 3 e 4 só são necessários na Fase 6. Os itens 1 e 2 são necessários na Fase 2.
Se faltarem, o Claude Code deve pular o coletor correspondente, registrar a pendência
e continuar — não travar o loop esperando por você.

---

## §1 Regras do loop

Estas regras existem porque o agente vai rodar sem alguém olhando. Cada uma veio de
um erro que já aconteceu neste projeto.

**Ambiente — não negociável**
1. Nada é instalado no sistema. Só `uv add` dentro do projeto. Nunca `apt`, `pip` global,
   `npm -g`, nem alteração de `~/.bashrc`, `/etc/wsl.conf` ou qualquer toolchain global.
2. Nenhum arquivo é tocado fora de `~/guardrail`.
3. O Postgres é local, sem Docker. Em cada sessão nova: `sudo service postgresql start`.
4. Branch principal é `main`. Nunca `master`. Nunca `git push --force`.
5. Um comando por vez, com a saída conferida antes do próximo. Comando que falha é lido
   e corrigido, não repetido.

**Qualidade — portão antes de cada commit**
6. Tudo isto tem que passar, sem exceção e sem `# type: ignore` novo:
```bash
   uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
```
7. `mypy --strict` cobre `src` **e** `tests`. Teste sem tipo não entra.
8. Todo coletor novo nasce com quatro testes: payload gravado com `respx`, payload malformado,
   resposta parcial, e HTTP 429. Sem isso o coletor não é commitado.
9. Proibido `assert ... or True` e variações que nunca falham.

**Banco**
10. Toda função que escreve no banco recebe a sessão de quem a chama. Nenhuma abre a própria.
    Apenas o ponto de entrada de cada comando abre uma sessão e a repassa.
    (`SPEC.md` §12.1 — isso já apagou 2.184 linhas reais uma vez.)
11. Dinheiro e preço: `NUMERIC` no Postgres, `Decimal` no Python. Nunca `float`.
12. Timestamp sempre `timestamptz` gravado em UTC. A sessão já fixa UTC na conexão; manter.
13. Migration tem que subir e descer. Alembic não gera CHECK constraint, não gera drop de tipo
    enum e não gera valor novo de enum — escrever à mão e testar `downgrade` antes de commitar.
14. `drop_constraint` recebe o nome curto; a convenção do projeto prefixa sozinha.

**Dados**
15. Escrita idempotente: `ON CONFLICT DO NOTHING` para série temporal, `ON CONFLICT DO UPDATE`
    com `COALESCE` quando duas fontes preenchem o buraco uma da outra.
16. Backfill retomável pelos dois lados: checar a barra mais nova **e** a mais antiga guardada.
    Só olhar a mais nova já quebrou duas vezes.
17. Fato registrado nunca é inferido do gráfico. Evento societário, dormência de código e data
    de resultado são buscados da fonte. Quando o trabalho virar "achar o número que separa dado
    bom de dado ruim", **falta dado, não calibragem** — isso errou três vezes neste projeto.
18. Nenhuma lista de ativo escrita à mão em lugar nenhum. Universo é regra (`SPEC.md` §9.1).

**Falha de fonte — o ponto mais importante para rodar sozinho**
19. Fonte que falha na verificação **não** ganha substituto improvisado. Registrar em
    `docs/FONTES.md`: URL testada, comando, código HTTP, erro, data. Seguir para o próximo item.
20. Nenhum bloco da tela renderiza vazio em silêncio. Sem dado, o card mostra faixa de aviso com
    o motivo e a hora da última tentativa. Tela com buraco mudo é pior que tela sem o bloco.
21. Todo dado exibido carrega a idade. Dado mais velho que o limite da fonte aparece marcado como
    velho e não entra em nenhuma regra.
22. Nenhuma fonte paga. Nenhum contorno de proteção antirrobô. Se a única saída for pagar ou
    burlar, a fonte está fora — escrever isso em `docs/FONTES.md` e seguir.

**Segurança**
23. `.env` com `chmod 600`, no `.gitignore` desde sempre. **O repositório é público.**
24. Fixture de teste gravada de API que leva chave na URL (Alpha Vantage, FRED) tem a chave
    substituída por `REDACTED` antes de ir para o repositório. Conferir o diff antes do commit.
25. Conteúdo de terceiro (manchete, nome de ativo, título de evento) é dado hostil: escapado
    sempre, nunca interpolado em SQL nem em HTML cru. URL só vira link depois de validar o
    esquema `http`/`https`, com `rel="noopener noreferrer"`.
26. A tela escuta só em `127.0.0.1`. Nunca `0.0.0.0`.
27. Constantes de risco (`SPEC.md` §8) não mudam neste plano. Nem para teste.

**Quando parar e reportar em vez de decidir**
28. Migration que precisaria apagar dado.
29. Fonte que passou a exigir pagamento ou que mudou o contrato de resposta.
30. Chave ausente no `.env`.
31. Qualquer coisa que exija alterar configuração do Ubuntu.

Nestes quatro casos: escrever em `docs/PROGRESSO.md`, pular o item, continuar o resto.

---

## §2 O que "ver o mercado antecipado" é, concretamente

Isto precisa estar escrito com precisão, porque a diferença entre o que dá para construir e o
que não dá é a diferença entre uma ferramenta útil e uma que mente.

**Não dá para prever preço.** Nenhuma projeção de preço futuro entra neste sistema, de nenhuma
fonte, em nenhuma tela.

**Dá para antecipar três coisas, e as três juntas respondem "o que pode acontecer":**

**1. Agenda — o que tem data marcada.** Resultado de empresa, reunião do Fed e do Copom,
divulgação de inflação e de emprego, desbloqueio de token, vencimento de opção e de futuro, fim
de lock-up de IPO, data ex-dividendo, feriado de bolsa. Isso não é previsão: é calendário. É a
camada mais valiosa e a mais barata, e é a que não existe hoje no projeto.

**2. Posicionamento — onde está o combustível.** Funding, open interest, razão de posição de
top trader, razão de volume de taker. Já existem 949.886 linhas disso, de 5 em 5 minutos, há
3 anos. Posicionamento não diz a direção; diz o tamanho do estrago possível se o mercado for
para o lado errado do consenso.

**3. Distribuição histórica condicionada — a "projeção" honesta.** Para cada tipo de evento e
cada ativo: quando isso aconteceu antes, o que o preço fez. Não "vai subir 5%", e sim:

> Decisão do Fed · BTC · n=11 reuniões
> mediana do movimento de D-1 para D+1: −0,8%
> faixa de 80%: −6,2% a +5,4%
> fechou positivo em 5 de 11

Isso é uma distribuição empírica com `n` à vista, e é a única forma de projeção que não é
invenção. É o que transforma a agenda em decisão: saber que a SOL destrava 1,2% do supply em
3 dias vale pouco; saber que nos 14 desbloqueios anteriores a mediana foi −2,1% com cauda até
−9% vale a posição.

**Regra de honestidade, obrigatória no código:** distribuição com `n < 8` não é exibida como
número. Mostra `histórico insuficiente (n=3)`. E a janela de cálculo de qualquer evento passado
que contenha uma descontinuidade da `SPEC.md` §7.1 é descartada da amostra.

---

## §3 Fontes

### 3.1 Já verificadas e em uso

| Domínio | Fonte | Endpoint | Chave |
|---|---|---|---|
| Velas cripto | Binance REST | `/api/v3/klines` | não |
| OI + posicionamento histórico | Binance Vision | dumps diários em `data/futures/um/daily/metrics/` | não |
| OI recente (30d) | Binance REST | `/futures/data/openInterestHist` | não |
| Funding | Binance fapi | `/fapi/v1/fundingRate` | não |
| Barras diárias US | Alpaca v2 | `/v2/stocks/bars` com `feed=sip` | sim, já no `.env` |
| Universo negociável | Alpaca | `/v2/assets` | sim |
| Notícia de empresa US | Alpaca | `/v1beta1/news` | sim |
| Eventos societários US | Alpaca | `/v1/corporate-actions` | sim |
| Barras diárias B3 | brapi | `/api/quote/{ticker}` | sim, já no `.env` |

### 3.2 Verificadas nesta rodada, sem chave

| Domínio | Fonte | Endpoint | Formato confirmado |
|---|---|---|---|
| Calendário macro global | ForexFactory (mirror faireconomy) | `https://nfs.faireconomy.media/ff_calendar_thisweek.json` | lista JSON com `title`, `country`, `date` (ISO com offset), `impact` (High/Medium/Low/Holiday), `forecast`, `previous`. Amostra real trouxe `Core CPI m/m, USD, 2026-10-14T08:30:00-04:00, High, forecast 0.2%, previous 0.3%` e falas de membros do FOMC |
| Notícia Brasil | RSS InfoMoney, Money Times, Seu Dinheiro | feeds RSS | 10 itens cada, com `pubDate` |
| Notícia cripto | RSS CoinDesk, Decrypt | feeds RSS | 25 e 39 itens, com `pubDate` |
| Selic e séries BCB | BCB SGS | `https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados?formato=json` | JSON `data`/`valor` |

### 3.3 A verificar na Fase 0 — comandos exatos

Estes quatro não foram confirmados por rede restrita e **precisam ser testados na máquina do
operador**. O resultado de cada um vai para `docs/FONTES.md`.

```bash
# 1. Projeções Focus do Banco Central (IPCA, Selic, PIB, câmbio esperados pelo mercado)
curl -s -w "\nhttp=%{http_code}\n" \
 "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais?\$top=3&\$format=json" | head -40

# 2. Calendário de resultados das ações US (precisa da chave do §0 item 1)
set -a && . ./.env && set +a && curl -s -w "\nhttp=%{http_code}\n" \
 "https://www.alphavantage.co/query?function=EARNINGS_CALENDAR&horizon=3month&apikey=$ALPHAVANTAGE_API_KEY" | head -5

# 3. Desbloqueio de token (emissões)
curl -s -w "\nhttp=%{http_code}\n" "https://api.llama.fi/emissions" -o /tmp/dl.json; head -c 600 /tmp/dl.json

# 4. Séries macro US (precisa da chave do §0 item 2) — CPI americano
set -a && . ./.env && set +a && curl -s -w "\nhttp=%{http_code}\n" \
 "https://api.stlouisfed.org/fred/series/observations?series_id=CPIAUCSL&api_key=$FRED_API_KEY&file_type=json&limit=3&sort_order=desc"
```

Para cada um: se vier HTTP 200 com o conteúdo esperado, implementar o coletor na Fase 2. Se vier
erro, registrar em `docs/FONTES.md` com o erro literal e seguir. **Não procurar substituto pago,
não raspar HTML de site que bloqueia robô.**

### 3.4 Fora do projeto, com motivo

- **Pré-mercado e after-hours de ações US.** O plano gratuito da Alpaca responde 403 em dado SIP
  recente, e o feed `iex` registra de 2% a 6% do volume real — em pré-mercado isso é pior ainda.
  Dado de pré-mercado confiável é pago. Fica fora. Cripto não tem esse problema: é 24 horas e o
  dado da Binance é ao vivo e gratuito.
- **Stooq.** Passou a exigir prova de trabalho em navegador. Contornar seria burlar proteção
  antirrobô.
- **Coinglass heatmap de liquidação, Polygon.io, qualquer API de sentimento.** Motivos na
  `SPEC.md` §5.
- **Projeção de preço de qualquer fornecedor.** Ver §2.

### 3.5 Orçamento de requisição — obrigatório

Duas fontes têm teto mensal e um loop sem supervisão queima teto em uma noite:

| Fonte | Teto | Consumo planejado |
|---|---|---|
| brapi (B3) | 15.000 req/mês, 1 ticker por chamada | fechamento diário de 600 tickers daria ~12.600/mês, **é demais**. Ver decisão abaixo |
| Alpha Vantage | 25 req/dia | 1 chamada/dia pega 3 meses de calendário inteiro. Folga enorme |

**Decisão para a brapi:** fechamento completo dos 600 tickers roda **duas vezes por semana**
(terça e sexta, após o fechamento) = ~4.800/mês. Uma watchlist de 15 tickers (maior liquidez
mais os marcados pelo operador) roda de 30 em 30 minutos durante o pregão = ~4.400/mês.
Total ~9.200 de 15.000, com folga para refazer o que falhar.

Isso vira tabela, não comentário:

```
api_quota   source, period (date, primeiro dia do mês), used int, budget int, updated_at
```

O cliente HTTP incrementa `used` em cada chamada, na mesma transação da escrita do dado. Acima
de 80% do teto, só a coleta de fechamento passa; o resto recusa com log. Acima de 95%, recusa
tudo e manda alerta. Teste obrigatório: coletor acima do teto não faz chamada nenhuma.

---

## §4 Modelo de dados novo

Acrescenta ao esquema da `SPEC.md` §7. Nada existente é alterado.

```sql
-- Evento com data conhecida. O coração da camada de antecipação.
scheduled_event
  id                bigserial     PK
  source            varchar(32)   NOT NULL   -- forexfactory|alphavantage|defillama|bcb|fred|alpaca|manual
  kind              event_kind    NOT NULL
  scope             event_scope   NOT NULL   -- global|us|br|crypto
  instrument_id     int           NULL  FK instrument ON DELETE CASCADE
  title             text          NOT NULL
  starts_at         timestamptz   NOT NULL   -- sempre UTC
  has_time          boolean       NOT NULL   -- false quando a fonte só publica a data
  date_is_estimated boolean       NOT NULL DEFAULT false  -- data de resultado ainda não confirmada
  impact            event_impact  NOT NULL   -- high|medium|low
  expected          numeric(28,8) NULL       -- consenso publicado pela fonte
  previous          numeric(28,8) NULL
  actual            numeric(28,8) NULL       -- preenchido depois que sai
  unit              varchar(16)   NULL       -- '%'|'bps'|'USD'|'tokens'|'pct_supply'
  payload           jsonb         NOT NULL   -- resposta crua, para auditoria
  identity_key      varchar(64)   NOT NULL UNIQUE
  first_seen_at     timestamptz   NOT NULL DEFAULT now()
  updated_at        timestamptz   NOT NULL DEFAULT now()

  INDEX (starts_at)                    -- varredura "o que vem aí"
  INDEX (instrument_id, starts_at)     -- agenda de um ativo
  INDEX (scope, impact, starts_at)     -- página por mercado

event_kind  = rate_decision | inflation_print | jobs_print | gdp_print | activity_print
            | earnings | token_unlock | options_expiry | futures_expiry | lockup_expiry
            | dividend_ex | auction | central_bank_speech | holiday | other
event_scope = global | us | br | crypto
event_impact= high | medium | low
```

**`identity_key` é a parte delicada e já tem um modo de falha conhecido.** Data de evento muda:
empresa adia resultado, o BCB remarca reunião. Se a data entrar na chave, cada adiamento cria
uma linha nova e a agenda duplica. Então:

- resultado de empresa: `sha256('earnings|' || instrument_id || '|' || fiscal_period)`
- evento de calendário macro: `sha256(source || '|' || kind || '|' || normalize(title) || '|' || date_trunc('month', starts_at))`
- desbloqueio de token: `sha256('unlock|' || instrument_id || '|' || date(starts_at))`

`normalize(title)`: minúsculas, sem acento, espaço colapsado. Os 32 primeiros caracteres do hex.

A escrita é `ON CONFLICT (identity_key) DO UPDATE` em `starts_at`, `expected`, `previous`,
`actual`, `impact`, `date_is_estimated`, `payload`, `updated_at`, cada um com
`COALESCE(EXCLUDED.x, scheduled_event.x)` — fonte que não publica um campo não apaga o que outra
já preencheu. Mudança de `starts_at` é logada em nível `info` com os dois valores.

```sql
-- Série macro (Selic, IPCA, CPI, desemprego, curva de juros).
macro_series    code varchar(32) PK, source varchar(32), name text, unit varchar(16), scope event_scope
macro_point     code FK, ref_date date, value numeric(28,8), PK (code, ref_date)

-- Projeção de mercado (Focus do BCB). É expectativa, não fato: tabela separada de propósito.
expectation     code varchar(32), horizon_year int, median numeric(28,8), mean numeric(28,8),
                stdev numeric(28,8), n_respondents int, published_on date,
                PK (code, horizon_year, published_on)

-- Distribuição histórica condicionada. Recalculada toda noite, nunca escrita à mão.
event_impact_stat
  kind event_kind, instrument_id int, horizon varchar(4),   -- d1|d3|w1
  n int, median_signed numeric, median_abs numeric, p10 numeric, p90 numeric,
  pct_positive numeric, window_start date, window_end date, computed_at timestamptz,
  PK (kind, instrument_id, horizon)

-- Tema do mundo: agrupa notícia e liga a ativos que se moveram junto.
news_theme        id bigserial PK, label text, first_seen_at timestamptz, last_seen_at timestamptz
news_theme_item   theme_id FK, catalyst_id FK, PK (theme_id, catalyst_id)
news_theme_asset  theme_id FK, instrument_id FK, move_pct numeric, PK (theme_id, instrument_id)

-- Teto de requisição por fonte (§3.5).
api_quota       source varchar(32), period date, used int, budget int, updated_at timestamptz,
                PK (source, period)
```

**`event_impact_stat` — como é calculada.** Para cada evento passado com `actual` conhecido ou
data já vencida, pega o fechamento em D-1 e em D+1 (ou D+3, ou D+5 para `w1`) do ativo, ignora a
amostra se a janela contiver descontinuidade da `SPEC.md` §7.1, e agrega. Para evento de escopo
global sem `instrument_id` (Fed, CPI), calcula contra uma lista de ativos de referência — BTC,
ETH, SOL, SPY, QQQ, IWM, e no Brasil IBOV e USDBRL — definida em uma tabela, não no código.

**Agrupamento de tema (`news_theme`)** — primeira versão deliberadamente simples e explicável:
normaliza a manchete, extrai termos, agrupa por sobreposição de termos acima de um limiar em
janela de 48h, rotula o tema pelos três termos mais frequentes. Sem modelo de linguagem, sem
embedding, sem serviço externo. Se mostrar que não serve, melhora depois com dado na mão. Um
tema só aparece na tela com no mínimo 4 notícias de pelo menos 2 veículos diferentes.

---

## §5 A interface

Esta seção é especificação, não sugestão. É para ser implementada como está escrita.

### 5.1 Decisão de stack — altera a `SPEC.md` §6

A `SPEC.md` previa Next.js 15 + Tailwind. **Está alterado: a tela é renderizada no servidor pelo
FastAPI com Jinja2 e um único CSS escrito à mão.**

Por quê, e por que isso é melhor e não um atalho:

- Um processo, um comando (`uv run guardrail-web`). Nada de `node_modules`, nada de build, nada
  de segundo runtime instalado na máquina — o que também respeita a regra de não mexer no
  ambiente.
- Jinja2 escapa por padrão. O vetor de XSS armazenado da `SPEC.md` §16 — manchete de API externa
  renderizada na tela — morre na configuração, não na disciplina de quem escreve o template.
- Sem hidratação, sem estado no cliente, sem tela que pisca e troca de conteúdo. A página chega
  pronta em uma requisição.
- Gráfico: sparkline em SVG gerado no servidor, sem biblioteca. 30 pontos de fechamento viram
  um `<polyline>`. Nenhum JavaScript é necessário para a tela funcionar.
- O único JavaScript da página são dez linhas: recarregar a cada 60 segundos e lembrar qual
  aba estava aberta. Se o JS falhar, a página continua inteira.

Custo real dessa escolha: filtro interativo e ordenação por clique não existem de graça. Para
uma tela de um usuário que abre, bate o olho e decide, isso não é perda.

### 5.2 Regras visuais

**Proibido, sem exceção:** fundo escuro, modo noturno, neon, gradiente, vidro fosco
(*glassmorphism*), sombra colorida, roxo sobre preto, fonte de internet sem fallback, emoji,
animação, transição, ícone decorativo, canto muito arredondado, texto cinza claro sobre fundo
claro.

**Obrigatório:** fundo claro de papel, texto quase preto, duas cores de acento no máximo além do
verde e vermelho de variação, número com largura fixa (tabular), hierarquia feita por tamanho e
peso — nunca por cor.

Fichas de cor, em `web/static/app.css`, como variáveis. Nenhuma cor escrita solta no template:

```css
:root {
  color-scheme: light;

  --paper:     #FAFAF8;  /* fundo da página, branco quente */
  --card:      #FFFFFF;
  --line:      #E4E1DA;  /* borda de card e divisória */
  --line-soft: #EFEDE7;
  --ink:       #16140F;  /* texto principal */
  --ink-soft:  #5B564C;  /* rótulo, legenda, idade do dado */
  --up:        #0F6B3C;  /* verde escuro, legível, não neon */
  --down:      #A32220;  /* vermelho tijolo */
  --accent:    #1A4B96;  /* azul escuro: link, marcador de evento */
  --warn-bg:   #FFF4D9;  /* faixa de atenção: alto impacto, dado velho */
  --warn-ink:  #7A5B0B;
  --alert-bg:  #FDE8E6;  /* faixa de erro: fonte falhou */
  --alert-ink: #8E1B19;

  --sans: "IBM Plex Sans", -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace;

  --radius: 6px;
  --shadow: 0 1px 2px rgba(20,18,15,.06);
}
body {
  background: var(--paper);
  color: var(--ink);
  font: 15px/1.55 var(--sans);
  font-variant-numeric: tabular-nums;
  margin: 0;
}
.num { font-family: var(--mono); font-variant-numeric: tabular-nums; font-feature-settings: "tnum"; }
```

Tamanhos: corpo 15px, rótulo 11px em maiúscula com `letter-spacing: .06em`, número principal do
card 25px peso 600, título de página 22px peso 600. Largura máxima do conteúdo 1400px, centrado.
Grade de cards: `repeat(auto-fill, minmax(330px, 1fr))`, intervalo de 14px. Sempre pelo menos
16px de respiro nas laterais, e nenhuma rolagem horizontal na página.

### 5.3 Cabeçalho, igual em toda página

Uma faixa fina, fixa no topo, com:

1. **Navegação** — sete links, o da página atual em negrito com sublinhado grosso. Sem menu
   escondido, sem ícone de hambúrguer.
2. **Relógio dos mercados** — `B3 aberta · NYSE fecha em 2h14 · cripto 24h`, calculado do fuso e
   do calendário de feriados (`event_kind = holiday`).
3. **Saúde das fontes** — um ponto por fonte, verde quando fresca, âmbar quando atrasada,
   vermelho quando falhou, com a hora da última coleta no `title`. Lido de `source_health`.
   É assim que se sabe, em meio segundo, se a tela está mostrando a realidade.
4. **Risco** — `livre US$ X · risco aberto Y% de 4% · posições novas no mês N de 6`, da
   `SPEC.md` §8. Fica no cabeçalho de propósito: o limite tem que estar à vista em toda tela.

### 5.4 Páginas

**`/` — Hoje.** A página de abrir de manhã. De cima para baixo:

1. **Faixa "próximas 72 horas"** — linha do tempo horizontal com os eventos de alto impacto
   posicionados por hora, cada um um marcador com rótulo curto. Uma olhada e se sabe se hoje
   tem Fed, resultado de empresa grande ou desbloqueio grande.
2. **Cards de evento** dos próximos 7 dias, alto impacto primeiro, com contagem regressiva.
3. **Cards de tema do mundo** — os 3 temas mais ativos das últimas 24h com os ativos que se
   moveram junto.
4. **Movimento anormal de hoje** — até 12 cards de ativo, de qualquer mercado, ordenados por
   volume relativo, com liquidez mínima aplicada na data (US$ 20 M nos EUA, R$ 5 M no Brasil).

**`/cripto`, `/eua`, `/brasil`** — mesma estrutura, um mercado cada:
- agenda do mercado (próximos 14 dias)
- cards de ativo com movimento anormal
- cards de ativo com evento marcado nos próximos 7 dias
- no `/cripto`, mais um bloco de posicionamento: funding e variação de OI em 24h de BTC, ETH, SOL
- no `/brasil`, mais um bloco com Selic, IPCA e as projeções Focus

**`/macro`** — Fed, Copom, inflação, emprego, juros. Para cada: data do próximo, o que o mercado
espera (`expected` da fonte, ou mediana do Focus no Brasil), o valor anterior, e a distribuição
histórica do impacto nos ativos de referência.

**`/mundo`** — a página de ativo se movendo por causa do que acontece no mundo. Tema por tema,
com as manchetes e os ativos ligados. Tema com menos de 4 notícias de 2 veículos não aparece.

**`/agenda`** — calendário completo, 90 dias, agrupado por dia, com filtro por mercado e por
impacto feito em `<a href>` com parâmetro de query, não em JavaScript.

**`/radar`** — posições, capital travado, orçamento de risco, data de revisão vencida em
destaque no topo. É a parte de disciplina da `SPEC.md` §8 e §9.

### 5.5 Anatomia dos cards

**Card de ativo** — o mais importante da tela. Cinco blocos, divisória fina entre eles, sempre na
mesma ordem, para que o olho aprenda o lugar de cada coisa:

```
┌──────────────────────────────────────────────────┐
│ SOL                                  ▲ +4,2%     │  símbolo 18px/600 · variação do dia à direita
│ Solana · Binance                                 │  nome e praça 12px, --ink-soft
│ US$ 122,40            ╱╲╱──╲╱╲      30 dias      │  preço 25px/600 + sparkline SVG
│ ────────────────────────────────────────────────  │
│ PRÓXIMO EVENTO                                   │  rótulo 11px maiúscula
│ Desbloqueio · 0,9% do supply        em 3 dias    │  contagem regressiva em chip
│ ────────────────────────────────────────────────  │
│ POSICIONAMENTO                                   │
│ Funding −0,012%          OI 24h −8,4%            │
│ Alavancagem comprada saindo                      │  leitura em português, uma linha
│ ────────────────────────────────────────────────  │
│ QUANDO ISSO ACONTECEU ANTES              n=14    │
│ mediana −2,1%     faixa 80%  −9,0% a +6,2%       │
│ ────────────────────────────────────────────────  │
│ "Solana ETF decision delayed"                    │
│ CoinDesk · há 2h                                 │
└──────────────────────────────────────────────────┘
```

Regras do card:
- Variação do dia é o único lugar com cor forte. Seta mais sinal, nunca só cor — quem não
  distingue verde de vermelho tem que conseguir ler.
- Bloco sem dado não desaparece: vira `sem dado · fonte X falhou às 14:32` em `--alert-ink`.
- `n < 8` no bloco histórico imprime `histórico insuficiente (n=3)`.
- Volume relativo acima de 3x coloca uma faixa `--warn-bg` no topo do card com
  `volume 4,2x a média de 20 dias`.
- Dado de preço mais velho que o esperado para a praça imprime `fechamento de 13/10` em vez de
  fingir que é de agora.

**Card de evento:**

```
┌──────────────────────────────────────────────────┐
│ ALTO IMPACTO                quarta 28/10 · 15:00 │  faixa --warn-bg quando alto impacto
│ Decisão de juros — Fed (FOMC)         em 14 dias │
│ Esperado manter 4,00%        Anterior 4,00%      │
│ Afeta  BTC · ETH · SPY · QQQ · IBOV · USDBRL     │
│ Nas 11 reuniões anteriores, BTC: mediana ±3,4%   │
└──────────────────────────────────────────────────┘
```

**Card de tema:**

```
┌──────────────────────────────────────────────────┐
│ TEMA  tarifa · aço · china          18 notícias  │
│ Movendo junto                                    │
│   VALE3 +3,1%   CSNA3 +4,8%   USDBRL −1,2%       │
│ "Governo chinês amplia cota..." — InfoMoney · 40min │
│ "Siderúrgicas sobem..." — Money Times · há 1h    │
│ + 16 notícias                                    │
└──────────────────────────────────────────────────┘
```

### 5.6 O que a tela nunca faz

- Não mostra tabela de dezenas de linhas de número. Foi o erro da primeira versão: não acrescenta
  nada ao TradingView.
- Não pontua ativo com nota de 0 a 100. Nota inventada parece objetiva e esconde a regra.
- Não diz comprar nem vender. Mostra o que tem data marcada, onde está o posicionamento, e o que
  aconteceu antes.
- Não esconde falha. Fonte quebrada aparece no cabeçalho e no card.

---

## §6 Fases

Cada fase tem critério de saída verificável por comando. Commit ao cumprir. Não começar a
próxima antes.

### Fase 0 — Verificação e limpeza do que está pendente

1. Rodar o portão de qualidade (§1 regra 6) no estado atual e deixar tudo verde.
2. `rss_news.py` já está escrito mas nunca foi conferido: rodar, testar, commitar ou corrigir.
3. Rodar os quatro comandos da §3.3 e escrever `docs/FONTES.md` com o resultado literal de cada.
4. Criar `docs/PROGRESSO.md`.
5. Criar a tabela `api_quota` e o contador no cliente HTTP, com teste de recusa acima do teto.

**Saída:** portão verde, `docs/FONTES.md` com as quatro fontes classificadas como disponível ou
indisponível com o erro, `api_quota` migrada e testada.

### Fase 1 — Esquema da antecipação

Migrations de `scheduled_event`, `macro_series`, `macro_point`, `expectation`,
`event_impact_stat`, `news_theme*`, com os três enums, os índices da §4 e os CHECK escritos à mão.

**Saída:** `alembic upgrade head` e `alembic downgrade -1` rodam limpos nos dois sentidos; teste
de schema cobrindo unicidade de `identity_key` e rejeição de `impact` inválido.

### Fase 2 — Coletores de evento

Um por vez, cada um com os quatro testes da §1 regra 8, entrada em `source_health` e respeito ao
`api_quota`:

1. **ForexFactory** (`nfs.faireconomy.media/ff_calendar_thisweek.json`) — verificado. Mapear
   `impact` para o enum, `country` para `scope`, casar `title` com `event_kind` por tabela de
   padrões no banco, não por `if` no código. Roda de 6 em 6 horas.
2. **Alpha Vantage `EARNINGS_CALENDAR`** — se a Fase 0 confirmou. `horizon=3month`, uma chamada
   por dia, resposta CSV. `date_is_estimated = true` quando a fonte não confirma a data.
3. **DefiLlama emissões** — se a Fase 0 confirmou. Só os tokens que existem em `instrument`.
   Converter o montante para percentual do supply circulante; sem supply, gravar `NULL` e não
   exibir o percentual.
4. **BCB** — Selic e IPCA por SGS (verificado) e Focus por Olinda (se a Fase 0 confirmou), mais
   as datas de reunião do Copom.
5. **FRED** — se a Fase 0 confirmou. CPI, desemprego, fed funds, curva 10y-2y.
6. **Vencimento de opção e de futuro** — gerado por regra de calendário, não buscado: terceira
   sexta do mês para opção de ação US, último dia útil para futuro. Vira `scheduled_event` com
   `source = 'manual'`.
7. **Feriado de bolsa** — do `/v2/calendar` da Alpaca para os EUA; B3 por tabela versionada no
   repositório, que é fato público e estável.

**Saída:** `scheduled_event` com no mínimo 60 dias de eventos futuros povoados de pelo menos três
fontes distintas; rodar duas vezes seguidas insere zero linha nova e não duplica nada; toda fonte
indisponível registrada em `docs/FONTES.md` e com o coletor ausente, não meio feito.

### Fase 3 — Distribuição histórica

`guardrail.analysis.event_impact`: calcula `event_impact_stat` de `scheduled_event` passado mais
`candle`, ignorando janela com descontinuidade da `SPEC.md` §7.1, para `d1`, `d3` e `w1`.

Teste obrigatório, de valor fixo calculado à mão: um conjunto pequeno de eventos e velas
construído no teste, com a mediana, p10, p90 e `pct_positive` conferidos por cálculo manual no
próprio teste. Essa é a função que vai para a tela como "projeção"; ela não pode estar errada.

**Saída:** `event_impact_stat` povoada; `n < 8` marcado; teste de valor fixo passando; a
recomputação é idempotente.

### Fase 4 — A tela

1. `web/templates/` com `base.html`, `_card_asset.html`, `_card_event.html`, `_card_theme.html`,
   e uma página por rota da §5.4. Jinja2 com `autoescape=True`.
2. `web/static/app.css` com as fichas da §5.2. Nenhuma cor fora das variáveis.
3. `web/sparkline.py` — 30 fechamentos viram `<polyline>`. Sem biblioteca. Teste de valor fixo.
4. `web/queries.py` — uma função por bloco, cada uma com teto de linhas e liquidez mínima
   aplicada **na data do dado**, não hoje.
5. `web/health.py` — o painel de fontes do cabeçalho, de `source_health`.
6. Bind em `127.0.0.1:8787`. Comando `uv run guardrail-web`.

**Saída:** as sete rotas respondem 200 com o banco cheio **e** com o banco vazio — no vazio, cada
bloco imprime o aviso da §1 regra 20, nenhuma página quebra e nenhuma renderiza bloco mudo.
Teste de snapshot de HTML para o card de ativo nos três estados: completo, sem evento, fonte
falhada. Teste de escape: manchete com `<script>` e com `{{ }}` sai como texto.

### Fase 5 — Agendamento

`apscheduler` dentro de um processo, com as cadências: ForexFactory 6h, notícia 30min, Alpha
Vantage 1x/dia, DefiLlama 1x/dia, BCB e FRED 1x/dia, fechamento de cripto por timeframe,
fechamento US após o pregão, B3 conforme a §3.5, `event_impact_stat` toda noite, auditoria de
completude toda noite.

Cada tarefa grava em `source_health` e respeita `api_quota`. Falha não derruba o processo: é
registrada, entra em backoff exponencial com jitter, e abre circuito depois de N falhas seguidas.

**Saída:** o processo roda 24 horas sem vazar memória nem estourar teto; matar e reiniciar não
duplica dado; `source_health` reflete a realidade.

### Fase 6 — Telegram

Quatro alertas, e só estes quatro:
1. evento de alto impacto a 24h e a 1h de acontecer, nos ativos do operador e nos de referência
2. volume relativo acima de 3x em ativo da watchlist
3. data de revisão de posição vencida (`SPEC.md` §8)
4. fonte quebrada há mais de 6 horas

Token no `.env`. Nada de SDK, `httpx` direto. Alerta é de leitura: o bot não recebe comando, não
executa nada, e não tem webhook — só envia. Isso remove a superfície de ataque inteira.

**Saída:** os quatro alertas disparam em teste com a API mockada; nenhum segredo em log.

### Fase 7 — Medição

Só depois de a tela estar de pé e com dado correndo. Com `scheduled_event` povoado, testar se
evento marcado melhora a expectativa do `equity_volume_breakout` e do `crypto_deleverage_flush`
pelo portão da `SPEC.md` §11: 40 disparos mínimos, meia-metade, e linha de base de entrada
aleatória com o mesmo tempo de permanência.

**Isto fica no fim de propósito.** A medição anterior não achou regra automatizável nestes dados,
e a camada de antecipação é útil independentemente disso — ela mostra o que vem, e a decisão é
do operador. Se a medição com evento também não passar o portão, o resultado é que o sistema
continua sendo tela de contexto e disciplina, e nenhuma regra é habilitada. Esse é um resultado
aceitável e deve ser escrito em `docs/PROGRESSO.md` com os números, não contornado.

---

## §7 Análise de risco e segurança da camada nova

A superfície de ataque cresceu em três pontos. Os três têm mitigação obrigatória.

**1. Conteúdo de terceiro renderizado na tela.** Manchete de RSS, título de evento e nome de
ativo vêm de fora. Renderizados crus, são XSS armazenado numa página que mostra capital.
Mitigação: Jinja2 com `autoescape=True` (padrão), proibido `|safe` em campo ingerido, nenhum
`innerHTML`. URL de notícia só vira `href` depois de validar esquema `http`/`https` e passa com
`rel="noopener noreferrer"`. Título truncado em 300 caracteres na ingestão. Teste adversarial
obrigatório: manchete com `<script>`, com `javascript:`, e com sintaxe de template.

**2. Chave em URL.** Alpha Vantage e FRED levam a chave na query string. Riscos: a chave vaza em
log de requisição, em mensagem de exceção do `httpx` e em fixture de teste gravada que vai para
um repositório **público**. Mitigação: `SecretStr` no settings; filtro de redação no `structlog`
que troca o valor de qualquer parâmetro chamado `apikey`/`api_key` por `***`; `respx` gravando
fixture só depois da redação; e um padrão novo no scanner de segredos do `pre-commit` para o
formato dessas duas chaves. Conferir o diff antes do commit que adiciona fixture.

**3. Tela com mais informação sensível.** Ela agora mostra capital livre, risco aberto, posição e
watchlist. Isso é um documento de alvo. Mitigação: `127.0.0.1` apenas, nunca `0.0.0.0`, nunca
atrás de túnel público, nenhum CORS liberado, nenhuma rota de escrita. Se um dia precisar acesso
remoto, Tailscale — não porta aberta.

**Modo de falha mais perigoso desta camada, e é o calado:** um coletor de evento morre, a agenda
congela, e a tela mostra "nenhum evento nos próximos 7 dias" num dia de Fed. Isso é pior que
erro, porque parece informação. Mitigação em três camadas: `source_health` no cabeçalho de toda
página; frescor conferido por fonte antes de renderizar, com aviso explícito no bloco; e alerta
no Telegram com fonte parada há mais de 6 horas. Ausência de dado nunca é exibida como ausência
de evento.

---

## §8 Como testar

Além do que a `SPEC.md` §15 já manda:

- **Valor fixo onde o número importa.** `event_impact_stat` e o sparkline são testados contra
  valores calculados à mão dentro do próprio teste. Função estatística que vai para a tela sem
  teste de valor fixo é confiança em código não conferido.
- **Propriedade (`hypothesis`) na `identity_key`.** Para qualquer evento, gerar a chave duas
  vezes dá o mesmo resultado; mudar só a data de um evento de calendário macro dentro do mesmo
  mês **não** muda a chave; mudar o `instrument` muda.
- **Idempotência como teste, não como intenção.** Todo coletor roda duas vezes no mesmo teste e
  a segunda insere zero linha.
- **Banco vazio como caso de borda de primeira classe.** Cada rota tem teste com o banco limpo.
  É o estado em que a tela mais mente.
- **Payload adversarial em evento:** data no passado distante, data a 10 anos, `impact`
  desconhecido, `expected` como texto, campo faltando, JSON truncado.
- **Teto de requisição:** coletor acima do teto não faz chamada nenhuma. Verificado contando
  chamadas no `respx`.

---

## §9 O que este plano não entrega

Escrito aqui para não haver dúvida depois.

- **Previsão de preço.** Não existe e não vai existir aqui.
- **Pré-mercado de ações americanas.** Dado confiável é pago (§3.4).
- **B3 ao vivo e completa.** O teto gratuito da brapi não permite: é fechamento duas vezes por
  semana mais watchlist de 15 tickers intradiária (§3.5).
- **Fluxo de liquidação de cripto.** A Binance parou de publicar o histórico e o stream ao vivo
  exige processo 24h num ambiente sem systemd (`SPEC.md` §5).
- **Fundos.** ETF americano já entra pelo universo da Alpaca e aparece normalmente. **Fundo
  brasileiro — FII e fundo de investimento — fica fora desta versão**: FII dá para pegar pela
  brapi, mas consome o mesmo teto de 15.000 que as ações já comprometem, e fundo de investimento
  comum tem cota divulgada com atraso de dias pela CVM, o que não serve para antecipar nada. Se
  quiser FII, o caminho é trocar: tirar tickers de ação da watchlist para abrir espaço.
- **Garantia de ganho.** Com R$ 2.000 a R$ 8.000 livres e 1,5% de risco por trade, um mês bom dá
  algumas centenas de reais. A ferramenta mostra o que vem e impede o erro de execução que já
  custou caro. Ela não resolve renda instável, e este plano não finge que resolve.

---

## §10 Ordem de valor, se faltar tempo

Se alguma coisa tiver que ficar para depois, esta é a ordem decrescente de utilidade real:

1. **Agenda** (Fases 1 e 2) — é o que não existe hoje e o mais valioso. Saber que amanhã tem CPI
   e que uma empresa grande reporta na quarta muda decisão hoje.
2. **A tela** (Fase 4) — sem ela, a agenda fica no banco e não na cabeça do operador.
3. **Distribuição histórica** (Fase 3) — transforma a agenda em tamanho de posição.
4. **Telegram** (Fase 6) — importa quando o operador não está na frente do computador.
5. **Medição de regra** (Fase 7) — a menos urgente, e a que já falhou uma vez.

---

## §11 Convivência com a `SPEC.md`

A `SPEC.md` continua sendo a fonte de verdade para: problema, princípio de três camadas, motor de
risco (§8), capital travado (§9), universo de ações (§9.1), portão de backtest (§11), regra de
sessão de banco (§12.1), descontinuidades (§7.1), segurança (§16) e tributação (§17).

Este plano acrescenta a camada de antecipação e a interface, e **altera dois pontos**:

| `SPEC.md` | Dizia | Passa a ser |
|---|---|---|
| §6 Frontend | Next.js 15 + TypeScript + Tailwind | FastAPI + Jinja2 + um CSS escrito à mão, SVG no servidor, sem Node (§5.1) |
| §14 Fase 4 | "Uma tela, sem visão geral de mercado" | Sete páginas, cards por mercado, agenda e temas do mundo (§5.4) |

A segunda alteração merece uma linha de justificativa, porque contraria uma decisão deliberada da
spec. A spec evitava visão geral de mercado para não aumentar o número de gatilhos de entrada
apresentados ao operador — o problema primário é execução, não informação. A camada de
antecipação não é lista de gatilho: é contexto com data marcada, que diminui trade por impulso
em vez de aumentar. O limite rígido de 5 setups por dia, o teto de 6 posições novas por mês e o
contrato obrigatório da §8 continuam valendo sem nenhuma alteração. **O cabeçalho mostra o
orçamento de risco em toda página exatamente por isso.**