"""Item 3.6 — the ESPN player-news wire: parse, entity-resolve, store, and the
mandatory as-of leakage tests (day gate + intraday published_before gate)."""

import pytest

from ziggurat.data.nfl import news


def _seed_crosswalk(conn, rows):
    """Minimal players crosswalk so gsis_by_espn resolves the news links."""
    for espn_id, gsis_id, name in rows:
        conn.execute(
            "INSERT INTO players (gsis_id, espn_id, name, retrieved_as_of, knowable_as_of) "
            "VALUES (?, ?, ?, ?, ?)",
            (gsis_id, str(espn_id), name, "2026-09-01", "2026-09-01"),
        )
    conn.commit()


def _payload(articles):
    return {"articles": articles}


def _article(news_id, published, *, headline="Report", athletes=(), body="blurb", art_type="Story"):
    return {
        "id": news_id,
        "type": art_type,
        "headline": headline,
        "description": body,
        "byline": "Beat Writer",
        "published": published,
        "links": {"web": {"href": f"https://espn.com/{news_id}"}},
        "categories": [
            {"type": "athlete", "athleteId": aid, "description": name}
            for aid, name in athletes
        ],
    }


def test_pull_parses_and_resolves_direct_espn_join(push_db):
    _seed_crosswalk(push_db, [(4595348, "00-0037765", "Malik Nabers")])
    payload = _payload(
        [
            _article(
                49488047,
                "2026-09-10T13:51:50Z",
                headline="Nabers full go",
                athletes=[(4595348, "Malik Nabers")],
            )
        ]
    )
    summary = news.pull_news(push_db, retrieved_as_of="2026-09-10", fetch=lambda limit: payload)
    assert summary == {"articles": 1, "links": 1, "resolved": 1, "unresolved": 0}

    got = news.recent_news(push_db, as_of="2026-09-10")
    assert len(got) == 1
    assert got[0]["headline"] == "Nabers full go"
    assert got[0]["players"][0]["espn_id"] == "4595348"
    assert got[0]["players"][0]["gsis_id"] == "00-0037765"  # resolved via crosswalk


def test_unresolved_athlete_link_is_kept_not_dropped(push_db):
    # No crosswalk row for this athlete: the link row must still exist with NULL gsis.
    payload = _payload(
        [_article(1, "2026-09-10T10:00:00Z", athletes=[(9999999, "Rookie Unknown")])]
    )
    summary = news.pull_news(push_db, retrieved_as_of="2026-09-10", fetch=lambda limit: payload)
    assert summary["links"] == 1 and summary["resolved"] == 0 and summary["unresolved"] == 1
    got = news.recent_news(push_db, as_of="2026-09-10")
    assert got[0]["players"][0]["gsis_id"] is None
    assert got[0]["players"][0]["espn_id"] == "9999999"


def test_article_without_id_or_publish_is_dropped(push_db):
    payload = {"articles": [
        {"id": None, "published": "2026-09-10T10:00:00Z", "headline": "no id"},
        {"id": 5, "published": None, "headline": "no time"},
        _article(6, "2026-09-10T10:00:00Z", headline="good"),
    ]}
    summary = news.pull_news(push_db, retrieved_as_of="2026-09-10", fetch=lambda limit: payload)
    assert summary["articles"] == 1
    assert [a["headline"] for a in news.recent_news(push_db, as_of="2026-09-10")] == ["good"]


def test_leakage_day_gate(push_db):
    # An article published on D is not knowable at D-1 (Rule 1 day gate).
    payload = _payload([_article(10, "2026-09-11T09:00:00Z", headline="Thursday note")])
    news.pull_news(push_db, retrieved_as_of="2026-09-11", fetch=lambda limit: payload)
    assert news.recent_news(push_db, as_of="2026-09-10") == []
    assert len(news.recent_news(push_db, as_of="2026-09-11")) == 1


def test_leakage_retrieved_gate(push_db):
    # Even a published_at in the past must not be visible before it was RETRIEVED
    # (a backfilled pull cannot inform a decision made before the pull existed).
    payload = _payload([_article(11, "2026-09-05T09:00:00Z", headline="old note, late pull")])
    news.pull_news(push_db, retrieved_as_of="2026-09-11", fetch=lambda limit: payload)
    # historical view gates retrieved_as_of <= as_of, so as_of=09-06 sees nothing.
    assert news.recent_news(push_db, as_of="2026-09-06") == []
    assert len(news.recent_news(push_db, as_of="2026-09-11")) == 1


