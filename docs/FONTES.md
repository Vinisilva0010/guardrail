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
