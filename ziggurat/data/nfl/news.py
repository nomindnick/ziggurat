"""The player-news wire (item 3.6, the SPEC "speed lane").

PRIMARY SOURCE: ESPN's public news API
``GET https://site.api.espn.com/apis/site/v2/sports/football/nfl/news`` — chosen
(recon R3) because each article carries a stable integer id, an ISO-UTC
``published`` instant (a leakage-clean ``knowable_as_of`` basis — publish time,
never gameday), and a ``categories[]`` array whose athlete entries carry
``athleteId`` == ``players.espn_id``: a DIRECT structured join, zero fuzzy
name-matching, so this permanent module never reaches into ``draft/resolver.py``
(Rule 8). No auth needed.

A RotoWire RSS fallback (``source='rotowire'``) is a labelled deferral: it is the
truer minutes-fresh beat-note wire but its free feed is 5-item/lossy, its blurb
text is copyrighted (local-only), and it has no espn_id — it needs a Sleeper
``rotowire_id`` bridge. The schema's ``source`` column lets it land additively.

TWO-COLUMN TIME (Rule 1). ``knowable_as_of`` is the day-granular gate every
accessor shares; ``published_at`` keeps the FULL UTC instant so a leakage-sensitive
reader (an intraday backtest, a Sunday-1pm lineup) can pass ``published_before=``
and refuse a note that only went public at 2pm. Both are leakage-tested.

APPEND-ONLY. Each pull re-fetches the current window and upserts by
``(source, news_id, retrieved_as_of)``; a correction (ESPN edits an article) lands
as a new retrieved version that ``select_as_of`` resolves as "latest per key".
Nothing is ever deleted, so there is no destroy-the-day partition to floor.
"""

import json
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ziggurat import net
from ziggurat.data.nfl import base

ESPN_NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news"
DEFAULT_LIMIT = 50
SOURCE_ESPN = "espn"

#: The calendar `knowable_as_of` is bucketed in. It MUST match the calendar the
#: live as-of gate is expressed in: the authoritative host runs on Pacific time and
#: the CLI's `_today()` is `date.today()` there, so a note is knowable on its
#: PACIFIC calendar day. Bucketing on the raw UTC date (the earlier bug) stamped an
#: evening-Pacific note as UTC-tomorrow and silently withheld it from the speed lane
#: until the next local midnight — exactly the minutes-fresh hours (audit D1).
KNOWABLE_TZ = ZoneInfo("America/Los_Angeles")


def _knowable_date(published: str) -> str | None:
    """The Pacific-calendar day of a UTC publish instant (see KNOWABLE_TZ)."""
    if not published:
        return None
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return base.iso_date(published)  # fallback: raw truncation on a weird string
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KNOWABLE_TZ).date().isoformat()


def fetch_espn_news(limit: int = DEFAULT_LIMIT) -> dict:
    """The ONE ESPN-news network seam. Tests patch this; no live call runs offline.

    Bounded (item 3.1b): an unbounded ``urlopen`` under systemd ``Type=oneshot``
    parks the alert cadence forever. This runs on the ~20-minute alert tick.
    """
    url = f"{ESPN_NEWS_URL}?{urllib.parse.urlencode({'limit': limit})}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "ziggurat/3.6 (personal fantasy tool)"}
    )
    with urllib.request.urlopen(req, timeout=net.HTTP_TIMEOUT) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _parse_articles(payload: dict, *, retrieved_as_of: str) -> tuple[list[dict], list[dict]]:
    """Split an ESPN news payload into (article rows, athlete-link rows).

    An article with no stable id or no publish instant is UNUSABLE (we could not
    dedup it or gate it), so it is dropped — not stored with a guessed time.
    """
    articles: list[dict] = []
    links: list[dict] = []
    for art in payload.get("articles") or []:
        news_id = art.get("id")
        published = art.get("published")
        if news_id is None or not published:
            continue
        news_id = str(news_id)
        knowable = _knowable_date(published)
        headline = (art.get("headline") or "").strip()
        articles.append(
            {
                "source": SOURCE_ESPN,
                "news_id": news_id,
                "news_type": art.get("type"),
                "headline": headline or "(untitled)",
                "body": art.get("description"),
                "byline": art.get("byline"),
                "url": ((art.get("links") or {}).get("web") or {}).get("href"),
                "published_at": published,
                "knowable_as_of": knowable,
                "retrieved_as_of": retrieved_as_of,
            }
        )
        seen: set[str] = set()
        for cat in art.get("categories") or []:
            if cat.get("type") != "athlete":
                continue
            athlete_id = cat.get("athleteId")
            if athlete_id is None:
                athlete_id = (cat.get("athlete") or {}).get("id")
            if athlete_id is None:
                continue
            espn_id = str(athlete_id)
            if espn_id in seen:  # an article naming the same athlete twice collapses
                continue
            seen.add(espn_id)
            links.append(
                {
                    "source": SOURCE_ESPN,
                    "news_id": news_id,
                    "espn_id": espn_id,
                    "gsis_id": None,  # resolved via crosswalk in pull_news
                    "player_name": cat.get("description"),
                    "team": None,
                    "published_at": published,
                    "knowable_as_of": knowable,
                    "retrieved_as_of": retrieved_as_of,
                }
            )
    return articles, links


