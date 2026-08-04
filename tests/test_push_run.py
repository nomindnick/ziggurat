"""Item 3.6 — the push orchestration layer (push/runs.py + push/run.py): run-log
discipline, reserve-then-push dedup, the alert tick, and the briefing run. No
network (fake poster) and no LLM (fake/omitted router)."""

import json

import pytest

from ziggurat.push import outbound, run as push_run, runs


def _seed_own_team(conn, *, own_team_id=1):
    for tid, name in [(1, "My Squad"), (2, "Rivals Inc")]:
        conn.execute(
            "INSERT INTO league_teams (season, team_id, name, abbrev, primary_owner, "
            "retrieved_as_of, knowable_as_of) VALUES (2026, ?, ?, ?, ?, '2026-09-01', '2026-09-01')",
            (tid, name, f"AB{tid}", f"{{OWNER-{tid}}}"),
        )
    conn.commit()


def _snap(conn, day, espn_id, name, on_team, status, sp=1):
    conn.execute(
        "INSERT INTO league_player_state (season, espn_player_id, gsis_id, player, position, "
        "pro_team, on_team_id, injury_status, scoring_period, percent_owned, retrieved_as_of, "
        "knowable_as_of) VALUES (2026, ?, ?, ?, 'RB', 'ATL', ?, ?, ?, 5.0, ?, ?)",
        (str(espn_id), f"00-000{espn_id}", name, on_team, status, sp, day, day),
    )


def _cfg():
    return outbound.NtfyConfig(server="https://ntfy.sh", topic="zig-secret", token=None)


# ------------------------------------------------------------------ runs.py


def test_run_log_start_finish(push_db):
    rid = runs.start_run(push_db, kind="alert", season=2026, scope="events", started_at="2026-09-10T06:00:00")
    row = runs.last_run(push_db, kind="alert")
    assert row["status"] == runs.STATUS_RUNNING and row["run_id"] == rid
    runs.finish_run(push_db, rid, status=runs.STATUS_EMPTY, finished_at="2026-09-10T06:00:05",
                    events_found=0, events_pushed=0)
    assert runs.last_run(push_db, kind="alert")["status"] == runs.STATUS_EMPTY


def test_reap_orphans(push_db):
    runs.start_run(push_db, kind="brief", season=2026, scope="w2", started_at="2026-09-10T00:00:00")
    reaped = runs.reap_orphans(push_db, now="2026-09-10T06:00:00")  # 6h later > 1h threshold
    assert reaped == 1
    assert runs.last_run(push_db, kind="brief")["status"] == runs.STATUS_ABANDONED


def test_reserve_is_idempotent(push_db):
    kw = dict(season=2026, dedup_key="inj:100:2026-09-10:ruled_out", channel="phone",
              kind="INJURY_OUT", espn_player_id="100", event_day="2026-09-10",
              first_seen_at="2026-09-10T06:00:00", payload_summary="x")
    assert runs.reserve(push_db, **kw) is True   # first wins
    assert runs.reserve(push_db, **kw) is False  # second is a no-op
    assert runs.already_seen(push_db, season=2026, dedup_key=kw["dedup_key"], channel="phone")


# ------------------------------------------------------------------ alert tick


def test_alert_tick_pushes_own_player_and_dedups(push_db):
    _seed_own_team(push_db)
    _snap(push_db, "2026-09-09", 100, "Star Back", 1, "ACTIVE")
    _snap(push_db, "2026-09-10", 100, "Star Back", 1, "OUT")
    push_db.commit()
    sent = []
    poster = lambda url, body, headers, timeout: (sent.append(body.decode()) or 200)

    r1 = push_run.run_alert_tick(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                                 now="2026-09-10T06:00:00", week=2, pull_news=False,
                                 config=_cfg(), poster=poster)
    assert r1["status"] == runs.STATUS_OK and r1["pushed"] == 1
    assert any("YOUR Star Back" in s for s in sent)

    # Second tick: same transition -> deduped, nothing new pushed, EMPTY status.
    sent.clear()
    r2 = push_run.run_alert_tick(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                                 now="2026-09-10T06:20:00", week=2, pull_news=False,
                                 config=_cfg(), poster=poster)
    assert sent == [] and r2["pushed"] == 0


