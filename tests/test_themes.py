"""Tests for headline grouping.

Worked by hand. After the stopword and length filters the five steel headlines
have these terms (accents dropped for comparison):

    S1 china amplia tarifa aco importado
    S2 tarifa aco china pressiona siderurgicas
    S3 siderurgicas sobem tarifa aco china
    S4 governo chines mantem tarifa aco              (shares 2 of 5 = 0.4: joins)
    S5 aco china decide tarifa siderurgicas reagem

Term counts over the five: aco 5, tarifa 5, china 4, siderurgicas 3, others 1.
The label takes the top three, ties broken alphabetically: aco, tarifa, china.
"""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from guardrail.analysis.themes import (
    MIN_OVERLAP,
    MIN_SHARED,
    Story,
    cluster_stories,
    recompute,
    terms,
)
from guardrail.db.models import (
    AssetClass,
    Candle,
    Catalyst,
    Instrument,
    NewsTheme,
    NewsThemeAsset,
    NewsThemeItem,
)
from guardrail.web import queries

D = Decimal
NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)

STEEL = [
    ("rss.infomoney", "China amplia tarifa sobre aço importado"),
    ("rss.moneytimes", "Tarifa do aço da China pressiona siderúrgicas"),
    ("rss.seudinheiro", "Siderúrgicas sobem com tarifa de aço na China"),
    ("rss.infomoney", "Governo chinês mantém tarifa do aço"),
    ("rss.moneytimes", "Aço: China decide tarifa e siderúrgicas reagem"),
]
OTHER = [
    ("rss.infomoney", "Petrobras anuncia dividendos extraordinários"),
    ("rss.moneytimes", "Dólar fecha em queda com dados dos EUA"),
]


def _story(i: int, outlet: str, headline: str, hours: float = 0) -> Story:
    return Story(
        outlet,
        headline,
        NOW - timedelta(hours=10) + timedelta(hours=i + hours),
        (i,),
        (1,),
    )


def _stories() -> list[Story]:
    return [_story(i, o, h) for i, (o, h) in enumerate(STEEL + OTHER)]


# --- terms --------------------------------------------------------------------------


def test_terms_drop_short_words_stopwords_and_numbers() -> None:
    assert set(terms("A queda do dólar em 2026 com os EUA")) == {
        "queda",
        "dolar",
        "eua",
    }


def test_terms_compare_without_accents_but_display_with_them() -> None:
    found = terms("Aço: China decide tarifa e siderúrgicas reagem")

    assert set(found) == {"aco", "china", "decide", "tarifa", "siderurgicas", "reagem"}
    assert found["aco"] == "aço"
    assert found["siderurgicas"] == "siderúrgicas"


def test_terms_of_an_empty_or_symbol_only_headline() -> None:
    assert terms("") == {}
    assert terms("!!! ??? 123") == {}


def test_thresholds_are_the_documented_ones() -> None:
    assert (2, D("0.4")) == (MIN_SHARED, MIN_OVERLAP)


# --- grouping ------------------------------------------------------------------------


def test_steel_headlines_form_one_theme_and_the_others_stay_apart() -> None:
    clusters = cluster_stories(_stories())

    sizes = sorted(len(c.stories) for c in clusters)
    assert sizes == [1, 1, 5]


def test_label_is_the_three_most_frequent_terms() -> None:
    steel = max(cluster_stories(_stories()), key=lambda c: len(c.stories))

    assert steel.label == "aço · tarifa · china"
    assert (steel.counts["aco"], steel.counts["tarifa"], steel.counts["china"]) == (
        5,
        5,
        4,
    )
    assert steel.counts["siderurgicas"] == 3


def test_a_story_sharing_exactly_the_threshold_joins() -> None:
    """S4 shares tarifa and aco: 2 of its 5 terms, 0.4."""
    steel = max(cluster_stories(_stories()), key=lambda c: len(c.stories))

    assert any(
        s.headline == "Governo chinês mantém tarifa do aço" for s in steel.stories
    )


