"""Group headlines into themes, in a way that can be explained line by line.

PLANO_ANTECIPACAO §4: normalise the headline, extract terms, group stories whose
terms overlap above a threshold inside a 48 hour window, label the theme with
its three most frequent terms. No language model, no embedding, no external
service. If it proves not good enough it is improved with data in hand.

A "story" is one (outlet, headline). The catalyst table holds one row per story
per instrument it names, so a story tagged on four stocks is four catalyst rows
but one story; counting rows would make a single article look like a trend.
"""

import argparse
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Final

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from guardrail.db.models import (
    Candle,
    Catalyst,
    NewsTheme,
    NewsThemeAsset,
    NewsThemeItem,
)
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

WINDOW: Final = timedelta(hours=48)

# A story joins a theme when at least MIN_SHARED of its terms are among the
# theme's PROFILE_SIZE most frequent terms and they make up at least MIN_OVERLAP
# of the story's terms.
MIN_SHARED: Final = 2
MIN_OVERLAP: Final = Decimal("0.4")
PROFILE_SIZE: Final = 10
LABEL_TERMS: Final = 3
MIN_TERM_LENGTH: Final = 3
# Themes with fewer stories than this are not stored: the screen would not
# show them anyway (queries.THEME_MIN_ITEMS), and the rest is noise.
MIN_STORIES_STORED: Final = 2
MAX_ASSETS: Final = 8


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Words that carry no subject. Written without accents: terms are compared
# without them. Plain data, easy to extend when a useless word shows up in a
# label.
STOPWORDS: Final = _words(
    """
para com uma uns umas dos das nos nas por mais como entre sobre apos depois
ate pela pelo seu sua seus suas esta este essa esse isso mas nao sem ser foi
sao vai pode ainda quando tambem aos que sua dele dela eles elas tem ter
the and for with from that this will after over into amid about are was were
has have its their than more says said new not but out can may how why who
what when where year years
    """
)

# Vocabulary of analyst-rating notes ("Morgan Stanley Maintains Overweight on X,
# Raises Price Target to $Y") and of the brokers that write them. Measured on a
# real 48-hour window of 1,278 stories: these words appear in 4% to 44% of all
# headlines, and without removing them 218 notes on unrelated companies formed
# one "theme". They say nothing about what is happening in the world.
RATING_NOTE_WORDS: Final = _words(
    """
price target targets maintains maintain lowers lower raises raise reiterates
reiterated upgrades upgrade downgrades downgrade upgraded downgraded
initiates initiated coverage rating ratings buy sell hold overweight
underweight outperform underperform neutral equal weight perform stock
stocks shares share earnings call transcript announces announced analyst
analysts would much today ago says said
morgan stanley barclays wells fargo rbc goldman sachs jpmorgan citi
citigroup ubs deutsche baird btig piper sandler jefferies evercore stifel
cowen keybanc oppenheimer mizuho nomura bernstein raymond james truist
wedbush needham canaccord craig hallum loop capital securities
    """
)
ALL_STOPWORDS: Final = STOPWORDS | RATING_NOTE_WORDS

_WORD: Final = re.compile(r"[^\W\d_]+", re.UNICODE)


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def terms(headline: str) -> dict[str, str]:
    """Subject words of a headline: normalised key -> lower-case display form.

    Keys drop accents (so "aço" and "aco" are one term); the display form keeps
    them for the label.
    """
    out: dict[str, str] = {}
    for word in _WORD.findall(headline.lower()):
        key = _strip_accents(word)
        if len(key) < MIN_TERM_LENGTH or key in ALL_STOPWORDS:
            continue
        out.setdefault(key, word)
    return out


@dataclass(frozen=True, slots=True)
class Story:
    """One headline from one outlet, with every catalyst row it produced."""

    outlet: str
    headline: str
    ts: datetime
    catalyst_ids: tuple[int, ...]
    instrument_ids: tuple[int, ...]


@dataclass(slots=True)
class Cluster:
    """A group of stories about the same subject."""

    stories: list[Story] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    display: dict[str, str] = field(default_factory=dict)

    def add(self, story: Story, story_terms: dict[str, str]) -> None:
        self.stories.append(story)
        self.counts.update(story_terms.keys())
        for key, shown in story_terms.items():
            self.display.setdefault(key, shown)

    def profile(self) -> set[str]:
        """The most frequent terms; ties break alphabetically so it is stable."""
        ranked = sorted(self.counts, key=lambda t: (-self.counts[t], t))
        return set(ranked[:PROFILE_SIZE])

    @property
    def label(self) -> str:
        ranked = sorted(self.counts, key=lambda t: (-self.counts[t], t))
        return " · ".join(self.display[t] for t in ranked[:LABEL_TERMS])

    @property
    def first_seen(self) -> datetime:
        return min(s.ts for s in self.stories)

    @property
    def last_seen(self) -> datetime:
        return max(s.ts for s in self.stories)


