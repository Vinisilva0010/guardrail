"""Risk limits (SPEC §8).

Constants, not configuration: changing one takes a commit and a review, not an
impulse at eleven at night. The risk engine that enforces them is a later phase;
the screen reads them here so the limits are in view on every page.
"""

from decimal import Decimal
from typing import Final

# Percent of free capital.
MAX_RISK_PER_TRADE_PCT: Final = Decimal("1.5")
MAX_OPEN_RISK_PCT: Final = Decimal("4")

MAX_NEW_POSITIONS_PER_MONTH: Final = 6
MAX_REVIEW_DAYS: Final = 30

COOLDOWN_AFTER_LOSSES: Final = 2
COOLDOWN_DAYS: Final = 7