def test_a_story_below_the_overlap_threshold_stays_out() -> None:
    # Shares tarifa and aco only: 2 of its 7 terms, 0.29, under the 0.4 threshold.
    extra = _story(
        20, "rss.infomoney", "Tarifa do aço: ministério anuncia revisão ampla geral"
    )

    clusters = cluster_stories([*_stories(), extra])

    steel = max(clusters, key=lambda c: len(c.stories))
    assert extra not in steel.stories


def test_one_shared_term_is_never_enough() -> None:
    extra = _story(21, "rss.infomoney", "China fecha acordo comercial")

    steel = max(cluster_stories([*_stories(), extra]), key=lambda c: len(c.stories))

    assert extra not in steel.stories


def test_the_same_subject_days_later_is_a_new_theme() -> None:
    later = _story(
        22, "rss.infomoney", "China amplia tarifa sobre aço importado", hours=72
    )

    clusters = cluster_stories([*_stories(), later])

    steel = max(clusters, key=lambda c: len(c.stories))
    assert later not in steel.stories


def test_grouping_does_not_depend_on_input_order() -> None:
    base = cluster_stories(_stories())
    shuffled = _stories()
    random.Random(7).shuffle(shuffled)

    other = cluster_stories(shuffled)

    assert sorted((c.label, len(c.stories)) for c in other) == sorted(
        (c.label, len(c.stories)) for c in base
    )


# --- stored themes ---------------------------------------------------------


def _blank(session: Session) -> None:
    session.execute(text("TRUNCATE instrument, news_theme CASCADE"))


def _instrument(session: Session, symbol: str) -> Instrument:
    instrument = Instrument(symbol=symbol, venue="b3", asset_class=AssetClass.EQUITY)
    session.add(instrument)
    session.flush()
    return instrument


def _bars(
    session: Session, instrument: Instrument, days_ago_last: int, prev: str, last: str
) -> None:
    for back, price in ((days_ago_last + 1, prev), (days_ago_last, last)):
        day = (NOW - timedelta(days=back)).date()
        session.add(
            Candle(
                instrument_id=instrument.id,
                timeframe="1d",
                ts=datetime(day.year, day.month, day.day, tzinfo=UTC),
                open=D(price),
                high=D(price),
                low=D(price),
                close=D(price),
                volume=D(1),
            )
        )
    session.flush()


def _news(
    session: Session, vale: Instrument, csna: Instrument, petr: Instrument
) -> int:
    rows = 0
    for i, (outlet, headline) in enumerate(STEEL + OTHER):
        # The first steel story names two stocks: two catalyst rows, one story.
        targets = [vale, csna] if i == 0 else [petr if i >= 5 else vale]
        for instrument in targets:
            session.add(
                Catalyst(
                    instrument_id=instrument.id,
                    ts=NOW - timedelta(hours=10) + timedelta(hours=i),
                    source=outlet,
                    headline=headline,
                    url=None,
                    dedupe_key=f"{i}-{instrument.id}".ljust(64, "x"),
                )
            )
            rows += 1
    session.flush()
    return rows


def test_recompute_stores_the_theme_its_items_and_the_assets_that_moved(
    session: Session,
) -> None:
    _blank(session)
    vale, csna, petr = (
        _instrument(session, "VALE3"),
        _instrument(session, "CSNA3"),
        _instrument(session, "PETR4"),
    )
    _bars(session, vale, 0, "100", "103.1")  # bar from today: +3.1%
    _bars(session, csna, 3, "50", "60")  # last bar predates the news: no claim
    rows = _news(session, vale, csna, petr)

    report = recompute(session, NOW)

    assert (report.stories, report.themes) == (7, 1)
    theme = session.execute(select(NewsTheme)).scalar_one()
    assert theme.label == "aço · tarifa · china"
    items = session.execute(
        select(func.count()).select_from(NewsThemeItem)
    ).scalar_one()
    assert items == 6  # five steel stories, one of them on two stocks
    assert rows == 8  # ...against all catalyst rows, including the unrelated pair
    moves = {
        r.instrument_id: r.move_pct
        for r in session.execute(select(NewsThemeAsset)).scalars()
    }
    assert moves == {vale.id: D("0.031")}


