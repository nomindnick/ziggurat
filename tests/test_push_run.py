"""Item 3.6 — the push orchestration layer (push/runs.py + push/run.py): run-log
discipline, reserve-then-push dedup, the alert tick, and the briefing run. No
network (fake poster) and no LLM (fake/omitted router)."""

from pathlib import Path
import json

from ziggurat.push import outbound, runs
from ziggurat.push import run as push_run


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


def test_alert_tick_never_pushes_context_news(push_db):
    """Operator decision 2026-08-05: the phone lane is action-only. A news story
    about a free agent is computed for the alert log but NOT pushed and NOT
    ledgered (it may still surface in the briefing), and a tick holding only
    such events is honestly EMPTY."""
    from ziggurat.data.nfl import news

    _seed_own_team(push_db)
    push_db.execute(
        "INSERT INTO players (gsis_id, espn_id, name, position, retrieved_as_of, knowable_as_of) "
        "VALUES ('00-000500', '500', 'Camp Hero', 'WR', '2026-09-01', '2026-09-01')")
    _snap(push_db, "2026-09-10", 500, "Camp Hero", None, "ACTIVE")  # a FREE AGENT
    push_db.commit()
    payload = {"articles": [{
        "id": 902, "type": "Story", "headline": "Camp Hero turning heads at practice",
        "description": "Feature piece.", "published": "2026-09-10T12:00:00Z",
        "links": {"web": {"href": "x"}},
        "categories": [{"type": "athlete", "athleteId": 500, "description": "Camp Hero"}],
    }]}
    news.pull_news(push_db, retrieved_as_of="2026-09-10", fetch=lambda limit: payload)

    sent = []
    r = push_run.run_alert_tick(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                                now="2026-09-10T13:00:00", week=2, pull_news=False,
                                config=_cfg(), poster=lambda u, b, h, t: (sent.append(b) or 200))
    assert sent == [] and r["pushed"] == 0
    assert r["status"] == runs.STATUS_EMPTY  # a context-only tick is healthy-empty
    assert push_db.execute(
        "SELECT 1 FROM alert_ledger WHERE dedup_key = 'news:espn:902'").fetchone() is None


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


def test_run_briefing_supplies_the_episode_history_provider(push_db, tmp_path, monkeypatch):
    """Item 4.2b audit DC-2 carried forward: the Wednesday briefing is a surface
    the NEW / REPEAT badge renders on, so push/run.py must hand the composer the
    archive reader (a callable) — otherwise every row reads FIRST SEEN forever."""
    from ziggurat.core import briefing as briefing_mod
    monkeypatch.setattr(push_run, "BRIEFINGS_DIR", tmp_path / "briefings")
    _seed_own_team(push_db)
    _snap(push_db, "2026-09-10", 100, "Star Back", 1, "ACTIVE", sp=1)
    push_db.commit()
    captured = {}

    def fake_build(conn, **kw):
        captured.update(kw)
        raise RuntimeError("stop after capturing kwargs")

    monkeypatch.setattr(briefing_mod, "build_briefing", fake_build)
    push_run.run_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                          now="2026-09-10T06:00:00", week=2, config=_cfg(),
                          poster=lambda *a, **k: 200, push=False)
    assert callable(captured.get("history")), sorted(captured)


# ------------------------------------------------------------------ briefing mirror (operator request 2026-09-08)


