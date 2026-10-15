# Fontes — verificação da Fase 0

Registro literal do que foi testado. Data da verificação: 2026-10-15 (UTC), a partir da
máquina do operador. Nenhum substituto foi procurado para fonte que falhou (regras 19 e 22).

## Resumo

| # | Fonte | Situação | Consequência |
|---|---|---|---|
| 1 | BCB Focus (Olinda) | **Disponível** | Coletor na Fase 2, item 4 |
| 2 | Alpha Vantage `EARNINGS_CALENDAR` | **Pendente — chave ausente** | Coletor pulado até a chave existir |
| 3 | DefiLlama emissões | **Indisponível — exige plano pago** | Fora do projeto; sem coletor de desbloqueio de token |
| 4 | FRED | **Pendente — chave ausente** | Coletor pulado até a chave existir |

## 1. BCB Focus — disponível

- URL: `https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais`
- Comando: `curl -s -w "\nhttp=%{http_code}\n" "$URL?\$top=3&\$format=json"`
- Resultado: `http=200`, JSON `{"@odata.context": ..., "value": [...]}`.
- Campos por linha: `Indicador`, `IndicadorDetalhe`, `Data` (publicação), `DataReferencia`
  (ano projetado), `Media`, `Mediana`, `DesvioPadrao`, `Minimo`, `Maximo`,
  `numeroRespondentes`, `baseCalculo`.
- **Armadilha 1:** sem `$orderby` a resposta vem em ordem antiga (a primeira linha é de 2017).
  O coletor tem que pedir `$orderby=Data desc`, senão grava lixo velho como se fosse atual.
- **Armadilha 2:** filtro com `$filter=Indicador eq 'IPCA'` funciona com `%20` na URL.
  `curl -G --data-urlencode` devolveu HTTP 400 (`The types 'Edm.Boolean' and 'Edm.String'
  are not compatible`). Usar `httpx` com `params` e conferir o filtro nos testes.
- Confirmação de frescor: `$filter=Indicador eq 'IPCA'&$orderby=Data desc&$top=2` devolveu
  `Data=2026-10-09`, IPCA 2026 mediana 5,2245 (144 respondentes) e IPCA 2027 mediana 4,3000.
- Mapeia para a tabela `expectation` (§4 do plano): `median`, `mean`, `stdev`,
  `n_respondents`, `published_on`.

## 2. Alpha Vantage `EARNINGS_CALENDAR` — pendente

- Não testado: `ALPHAVANTAGE_API_KEY` não existe no `.env` (verificado só pelos nomes das
  variáveis, sem imprimir valores).
- Comando a rodar quando a chave existir: o item 2 da §3.3 do plano.
- Regra 30: pular o coletor, registrar aqui, continuar. Não é falha da fonte.

## 3. DefiLlama emissões — indisponível

- URL: `https://api.llama.fi/emissions`
- Comando: `curl -s -m 60 -w "\nhttp=%{http_code}\n" "https://api.llama.fi/emissions"`
- Resultado: `http=402`, corpo: `Upgrade to the paid API plan at https://defillama.com/subscription`
- Data: 2026-10-15.
- Decisão: o endpoint passou a exigir plano pago (regras 22 e 29). **Fora do projeto.**
  Nenhum substituto, nenhum raspador de HTML.
- Efeito no produto: o card de ativo cripto não terá "desbloqueio de token" e a agenda não
  terá `token_unlock`. Isso precisa aparecer na tela como aviso de fonte ausente, e não como
  "nenhum desbloqueio nos próximos dias" (§7 do plano: ausência de dado nunca vira ausência
  de evento).

## 4. FRED — pendente

- Não testado: `FRED_API_KEY` não existe no `.env`.
- Comando a rodar quando a chave existir: o item 4 da §3.3 do plano.
- Regra 30: pular o coletor, registrar aqui, continuar.
- Efeito: sem FRED, a página `/macro` não tem CPI, desemprego, fed funds nem curva 10y-2y
  vindos de série oficial. O calendário do ForexFactory (Fase 2, item 1) ainda traz a data e o
  consenso desses eventos.

---

# Fase 2 — achados durante a implementação

## ForexFactory (`nfs.faireconomy.media/ff_calendar_thisweek.json`) — disponível, com teto não documentado

- Verificado em 2026-10-15: `http=200`, lista JSON com `title`, `country`, `date`, `impact`,
  `forecast`, `previous`. `country` é código de moeda (`USD`, `EUR`, ...) ou `All`.
- **Limite de requisição:** respondeu `429 Too Many Requests` depois de 5 requisições minhas em
  cerca de 10 minutos (1 verificação manual e 4 execuções do coletor). Nenhum cabeçalho de
  `Retry-After` foi usado. A cadência de 6 em 6 horas da Fase 5 fica muito abaixo disso, mas o
  agendador não pode repetir em laço apertado quando receber 429.
