"""Item 3.6 — the Rule-5 OUTBOUND boundary (the ntfy egress surface).

Mirrors tests/test_repo_boundary.py but for message TEXT, not file paths: a
colleague's team name must never leave the box for a public-by-obscurity ntfy
topic, even though it lives freely in the gitignored DB. Also: the size cap, the
own-team carve-out, the misconfig refusals, and the single-choke-point rule."""

from pathlib import Path

import pytest

from ziggurat.push import outbound
from ziggurat.push.outbound import (
    NtfyConfig,
    OutboundBoundaryError,
    assert_publishable,
    league_private_strings,
    publish,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed_teams(conn, rows, *, season=2026):
    """rows: list of (team_id, name, abbrev, primary_owner)."""
    for team_id, name, abbrev, owner in rows:
        conn.execute(
            "INSERT INTO league_teams (season, team_id, name, abbrev, primary_owner, "
            "retrieved_as_of, knowable_as_of) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (season, team_id, name, abbrev, owner, "2026-09-01", "2026-09-01"),
        )
    conn.commit()


def _ctx(conn):
    return dict(conn=conn, as_of="2026-09-01", season=2026, own_team_id=1)


def test_blocks_colleague_team_name(push_db):
    _seed_teams(push_db, [
        (1, "My Squad", "MINE", "{OWNER-ME}"),
        (2, "Josh's Juggernauts", "JUGS", "{OWNER-JOSH}"),
    ])
    with pytest.raises(OutboundBoundaryError, match="team 2"):
        assert_publishable("Heads up: you play Josh's Juggernauts this week", **_ctx(push_db))


def test_allows_player_only_teaser(push_db):
    _seed_teams(push_db, [
        (1, "My Squad", "MINE", "{OWNER-ME}"),
        (2, "Josh's Juggernauts", "JUGS", "{OWNER-JOSH}"),
    ])
    # No exception: pure player names + counts.
    assert_publishable("2 claims: add Bijan Robinson, drop Zach Charbonnet", **_ctx(push_db))


def test_allows_own_team_name(push_db):
    _seed_teams(push_db, [
        (1, "My Squad", "MINE", "{OWNER-ME}"),
        (2, "Rivals", "RVL", "{OWNER-JOSH}"),
    ])
    # The operator's OWN team name is SPEC-permitted to cross.
    assert_publishable("My Squad: 2 waiver claims ready", **_ctx(push_db))


def test_blocks_colleague_owner_string(push_db):
    _seed_teams(push_db, [
        (1, "My Squad", "MINE", "{OWNER-ME}"),
        (2, "Rivals", "RVL", "SomeColleagueGuid123"),
    ])
    with pytest.raises(OutboundBoundaryError, match="team 2"):
        assert_publishable("owner SomeColleagueGuid123 dropped a RB", **_ctx(push_db))


def test_short_abbrev_not_matched_but_long_is(push_db):
    # 2-3 char abbrevs collide with NFL codes / common words -> not matched.
    _seed_teams(push_db, [
        (1, "My Squad", "MINE", "{ME}"),
        (2, "Team Two", "GB", "{G}"),        # 2 chars -> NOT scrubbed
        (3, "Team Three", "LONGABB", "{L}"),  # >=4 chars -> scrubbed
    ])
    assert_publishable("GB defense is a good stream this week", **_ctx(push_db))  # allowed
    with pytest.raises(OutboundBoundaryError, match="team 3"):
        assert_publishable("waiver rival LONGABB grabbed him", **_ctx(push_db))


def test_word_boundary_does_not_overblock_substring(push_db):
    # audit D2: a team named 'Rivals' must NOT block a headline containing 'Arrivals'
    # (substring inside a longer word); it MUST still block the standalone token.
    _seed_teams(push_db, [(1, "My Squad", "MINE", "{ME}"), (2, "Rivals", "RVLS", "{J}")])
    assert_publishable("Arrivals and departures at camp", **_ctx(push_db))  # allowed
    with pytest.raises(OutboundBoundaryError, match="team 2"):
        assert_publishable("you face the Rivals this week", **_ctx(push_db))


def test_scrub_covers_title_and_tags_headers(push_db):
    # audit D2: title/tags/click leave the box too — a leak there must be caught.
    _seed_teams(push_db, [(1, "My Squad", "MINE", "{ME}"), (2, "Josh's Juggernauts", "JUGS", "{J}")])
    sent = []
    cfg = NtfyConfig(server="https://ntfy.sh", topic="zig-secret", token=None)
    with pytest.raises(OutboundBoundaryError):
        publish("add Bijan Robinson", conn=push_db, as_of="2026-09-01", season=2026,
                own_team_id=1, title="vs Josh's Juggernauts", config=cfg,
                poster=lambda *a: sent.append(a) or 200)
    assert sent == []  # a header leak blocks the send


def test_fail_closed_when_no_league_snapshot(push_db):
    # audit D2 / P1: a REAL push with no team rows cannot build the denylist -> refuse
    # (fail closed). A dry run is exempt (the --no-push preview before a sync).
    cfg = NtfyConfig(server="https://ntfy.sh", topic="zig-secret", token=None)
    with pytest.raises(OutboundBoundaryError, match="no league snapshot"):
        publish("add Bijan Robinson", conn=push_db, as_of="2026-09-01", season=2026,
                own_team_id=1, config=cfg, poster=lambda *a: 200)
    # dry run does NOT fail closed
    res = publish("add Bijan Robinson", conn=push_db, as_of="2026-09-01", season=2026,
                  own_team_id=1, config=cfg, dry_run=True, poster=lambda *a: 200)
    assert res.status == "dry_run"


def test_own_team_id_required(push_db):
    _seed_teams(push_db, [(1, "My Squad", "MINE", "{ME}")])
    with pytest.raises(OutboundBoundaryError, match="own_team_id"):
        league_private_strings(push_db, as_of="2026-09-01", season=2026, own_team_id=None)


def test_size_cap(push_db):
    _seed_teams(push_db, [(1, "My Squad", "MINE", "{ME}")])
    huge = "x" * (outbound.NTFY_MAX_BYTES + 1)
    with pytest.raises(OutboundBoundaryError, match="ntfy cap"):
        assert_publishable(huge, **_ctx(push_db))


def test_publish_runs_scrub_before_send(push_db):
    _seed_teams(push_db, [(1, "My Squad", "MINE", "{ME}"), (2, "Rivals Inc", "RVLS", "{J}")])
    sent = []

    def fake_poster(url, body, headers, timeout):
        sent.append((url, body, headers))
        return 200

    cfg = NtfyConfig(server="https://ntfy.sh", topic="zig-secret", token=None)
    # A leaking teaser must raise and NEVER call the poster.
    with pytest.raises(OutboundBoundaryError):
        publish("you face Rivals Inc", conn=push_db, as_of="2026-09-01", season=2026,
                own_team_id=1, config=cfg, poster=fake_poster)
    assert sent == []
    # A clean teaser goes through.
    res = publish("add Bijan Robinson (2 claims)", conn=push_db, as_of="2026-09-01",
                  season=2026, own_team_id=1, config=cfg, title="Ziggurat", tags="football",
                  poster=fake_poster)
    assert res.ok and res.status == "200"
    assert len(sent) == 1
    url, body, headers = sent[0]
    assert url == "https://ntfy.sh/zig-secret"
    assert headers["Title"] == "Ziggurat" and headers["Tags"] == "football"


def test_publish_dry_run_sends_nothing(push_db):
    _seed_teams(push_db, [(1, "My Squad", "MINE", "{ME}")])
    calls = []
    cfg = NtfyConfig(server="https://ntfy.sh", topic="zig-secret", token=None)
    res = publish("add Bijan Robinson", conn=push_db, as_of="2026-09-01", season=2026,
                  own_team_id=1, config=cfg, dry_run=True, poster=lambda *a: calls.append(a))
    assert res.status == "dry_run" and calls == []


def test_publish_network_failure_is_loud_not_swallowed(push_db):
    _seed_teams(push_db, [(1, "My Squad", "MINE", "{ME}")])

    def boom(url, body, headers, timeout):
        raise OSError("connection refused")

    cfg = NtfyConfig(server="https://ntfy.sh", topic="zig-secret", token=None)
    res = publish("add Bijan Robinson", conn=push_db, as_of="2026-09-01", season=2026,
                  own_team_id=1, config=cfg, poster=boom)
    assert res.ok is False and res.status.startswith("error:")


def test_load_config_requires_topic():
    with pytest.raises(OutboundBoundaryError, match="NTFY_TOPIC"):
        outbound.load_ntfy_config(environ={})
    cfg = outbound.load_ntfy_config(environ={"NTFY_TOPIC": "zig-xyz"})
    assert cfg.server == "https://ntfy.sh" and cfg.token is None
    assert cfg.url == "https://ntfy.sh/zig-xyz"


def test_ntfy_is_a_single_choke_point():
    """No module outside ziggurat/push/ may TALK to ntfy directly (Rule-4-shaped:
    one door, so the scrub cannot be bypassed). Reaching ntfy requires its config
    (the NTFY_* env vars) and/or the server URL; only ziggurat/push/ may. A prose
    mention of the word 'ntfy' in help text or a comment is fine — the signal is
    reading NTFY_* config or embedding the ntfy.sh endpoint."""
    offenders = []
    for path in (REPO_ROOT / "ziggurat").rglob("*.py"):
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        if rel.startswith("ziggurat/push/"):
            continue
        text = path.read_text(encoding="utf-8")
        if "NTFY_" in text or "ntfy.sh" in text:
            offenders.append(rel)
    assert offenders == [], f"ntfy accessed outside ziggurat/push/: {offenders}"
