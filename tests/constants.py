"""Identifiers reserved for tests.

Tests run against the real database inside a rolled-back transaction. They must
never reuse a symbol or venue that a real collector writes: the unique
constraint on (venue, symbol) would reject the insert, and get_or_create would
hand back a production row whose candles the assertions would then count.
"""

TEST_SYMBOL = "TESTCOIN"
TEST_SYMBOL_ALT = "TESTCOIN2"
TEST_VENUE = "test-venue"