- **Cobre uma semana só.** Não há horizonte de 60 dias nesta fonte. Isso importa para o
  critério de saída da Fase 2 (ver `PROGRESSO.md`).
- Itens `impact = Holiday` carregam hora em outro fuso (um feriado do JPY aparece às 19:00 do
  dia anterior em EDT), então o instante não serve como data de calendário. O coletor os descarta
  e os conta; feriado de bolsa tem coletor próprio (item 7).
- `forecast`/`previous` aparecem como texto (`0.2%`, `197K`, `-21.0B`) e às vezes como lixo
  (`3.80|1.6`). O que não é um número limpo vira `NULL`; o texto cru fica em `payload`.

## BCB SGS — disponível

- `https://api.bcb.gov.br/dados/serie/bcdata.sgs.{432|433}/dados?formato=json&dataInicial=dd/mm/aaaa`,
  `http=200`. 432 = Selic meta (diária), 433 = IPCA variação mensal.
- **A série 432 devolve pontos com data futura** (em 2026-10-15 os últimos seis eram de
  30/10 a 04/11/2026). Ponto datado depois de hoje não é observação: o coletor descarta e conta.
- Consulta de série diária é limitada a 10 anos por requisição; a carga inicial usa 5.

## BCB Focus (Olinda) — disponível, com armadilhas de codificação

- Ver a seção 1 acima. Além da ordenação, o servidor rejeita `+` no lugar de espaço e `%24` no
  lugar de `$` com `HTTP 400` e a mensagem enganosa `The types 'Edm.Boolean' and 'Edm.String'
  are not compatible`. O coletor monta a query à mão (`%20`, `$` literal) e há teste que trava isso.
- Indicadores coletados: IPCA, Selic, PIB Total, Câmbio (`baseCalculo = 0`).

## Calendário do Copom — indisponível (sem API acessível)

- Página oficial: `https://www.bcb.gov.br/controleinflacao/calendarioreunioescopom`
  → `http=200`, 2,8 KB: uma casca de aplicação Angular com o aviso "Essa pagina depende do
  javascript para abrir". Os dados vêm de um pacote carregado sob demanda.
- `https://www.bcb.gov.br/api/servico/sitebcb/copomcalendario` → `http=400`.
- Achar o endpoint real exigiria fazer engenharia reversa dos pacotes JS do site, sem contrato
  publicado. Não foi feito. **Nenhuma data do Copom foi escrita à mão** (regra 17).
- Efeito: `/macro` e `/brasil` mostram Selic e Focus, mas **sem data da próxima reunião**.
  A tela tem de dizer "data do Copom: fonte indisponível", nunca omitir a linha.

---

# Fase 5 — achados ao colocar as fontes no agendador

## Alpaca: "dado recente" é medido pela data de Nova York

- Verificado em 2026-10-15 02:28 UTC (22:28 em Nova York, ainda dia 14 lá):
  `GET /v2/stocks/bars?...&end=2026-10-14&feed=sip` → **HTTP 403**
  `{"message":"subscription does not permit querying recent SIP data"}`;
  com `end=2026-10-13`, `end=2026-10-12` e `end=2026-10-09` → HTTP 200.
- Consequência: "ontem" tem de ser a data de **Nova York** menos um dia. O código usava a data
  UTC, o que funcionava de manhã (horário de Brasília) e **falharia toda noite** no horário em que
  o agendamento rodaria (00:40 UTC = 20:40 em NY). Corrigido em `latest_available_day()`; o
  job de barras dos EUA passou para 05:30 UTC (01:30 em NY, já "dia seguinte").

## brapi: `/available` lista bem mais do que os 600 tickers do orçamento

- O coletor antigo `backfill_b3` percorre a lista inteira de `/available`. Rodado sem `--limit`
  pelo agendador, criou 489 instrumentos novos e gastou **1.081 requisições** (de 15.000) em
  poucos minutos, até eu interromper à mão. Detalhes e correção em `PROGRESSO.md`.
- A atualização agendada agora só visita os tickers **que já estão em `instrument`** (600), com
  teto de 700 requisições por rodada e parada limpa quando a cota recusa.

## BCB SGS: erro 502 transitório

- `GET .../bcdata.sgs.432/dados?...` respondeu `502 Bad Gateway` uma vez durante a verificação
  (2026-10-15 ~02:30 UTC); repetido minutos depois respondeu 200. Tratado como falha com
  retentativa e backoff, não como fonte indisponível.

## Notícias da Alpaca: quase tudo é nota de analista com molde fixo

- Em uma janela real de 48 h (1.278 manchetes), as palavras `price`, `target`, `maintains`
  aparecem em 44%, 44% e 38% das manchetes. São notas do tipo "X Maintains Buy on Y, Raises
  Price Target to $Z". Servem como catalisador booleano por ativo (o uso da SPEC §5), mas **não
  servem para detectar tema do mundo**, e vêm de um único veículo (`alpaca.news`).