def _fits(story_terms: dict[str, str], cluster: Cluster) -> bool:
    shared = len(story_terms.keys() & cluster.profile())
    if shared < MIN_SHARED or not story_terms:
        return False
    return Decimal(shared) / Decimal(len(story_terms)) >= MIN_OVERLAP


def cluster_stories(stories: list[Story]) -> list[Cluster]:
    """Greedy grouping, oldest story first.

    Each story goes to the best-fitting existing theme (most shared terms, then
    the earliest created) or starts its own. Deterministic for a given input.
    """
    clusters: list[Cluster] = []
    for story in sorted(stories, key=lambda s: (s.ts, s.outlet, s.headline)):
        story_terms = terms(story.headline)
        best: Cluster | None = None
        best_shared = 0
        for candidate in clusters:
            if story.ts - candidate.last_seen > WINDOW:
                continue
            if not _fits(story_terms, candidate):
                continue
            shared = len(story_terms.keys() & candidate.profile())
            if shared > best_shared:
                best, best_shared = candidate, shared
        if best is None:
            best = Cluster()
            clusters.append(best)
        best.add(story, story_terms)
    return clusters


@dataclass(slots=True)
class Report:
    """What a run did."""

    stories: int = 0
    themes: int = 0
    assets: int = 0


def load_stories(session: Session, now: datetime) -> list[Story]:
    """Stories of the last 48 hours, one per (outlet, headline)."""
    rows = session.execute(
        select(
            Catalyst.id,
            Catalyst.source,
            Catalyst.headline,
            Catalyst.ts,
            Catalyst.instrument_id,
        ).where(Catalyst.ts >= now - WINDOW, Catalyst.ts <= now)
    ).all()
    grouped: dict[tuple[str, str], list[tuple[int, datetime, int]]] = {}
    for cid, source, headline, ts, instrument_id in rows:
        grouped.setdefault((source, headline), []).append((cid, ts, instrument_id))
    return [
        Story(
            outlet=source,
            headline=headline,
            ts=min(ts for _, ts, _ in members),
            catalyst_ids=tuple(sorted(cid for cid, _, _ in members)),
            instrument_ids=tuple(sorted({i for _, _, i in members})),
        )
        for (source, headline), members in grouped.items()
    ]


def day_move(session: Session, instrument_id: int, since: datetime) -> Decimal | None:
    """Latest daily change of an instrument, if a bar exists from `since` on.

    A move measured on a bar from before the news says nothing about the news.
    """
    bars = session.execute(
        select(Candle.ts, Candle.close)
        .where(Candle.instrument_id == instrument_id, Candle.timeframe == "1d")
        .order_by(Candle.ts.desc())
        .limit(2)
    ).all()
    if len(bars) < 2:
        return None
    (last_ts, last_close), (_, prev_close) = bars
    since_day = datetime.combine(since.astimezone(UTC).date(), time(), tzinfo=UTC)
    if last_ts < since_day or prev_close <= 0:
        return None
    return Decimal(last_close) / Decimal(prev_close) - 1


def recompute(session: Session, now: datetime) -> Report:
    """Rebuild the themes of the last 48 hours inside the caller's transaction.

    Replaces every stored theme: they are derived, and a theme older than the
    window is no longer a theme of the day.
    """
    stories = load_stories(session, now)
    report = Report(stories=len(stories))
    session.execute(delete(NewsTheme))

    for cluster in cluster_stories(stories):
        if len(cluster.stories) < MIN_STORIES_STORED:
            continue
        theme = NewsTheme(
            label=cluster.label,
            first_seen_at=cluster.first_seen,
            last_seen_at=cluster.last_seen,
        )
        session.add(theme)
        session.flush()
        catalyst_ids = sorted({c for s in cluster.stories for c in s.catalyst_ids})
        session.add_all(
            [NewsThemeItem(theme_id=theme.id, catalyst_id=c) for c in catalyst_ids]
        )

        moves: list[tuple[int, Decimal]] = []
        for instrument_id in sorted(
            {i for s in cluster.stories for i in s.instrument_ids}
        ):
            move = day_move(session, instrument_id, cluster.first_seen)
            if move is not None:
                moves.append((instrument_id, move))
        moves.sort(key=lambda m: abs(m[1]), reverse=True)
        for instrument_id, move in moves[:MAX_ASSETS]:
            session.add(
                NewsThemeAsset(
                    theme_id=theme.id,
                    instrument_id=instrument_id,
                    move_pct=move.quantize(Decimal("0.00000001")),
                )
            )
            report.assets += 1
        report.themes += 1
    session.flush()
    return report


def main() -> int:
    """Command line entry point."""
    argparse.ArgumentParser(
        description="Group recent headlines into themes."
    ).parse_args()
    try:
        with session_scope() as session:
            report = recompute(session, datetime.now(UTC))
    finally:
        dispose_engine()
    print(f"\nstories read:  {report.stories}")
    print(f"themes stored: {report.themes}")
    print(f"asset moves:   {report.assets}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