def test_run_briefing_mirrors_the_full_file_when_a_mirror_dir_is_set(push_db, tmp_path, monkeypatch):
    """The phone teaser carries no names; the FULL briefing is mirrored into the
    operator's own private directory (an Obsidian vault that syncs to the phone)."""
    monkeypatch.setattr(push_run, "BRIEFINGS_DIR", tmp_path / "briefings")
    _seed_own_team(push_db)
    _snap(push_db, "2026-09-10", 100, "Star Back", 1, "ACTIVE", sp=1)
    push_db.commit()
    vault = tmp_path / "vault" / "Ziggurat"      # does not exist yet — must be created
    sent = []
    r = push_run.run_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                              now="2026-09-10T06:00:00", week=2, config=_cfg(),
                              poster=lambda u, b, h, t: (sent.append(b) or 200), mirror_dir=vault)
    assert r["status"] == runs.STATUS_OK and r["error"] is None
    assert len(sent) == 1 and b"full briefing in Obsidian" in sent[0]    # the teaser says where it went
    assert r["mirror"] == str(vault / "2026-w02-briefing.md")
    assert (vault / "2026-w02-briefing.md").read_bytes() == (tmp_path / "briefings" / "2026-w02-briefing.md").read_bytes()
    # re-running the same week REPLACES the copy (one file per week in the vault)
    push_run.run_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                          now="2026-09-10T06:05:00", week=2, config=_cfg(),
                          poster=lambda *a, **k: 200, mirror_dir=vault)
    assert [p.name for p in vault.iterdir()] == ["2026-w02-briefing.md"]


def test_run_briefing_mirror_failure_is_partial_and_never_costs_the_push(push_db, tmp_path, monkeypatch):
    monkeypatch.setattr(push_run, "BRIEFINGS_DIR", tmp_path / "briefings")
    _seed_own_team(push_db)
    push_db.commit()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the vault directory should be")
    sent = []
    r = push_run.run_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                              now="2026-09-10T06:00:00", week=2, config=_cfg(),
                              poster=lambda u, b, h, t: (sent.append(b) or 200),
                              mirror_dir=blocker / "Ziggurat")
    assert r["status"] == runs.STATUS_PARTIAL
    assert "briefing mirror" in r["error"] and str(blocker) in r["error"]
    assert r["mirror"] is None
    assert (tmp_path / "briefings" / "2026-w02-briefing.md").exists()   # intel/ copy written first
    assert len(sent) == 1 and r["ntfy"] == "200"                        # teaser still went out
    assert b"on the box" in sent[0] and b"Obsidian" not in sent[0]       # and does not claim a mirror it lacks


def test_run_briefing_default_mirror_comes_from_the_environment(push_db, tmp_path, monkeypatch):
    monkeypatch.setattr(push_run, "BRIEFINGS_DIR", tmp_path / "briefings")
    _seed_own_team(push_db)
    push_db.commit()
    # unset -> no mirror, no error
    assert push_run.briefing_mirror_dir(environ={}) is None
    assert push_run.briefing_mirror_dir(environ={"BRIEFING_MIRROR_DIR": "  "}) is None
    assert push_run.briefing_mirror_dir(environ={"BRIEFING_MIRROR_DIR": "~/v"}) == Path("~/v").expanduser()
    monkeypatch.setattr(push_run, "briefing_mirror_dir", lambda: tmp_path / "from-env")
    r = push_run.run_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                              now="2026-09-10T06:00:00", week=2, config=_cfg(),
                              poster=lambda *a, **k: 200)
    assert r["mirror"] == str(tmp_path / "from-env" / "2026-w02-briefing.md")
    r = push_run.run_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                              now="2026-09-10T06:00:00", week=2, config=_cfg(),
                              poster=lambda *a, **k: 200, mirror_dir=None)
    assert r["mirror"] is None and r["status"] == runs.STATUS_OK


def test_alert_log_never_touches_operator_intel():
    """Pin for conftest's autouse `_isolate_alert_log`: under pytest the alert
    log path must sit outside the real `intel/` tree (2026-09-14: 2,564 fixture
    rows had landed in the operator's `intel/weekly/alerts/2026-w02.jsonl`)."""
    from ziggurat import paths

    assert not str(push_run.ALERTS_DIR).startswith(str(paths.INTEL_DIR))


# ------------------------------------------- brief status ordering (item 3.17)


def _finished_run(conn, *, kind, started_at, ntfy_status, status=runs.STATUS_OK,
                  artifact=None):
    rid = runs.start_run(conn, kind=kind, season=2026, scope="week 2", started_at=started_at)
    runs.finish_run(conn, rid, status=status, finished_at=started_at,
                    ntfy_status=ntfy_status, artifact_path=artifact)
    return rid