def test_content_blocked_alert_is_recorded_not_crashed_or_retried(push_db):
    # audit D2/D4: if the Rule-5 scrub RAISES on an event (e.g. a colleague team is
    # named exactly like the injured player), the tick must NOT crash the whole run
    # and must record the block as reserved-not-pushed so it does not retry forever.
    _seed_own_team(push_db)  # team 2 = "Rivals Inc"
    # add a colleague team whose NAME collides with the injured player's name.
    push_db.execute(
        "INSERT INTO league_teams (season, team_id, name, abbrev, primary_owner, "
        "retrieved_as_of, knowable_as_of) VALUES (2026, 3, 'Star Back', 'SB', '{X}', "
        "'2026-09-01', '2026-09-01')")
    _snap(push_db, "2026-09-09", 100, "Star Back", 1, "ACTIVE")
    _snap(push_db, "2026-09-10", 100, "Star Back", 1, "OUT")
    push_db.commit()
    sent = []
    r = push_run.run_alert_tick(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                                now="2026-09-10T06:00:00", week=2, pull_news=False,
                                config=_cfg(), poster=lambda u, b, h, t: (sent.append(b) or 200))
    assert r["status"] == runs.STATUS_PARTIAL and r["pushed"] == 0 and sent == []
    row = push_db.execute(
        "SELECT payload_summary, pushed_at FROM alert_ledger "
        "WHERE dedup_key = 'inj:100:2026-09-10:ruled_out'").fetchone()
    assert row is not None and row["pushed_at"] is None  # reserved as blocked, will not retry
    assert row["payload_summary"].startswith("BLOCKED")  # and never records the private reason


def test_alert_tick_empty_is_healthy(push_db):
    _seed_own_team(push_db)
    r = push_run.run_alert_tick(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                                now="2026-09-10T06:00:00", week=2, pull_news=False,
                                config=_cfg(), poster=lambda *a: 200)
    assert r["status"] == runs.STATUS_EMPTY and r["found"] == 0
    assert runs.last_run(push_db, kind="alert")["status"] == runs.STATUS_EMPTY


def test_alert_tick_writes_appendonly_log(push_db, tmp_path, monkeypatch):
    monkeypatch.setattr(push_run, "ALERTS_DIR", tmp_path / "alerts")
    _seed_own_team(push_db)
    _snap(push_db, "2026-09-09", 100, "Star Back", 1, "ACTIVE")
    _snap(push_db, "2026-09-10", 100, "Star Back", 1, "OUT")
    push_db.commit()
    push_run.run_alert_tick(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                            now="2026-09-10T06:00:00", week=2, pull_news=False,
                            config=_cfg(), poster=lambda *a: 200)
    log = (tmp_path / "alerts" / "2026-w02.jsonl")
    assert log.exists()
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["new"] >= 1 and rec["events"][0]["player"] == "Star Back"


def test_alert_tick_infra_failure_does_not_consume_the_event(push_db):
    # An INFRA send failure (ntfy down) must NOT write the ledger, so the event
    # retries on the next tick (publish-then-record: reserve only after a confirmed
    # send). This is the audit-D4 fix — a transient outage cannot permanently drop.
    _seed_own_team(push_db)
    _snap(push_db, "2026-09-09", 100, "Star Back", 1, "ACTIVE")
    _snap(push_db, "2026-09-10", 100, "Star Back", 1, "OUT")
    push_db.commit()

    def failing(url, body, headers, timeout):
        raise OSError("down")

    r = push_run.run_alert_tick(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                                now="2026-09-10T06:00:00", week=2, pull_news=False,
                                config=_cfg(), poster=failing)
    assert r["status"] == runs.STATUS_PARTIAL and r["pushed"] == 0
    # NO ledger row: the event is not consumed and will retry.
    row = push_db.execute(
        "SELECT 1 FROM alert_ledger WHERE dedup_key = 'inj:100:2026-09-10:ruled_out'"
    ).fetchone()
    assert row is None

    # Next tick with a working poster delivers it.
    sent = []
    r2 = push_run.run_alert_tick(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                                 now="2026-09-10T06:20:00", week=2, pull_news=False,
                                 config=_cfg(), poster=lambda u, b, h, t: (sent.append(b) or 200))
    assert r2["pushed"] == 1 and sent


