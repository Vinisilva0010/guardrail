"""HTML rendering.

Black on white, large and bold. Organised by question rather than as one long
table: a screen that lists everything is a screen nobody reads.
"""

from datetime import UTC, datetime
from decimal import Decimal
from html import escape
from typing import TYPE_CHECKING, Final

from guardrail.web.queries import MarketPulse, Row

if TYPE_CHECKING:
    from guardrail.web.app import MarketView

REFRESH_SECONDS: Final = 60

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #fff; color: #000;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 16px; font-weight: 700; padding: 18px 22px 60px;
}
h1 { font-size: 24px; letter-spacing: 2px; }
.sub { font-size: 13px; font-weight: 500; margin: 2px 0 18px; }
.pulsebar { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 26px; }
.card { border: 3px solid #000; padding: 12px 16px; min-width: 215px; }
.card .t { font-size: 13px; letter-spacing: 1px; }
.card .v { font-size: 30px; line-height: 1.15; margin: 4px 0; }
.card .d { font-size: 13px; font-weight: 500; }
.market { font-size: 21px; letter-spacing: 2px; margin: 30px 0 4px;
          border-bottom: 4px solid #000; padding-bottom: 6px; }
.block { margin: 18px 0 0; }
.block h3 { font-size: 15px; letter-spacing: 1px; margin-bottom: 2px; }
.block .why { font-size: 12px; font-weight: 500; margin-bottom: 7px; }
table { border-collapse: collapse; width: 100%; }
th { font-size: 12px; text-align: right; padding: 5px 10px;
     border-bottom: 2px solid #000; white-space: nowrap; }
td { font-size: 16px; text-align: right; padding: 7px 10px;
     border-bottom: 1px solid #ccc; white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
tr:hover td { background: #f0f0f0; }
.up { color: #00661f; }
.down { color: #99000d; }
.tag { background: #000; color: #fff; padding: 2px 6px; font-size: 13px; }
.none { font-size: 14px; font-weight: 500; padding: 8px 0 12px; }
"""


def _pct(v: Decimal | None, casas: int = 1) -> str:
    if v is None:
        return "-"
    cls = "up" if v > 0 else "down" if v < 0 else ""
    return f'<span class="{cls}">{v * 100:+.{casas}f}%</span>'


def _price(v: Decimal, pre: str) -> str:
    return f"{pre}{v:,.2f}"


def _turn(v: Decimal | None, pre: str) -> str:
    if v is None:
        return "-"
    if v >= 1_000_000_000:
        return f"{pre}{v / 1_000_000_000:,.1f}B"
    return f"{pre}{v / 1_000_000:,.0f}M"


def _rvol(v: Decimal | None) -> str:
    if v is None:
        return "-"
    txt = f"{v:.1f}x"
    return f'<span class="tag">{txt}</span>' if v >= 3 else txt


def _card(titulo: str, valor: str, detalhe: str) -> str:
    return (
        f'<div class="card"><div class="t">{titulo}</div>'
        f'<div class="v">{valor}</div><div class="d">{detalhe}</div></div>'
    )


def _pulse_cards(p: MarketPulse, rotulo: str) -> str:
    if p.total == 0:
        return _card(rotulo, "-", "sem dados")
    subindo = round(p.rising / p.total * 100)
    return (
        _card(rotulo, f"{subindo}%", f"subindo hoje de {p.total} papeis")
        + _card(
            f"{rotulo} MEDIANA",
            f"{p.median_change * 100:+.2f}%",
            "variacao do papel do meio",
        )
        + _card(
            f"{rotulo} VOLUME",
            str(p.unusual_volume),
            "papeis com volume 3x acima da media",
        )
    )


def _crypto_cards(linhas: list[Row]) -> str:
    saida = []
    for r in linhas:
        nome = r.symbol.replace("USDT", "")
        funding = f"funding {r.funding * 100:+.4f}%" if r.funding is not None else ""
        saida.append(
            _card(
                nome,
                f"{r.chg_1d * 100:+.2f}%",
                f"{_price(r.price, '$')} &middot; {r.vs_high * 100:+.1f}% do topo 7d"
                f"{' &middot; ' + funding if funding else ''}",
            )
        )
    return "".join(saida)


def _table(linhas: list[Row], moeda: str) -> str:
    if not linhas:
        return '<p class="none">Nada aqui agora.</p>'
    corpo = "".join(
        f"<tr><td>{escape(r.symbol)}</td>"
        f"<td>{_price(r.price, moeda)}</td>"
        f"<td>{_pct(r.chg_1d)}</td>"
        f"<td>{_pct(r.chg_5d)}</td>"
        f"<td>{_rvol(r.rvol)}</td>"
        f"<td>{_pct(r.vs_high)}</td>"
        f"<td>{_turn(r.turnover, moeda)}</td>"
        f"<td>{'SIM' if r.has_news else '-'}</td></tr>"
        for r in linhas
    )
    return (
        "<table><thead><tr><th>PAPEL</th><th>PRECO</th><th>HOJE</th>"
        "<th>5 DIAS</th><th>VOLUME</th><th>DO TOPO 20D</th>"
        "<th>GIRO MEDIO</th><th>NOTICIA</th></tr></thead>"
        f"<tbody>{corpo}</tbody></table>"
    )


_BLOCOS = (
    (
        "breaking_out",
        "ROMPENDO COM VOLUME",
        "Fechou acima da maxima de 20 dias com o dobro do volume normal. "
        "Preco sozinho sobe sem significar nada; o volume diz que gente agiu.",
    ),
    (
        "turning_up",
        "CAIU E ESTA VOLTANDO",
        "Esta pelo menos 12% abaixo da maxima de 20 dias, subiu mais de 2% hoje "
        "e com volume acima do normal. As tres coisas juntas.",
    ),
    (
        "beaten_down",
        "CAIU FORTE",
        "Mais de 15% abaixo da propria maxima de 20 dias. Ainda nao virou; "
        "serve para acompanhar e esperar.",
    ),
    (
        "unusual_volume",
        "VOLUME SEM EXPLICACAO",
        "Volume 3x acima da media, sem romper maxima e sem noticia nos ultimos "
        "3 dias. Algo acontecendo que o preco ainda nao mostrou.",
    ),
)


def render_page(crypto: list[Row], mercados: list["MarketView"]) -> str:
    """Build the full page."""
    cards = _crypto_cards(crypto) + "".join(
        _pulse_cards(m.pulse, m.label.split()[0]) for m in mercados
    )

    corpo = []
    for m in mercados:
        corpo.append(f'<div class="market">{m.label}</div>')
        for atributo, titulo, porque in _BLOCOS:
            linhas: list[Row] = getattr(m, atributo)
            corpo.append(
                f'<div class="block"><h3>{titulo}</h3>'
                f'<p class="why">{porque}</p>{_table(linhas, m.currency)}</div>'
            )

    agora = datetime.now(UTC).strftime("%d/%m/%Y %H:%M UTC")
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Guardrail</title>
<style>{_CSS}</style>
</head>
<body>
<h1>GUARDRAIL</h1>
<p class="sub">{agora} &middot; recarrega sozinho a cada {REFRESH_SECONDS}s</p>
<div class="pulsebar">{cards}</div>
{"".join(corpo)}
</body>
</html>"""
