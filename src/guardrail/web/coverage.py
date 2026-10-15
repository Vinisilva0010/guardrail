"""What the screen cannot know, and why. Each entry is a known gap, not a guess.

An empty block must never read as "nothing is happening". Where a source is
missing, the block that would have been fed by it says so, with the reason from
docs/FONTES.md. Remove an entry when its collector exists.
"""

from typing import Final

# Asset-bound events (earnings, token unlocks) by market.
ASSET_EVENT_GAPS: Final = {
    "crypto": "desbloqueio de token: fonte paga (DefiLlama, HTTP 402)",
    "us": "resultados de empresas: chave ALPHAVANTAGE_API_KEY ausente",
    "br": "resultados de empresas: sem coletor",
}

# Macro dates the screen cannot show, by page.
MACRO_GAPS: Final = {
    "us": "séries macro EUA (FRED): chave FRED_API_KEY ausente",
    "br": "datas do Copom: sem API acessível (docs/FONTES.md)",
}

# The ForexFactory feed covers one week. Beyond it the macro agenda is unknown,
# not empty.
MACRO_HORIZON_NOTE: Final = (
    "o calendário macro cobre só a semana corrente; "
    "além disso a ausência de evento não significa que não haja"
)