def test_dry_run_alert_tick_does_not_poison_the_ledger(push_db):
    # THE headline audit fix (D4/D8): a --no-push preview must be side-effect-free
    # on the dedup ledger, or the next REAL tick treats the event as already-seen
    # and never pushes it.
    _seed_own_team(push_db)
    _snap(push_db, "2026-09-09", 100, "Star Back", 1, "ACTIVE")
    _snap(push_db, "2026-09-10", 100, "Star Back", 1, "OUT")
    push_db.commit()

    # Dry run (push=False): computes + records the run, but writes NO ledger row.
    rd = push_run.run_alert_tick(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                                 now="2026-09-10T06:00:00", week=2, pull_news=False, push=False,
                                 config=_cfg(), poster=lambda *a: 200)
    assert push_db.execute("SELECT COUNT(*) FROM alert_ledger").fetchone()[0] == 0

    # The subsequent REAL tick must still deliver the event.
    sent = []
    rr = push_run.run_alert_tick(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                                 now="2026-09-10T06:20:00", week=2, pull_news=False, push=True,
                                 config=_cfg(), poster=lambda u, b, h, t: (sent.append(b) or 200))
    assert rr["pushed"] == 1 and any(b"Star Back" in s for s in sent)


# ------------------------------------------------------------------ briefing run


def test_run_briefing_writes_file_and_pushes_teaser(push_db, tmp_path, monkeypatch):
    monkeypatch.setattr(push_run, "BRIEFINGS_DIR", tmp_path / "briefings")
    _seed_own_team(push_db)
    _snap(push_db, "2026-09-10", 100, "Star Back", 1, "ACTIVE", sp=1)
    push_db.commit()
    sent = []
    poster = lambda url, body, headers, timeout: (sent.append((body.decode(), headers)) or 200)

    r = push_run.run_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                              now="2026-09-10T06:00:00", week=2, config=_cfg(), poster=poster)
    assert r["status"] in (runs.STATUS_OK, runs.STATUS_PARTIAL)
    assert r["artifact"] is not None and (tmp_path / "briefings").exists()
    files = list((tmp_path / "briefings").glob("2026-w02-briefing.md"))
    assert len(files) == 1 and "Ziggurat briefing" in files[0].read_text()
    # teaser pushed, allowlist-safe (counts, no names)
    assert len(sent) == 1
    body, headers = sent[0]
    assert "alert(s)" in body and headers["Title"] == "Ziggurat briefing"


def test_run_briefing_llm_prose_used_when_router_given(push_db, tmp_path, monkeypatch):
    monkeypatch.setattr(push_run, "BRIEFINGS_DIR", tmp_path / "briefings")
    _seed_own_team(push_db)
    push_db.commit()

    class FakeRouter:
        def complete(self, task, prompt, *, system=None):
            from ziggurat.llm import LLMResponse
            assert task == "morning_briefing"
            return LLMResponse(text="TWO MINUTE SUMMARY", task=task, tier="standard",
                               backend="claude_cli", model="sonnet")

    push_run.run_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                          now="2026-09-10T06:00:00", week=2, router=FakeRouter(),
                          config=_cfg(), poster=lambda *a: 200)
    text = list((tmp_path / "briefings").glob("*.md"))[0].read_text()
    assert "TWO MINUTE SUMMARY" in text


def test_run_briefing_survives_llm_failure(push_db, tmp_path, monkeypatch):
    monkeypatch.setattr(push_run, "BRIEFINGS_DIR", tmp_path / "briefings")
    _seed_own_team(push_db)
    push_db.commit()

    class BoomRouter:
        def complete(self, task, prompt, *, system=None):
            raise RuntimeError("token limit")

    r = push_run.run_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                              now="2026-09-10T06:00:00", week=2, router=BoomRouter(),
                              config=_cfg(), poster=lambda *a: 200)
    # LLM failed but the deterministic briefing still got written + pushed.
    assert r["status"] == runs.STATUS_PARTIAL
    assert list((tmp_path / "briefings").glob("*.md"))