def test_recompute_is_idempotent(session: Session) -> None:
    _blank(session)
    vale, csna, petr = (
        _instrument(session, "VALE3"),
        _instrument(session, "CSNA3"),
        _instrument(session, "PETR4"),
    )
    _bars(session, vale, 0, "100", "103.1")
    _news(session, vale, csna, petr)

    recompute(session, NOW)
    recompute(session, NOW)

    assert (
        session.execute(select(func.count()).select_from(NewsTheme)).scalar_one() == 1
    )


def test_singletons_are_not_stored(session: Session) -> None:
    _blank(session)
    petr = _instrument(session, "PETR4")
    session.add(
        Catalyst(
            instrument_id=petr.id,
            ts=NOW - timedelta(hours=1),
            source="rss.infomoney",
            headline="Petrobras anuncia dividendos",
            url=None,
            dedupe_key="s" * 64,
        )
    )
    session.flush()

    report = recompute(session, NOW)

    assert (report.stories, report.themes) == (1, 0)


def test_empty_catalyst_table_gives_no_themes(session: Session) -> None:
    _blank(session)

    report = recompute(session, NOW)

    assert (report.stories, report.themes, report.assets) == (0, 0, 0)


def test_screen_counts_stories_and_outlets_not_catalyst_rows(session: Session) -> None:
    _blank(session)
    vale, csna, petr = (
        _instrument(session, "VALE3"),
        _instrument(session, "CSNA3"),
        _instrument(session, "PETR4"),
    )
    _bars(session, vale, 0, "100", "103.1")
    _news(session, vale, csna, petr)
    recompute(session, NOW)

    block = queries.themes(session, NOW, hours=24, limit=3)

    assert block.notice is None
    (theme,) = block.rows
    assert (theme.n_items, theme.n_outlets) == (5, 3)  # six catalyst rows, five stories
    assert theme.assets == (("VALE3", D("0.031")),)


# --- analyst-rating notes ----------------------------------------------------


def test_rating_note_boilerplate_and_brokers_are_not_subjects() -> None:
    found = terms(
        "Morgan Stanley Maintains Overweight on Apple, Raises Price Target to $300"
    )

    assert set(found) == {"apple"}


def test_notes_on_unrelated_companies_do_not_form_a_theme() -> None:
    """Measured on real data: 218 such notes once clustered as one theme."""
    notes = [
        _story(
            0,
            "alpaca.news",
            "BTIG Maintains Buy on CrowdStrike, Raises Price Target to $312",
        ),
        _story(
            1,
            "alpaca.news",
            "Baird Maintains Neutral on Domino's Pizza, Lowers Price Target to $320",
        ),
        _story(
            2,
            "alpaca.news",
            "Barclays Maintains Overweight on Shift4 Payments, Raises Price Target",
        ),
        _story(
            3,
            "alpaca.news",
            "Wells Fargo Reiterates Buy on Amplitude, Lowers Price Target to $16",
        ),
    ]

    assert all(len(c.stories) == 1 for c in cluster_stories(notes))


def test_the_subject_of_a_note_still_groups_its_own_coverage() -> None:
    notes = [
        _story(0, "rss.infomoney", "Barclays Maintains Buy on Dominos Pizza"),
        _story(1, "rss.moneytimes", "Baird Lowers Price Target on Dominos Pizza"),
    ]

    (cluster,) = cluster_stories(notes)

    assert len(cluster.stories) == 2
    assert cluster.label == "dominos · pizza"