def test_dry_run_marker_matches_what_outbound_actually_stamps(push_db):
    """The ONLY durable mark separating a --no-push preview from a real send is
    the ntfy_status literal outbound writes. If outbound renames it, this bucket
    silently stops working and every preview reads as a real run again."""
    result = outbound.publish("body", conn=push_db, as_of="2026-09-16", season=2026,
                              own_team_id=1, config=_cfg(), dry_run=True,
                              poster=lambda *a, **k: 200)
    assert result.status == runs.DRY_RUN_NTFY


def test_brief_status_lists_the_real_run_above_the_dry_runs(push_db):
    """Item 3.17 d4: the Week-1 listing was one newest-first stream across both
    kinds, so three afternoon previews buried the real Wednesday 06:00 run. The
    REAL run must be findable at the TOP whatever order the rows went in."""
    _finished_run(push_db, kind="brief", started_at="2026-09-09T06:04:09",
                  ntfy_status="200", artifact="/intel/weekly/briefings/x.md")
    for stamp in ("2026-09-15T13:15:35", "2026-09-15T13:28:34", "2026-09-15T16:17:55"):
        _finished_run(push_db, kind="brief", started_at=stamp,
                      ntfy_status=runs.DRY_RUN_NTFY)

    out = runs.format_status(push_db, kind="brief")
    lines = out.splitlines()
    assert lines[0] == "last REAL [brief] run: 2026-09-09T06:04:09 -> ok"

    real_at = next(i for i, ln in enumerate(lines) if ln.startswith("REAL runs"))
    dry_at = next(i for i, ln in enumerate(lines) if ln.startswith("DRY-RUN previews"))
    assert real_at < dry_at
    # the real run sits inside the REAL block, above every preview
    real_row = next(i for i, ln in enumerate(lines)
                    if ln.startswith("  [") and "2026-09-09T06:04:09" in ln)
    assert real_at < real_row < dry_at
    assert all(i > dry_at for i, ln in enumerate(lines) if runs.DRY_RUN_NTFY in ln
               and ln.startswith("  ["))
    assert "DRY-RUN previews (--no-push; nothing was sent) — 3" in out


def test_brief_status_says_so_when_every_recorded_run_is_a_preview(push_db):
    """Three previews and no send is NOT a healthy week — the same trap as item
    3.7's `no push runs recorded yet` reading as healthy."""
    for stamp in ("2026-09-15T13:15:35", "2026-09-15T13:28:34"):
        _finished_run(push_db, kind="brief", started_at=stamp, ntfy_status=runs.DRY_RUN_NTFY)
    out = runs.format_status(push_db, kind="brief")
    assert out.splitlines()[0].startswith("no REAL run recorded yet")
    assert "(none)" in out


def test_brief_status_keeps_the_never_run_sentinel_the_cadence_quotes(push_db):
    """CLAUDE.md's preflight distinguishes this exact string from healthy-empty."""
    assert runs.format_status(push_db, kind="brief") == "no push runs recorded yet."


def test_alert_ticks_are_unaffected_because_an_empty_tick_pushes_nothing(push_db):
    """An empty alert tick records ntfy_status NULL, not 'dry_run' — it must stay
    in the REAL bucket, since 'nothing to push' is the healthy common case and
    not a preview."""
    _finished_run(push_db, kind="alert", started_at="2026-09-15T06:20:00",
                  ntfy_status=None, status=runs.STATUS_EMPTY)
    out = runs.format_status(push_db, kind="alert")
    assert out.splitlines()[0] == "last REAL [alert] run: 2026-09-15T06:20:00 -> empty"
    assert "DRY-RUN previews" not in out


def test_status_across_kinds_summarises_each_kind(push_db):
    _finished_run(push_db, kind="brief", started_at="2026-09-09T06:04:09", ntfy_status="200")
    _finished_run(push_db, kind="alert", started_at="2026-09-15T06:20:00", ntfy_status=None,
                  status=runs.STATUS_EMPTY)
    out = runs.format_status(push_db)
    assert "last REAL [alert] run: 2026-09-15T06:20:00 -> empty" in out
    assert "last REAL [brief] run: 2026-09-09T06:04:09 -> ok" in out