def test_intraday_published_before_gate(push_db):
    # The speed-lane nuance: as_of is day-granular, so a 2pm note is "knowable"
    # all of Sunday. published_before excludes notes public after a decision time.
    payload = _payload(
        [
            _article(20, "2026-09-13T17:00:00Z", headline="1pm ET report"),
            _article(21, "2026-09-13T19:30:00Z", headline="3:30pm ET report"),
        ]
    )
    news.pull_news(push_db, retrieved_as_of="2026-09-13", fetch=lambda limit: payload)
    all_day = news.recent_news(push_db, as_of="2026-09-13")
    assert len(all_day) == 2
    pre_1pm = news.recent_news(
        push_db, as_of="2026-09-13", published_before="2026-09-13T17:00:00Z"
    )
    assert [a["headline"] for a in pre_1pm] == []  # strict <, so the 1pm note excluded
    pre_2pm = news.recent_news(
        push_db, as_of="2026-09-13", published_before="2026-09-13T18:00:00Z"
    )
    assert [a["headline"] for a in pre_2pm] == ["1pm ET report"]


def test_recent_news_since_and_ordering(push_db):
    payload = _payload(
        [
            _article(30, "2026-09-08T12:00:00Z", headline="older"),
            _article(31, "2026-09-11T12:00:00Z", headline="newer"),
        ]
    )
    news.pull_news(push_db, retrieved_as_of="2026-09-11", fetch=lambda limit: payload)
    got = news.recent_news(push_db, as_of="2026-09-11")
    assert [a["headline"] for a in got] == ["newer", "older"]  # newest first
    recent = news.recent_news(push_db, as_of="2026-09-11", since="2026-09-10")
    assert [a["headline"] for a in recent] == ["newer"]


def test_news_for_player_filters_by_espn_id(push_db):
    _seed_crosswalk(push_db, [(100, "00-0000100", "Star One"), (200, "00-0000200", "Star Two")])
    payload = _payload(
        [
            _article(40, "2026-09-11T12:00:00Z", headline="One hurts", athletes=[(100, "Star One")]),
            _article(41, "2026-09-11T13:00:00Z", headline="Two soars", athletes=[(200, "Star Two")]),
        ]
    )
    news.pull_news(push_db, retrieved_as_of="2026-09-11", fetch=lambda limit: payload)
    hits = news.news_for_player(push_db, as_of="2026-09-11", espn_id=100)
    assert [a["headline"] for a in hits] == ["One hurts"]


def test_evening_pacific_note_is_knowable_that_local_day(push_db):
    # audit D1: a note published 8pm PDT == 03:00 UTC next day must be knowable on
    # its PACIFIC calendar day (the calendar the live as_of gate uses), NOT withheld
    # to the UTC-tomorrow date.
    payload = _payload([_article(60, "2026-09-15T03:00:00Z", headline="late night bomb")])
    news.pull_news(push_db, retrieved_as_of="2026-09-14", fetch=lambda limit: payload)
    # Pacific date of 2026-09-15T03:00Z is 2026-09-14 (8pm PDT on the 14th).
    got = news.recent_news(push_db, as_of="2026-09-14")
    assert [a["headline"] for a in got] == ["late night bomb"]


def test_correction_dropping_athlete_leaves_no_ghost(push_db):
    # audit D1: an ESPN correction that REMOVES an athlete must not keep surfacing
    # the article under the dropped player (a ghost link -> spurious NEWS alert).
    _seed_crosswalk(push_db, [(100, "00-0000100", "Star A"), (200, "00-0000200", "Star B")])
    day1 = _payload([_article(70, "2026-09-11T12:00:00Z", headline="rumor",
                              athletes=[(100, "Star A"), (200, "Star B")])])
    news.pull_news(push_db, retrieved_as_of="2026-09-11", fetch=lambda limit: day1)
    # correction: same article, now only names A.
    day2 = _payload([_article(70, "2026-09-11T12:00:00Z", headline="rumor (corrected)",
                              athletes=[(100, "Star A")])])
    news.pull_news(push_db, retrieved_as_of="2026-09-12", fetch=lambda limit: day2)
    got = news.recent_news(push_db, as_of="2026-09-12")
    assert len(got) == 1 and got[0]["headline"] == "rumor (corrected)"
    ids = {p["espn_id"] for p in got[0]["players"]}
    assert ids == {"100"}  # B is gone, not a ghost


def test_correction_resolves_to_latest_retrieved(push_db):
    # A re-pull that carries an edited headline is a new retrieved version; the
    # latest-retrieved-per-key resolution returns the correction.
    day1 = _payload([_article(50, "2026-09-11T12:00:00Z", headline="original")])
    news.pull_news(push_db, retrieved_as_of="2026-09-11", fetch=lambda limit: day1)
    day2 = _payload([_article(50, "2026-09-11T12:00:00Z", headline="corrected")])
    news.pull_news(push_db, retrieved_as_of="2026-09-12", fetch=lambda limit: day2)
    got = news.recent_news(push_db, as_of="2026-09-12")
    assert len(got) == 1 and got[0]["headline"] == "corrected"