def pull_news(conn, *, retrieved_as_of, limit: int = DEFAULT_LIMIT, fetch=fetch_espn_news) -> dict:
    """Fetch, parse, resolve gsis, and store one ESPN news window.

    Returns a summary dict (``articles``/``links``/``resolved``/``unresolved``)
    the alert tick logs. "0 articles" is surfaced in the count, not swallowed — a
    caller treats a normally-populated feed returning empty as degraded.
    """
    retrieved = base.iso_date(retrieved_as_of)
    payload = fetch(limit=limit)
    articles, links = _parse_articles(payload, retrieved_as_of=retrieved)

    gsis_by_espn = base.gsis_by_espn(conn)
    resolved = 0
    for link in links:
        gsis = gsis_by_espn.get(link["espn_id"])
        if gsis:
            link["gsis_id"] = gsis
            resolved += 1

    with conn:
        if articles:
            base.upsert(
                conn,
                "player_news",
                articles,
                key_cols=("source", "news_id", "retrieved_as_of"),
                commit=False,
            )
        if links:
            base.upsert(
                conn,
                "player_news_links",
                links,
                key_cols=("source", "news_id", "espn_id", "retrieved_as_of"),
                commit=False,
            )
    return {
        "articles": len(articles),
        "links": len(links),
        "resolved": resolved,
        "unresolved": len(links) - resolved,
    }


def _links_for_versions(conn, article_versions) -> dict:
    """The athletes for each RESOLVED article version, keyed by the article's own
    (source, news_id, retrieved_as_of).

    WHY NOT select_as_of on the links table (audit D1 ghost-link fix): that resolves
    the latest-retrieved row PER (source, news_id, espn_id), so an athlete DROPPED by
    a correction (his link has no superseding version) stays visible — a ghost that
    surfaces the corrected article under a player it no longer concerns and fires a
    spurious NEWS alert. Because pull_news co-writes the article AND its full current
    athlete set at the SAME retrieved_as_of on every pull, the current athletes are
    exactly the links stamped with the article's RESOLVED retrieved_as_of; a dropped
    athlete carries an OLDER stamp and is correctly excluded.
    """
    by_article: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for source, news_id, retrieved in article_versions:
        for row in conn.execute(
            "SELECT espn_id, gsis_id, player_name, team FROM player_news_links "
            "WHERE source = ? AND news_id = ? AND retrieved_as_of = ?",
            (source, news_id, retrieved),
        ):
            by_article[(source, news_id)].append(
                {
                    "espn_id": row["espn_id"],
                    "gsis_id": row["gsis_id"],
                    "player_name": row["player_name"],
                    "team": row["team"],
                }
            )
    return by_article


def recent_news(
    conn,
    *,
    as_of,
    since=None,
    published_before=None,
    view: base.AsOfView = "historical",
    limit: int | None = None,
) -> list[dict]:
    """News articles knowable by ``as_of``, newest first, each with its resolved
    players attached.

    ``published_before`` (a full-precision ISO instant) is the INTRADAY leakage
    gate: ``as_of`` alone is day-granular, so a Sunday-2pm note is "knowable" all
    of Sunday; pass ``published_before='2026-09-13T18:00:00Z'`` to exclude notes
    that went public after a decision instant. ``since`` (a date) bounds how far
    back the window reaches — the alert tick uses it to only consider fresh notes.
    """
    clauses: list[str] = []
    params: dict = {}
    if since is not None:
        clauses.append("t.knowable_as_of >= :since")
        params["since"] = base.iso_date(since)
    if published_before is not None:
        clauses.append("t.published_at < :pub_before")
        params["pub_before"] = published_before

    rows = base.select_as_of(
        conn,
        "player_news",
        as_of=as_of,
        key_cols=("source", "news_id"),
        view=view,
        extra_where=" AND ".join(clauses),
        params=params,
    )
    by_article = _links_for_versions(
        conn, [(r["source"], r["news_id"], r["retrieved_as_of"]) for r in rows]
    )
    articles = [
        {
            "source": row["source"],
            "news_id": row["news_id"],
            "news_type": row["news_type"],
            "headline": row["headline"],
            "body": row["body"],
            "byline": row["byline"],
            "url": row["url"],
            "published_at": row["published_at"],
            "knowable_as_of": row["knowable_as_of"],
            "players": by_article.get((row["source"], row["news_id"]), []),
        }
        for row in rows
    ]
    articles.sort(key=lambda a: a["published_at"], reverse=True)
    if limit is not None:
        articles = articles[:limit]
    return articles


def news_for_player(
    conn,
    *,
    as_of,
    espn_id,
    since=None,
    published_before=None,
    view: base.AsOfView = "historical",
    limit: int | None = None,
) -> list[dict]:
    """The news window for ONE player (join on the direct espn_id link)."""
    articles = recent_news(
        conn, as_of=as_of, since=since, published_before=published_before, view=view
    )
    espn_id = str(espn_id)
    hits = [a for a in articles if any(p["espn_id"] == espn_id for p in a["players"])]
    if limit is not None:
        hits = hits[:limit]
    return hits
