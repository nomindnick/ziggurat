"""The suite never writes into the operator's real briefing mirror (item 3.6).

Pins conftest's autouse `_briefing_mirror_never_the_operator_vault`. Each test
points `ziggurat.paths.REPO_ROOT` at a temporary repo whose .env DOES name a
mirror, so the pin cannot pass on a fresh clone with no .env just because
there is nothing to read. Without the guard, every test here fails: the
default resolves to that temporary vault and the briefing is copied into it.
"""

import os

from ziggurat.push import outbound, runs
from ziggurat.push import run as push_run


def _repo_whose_env_names_a_mirror(tmp_path, monkeypatch):
    import ziggurat.paths as paths

    repo = tmp_path / "repo"
    repo.mkdir()
    vault = tmp_path / "vault" / "Ziggurat"
    (repo / ".env").write_text(f"BRIEFING_MIRROR_DIR={vault}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "REPO_ROOT", repo)
    return vault


def test_the_default_mirror_resolves_to_none_inside_the_suite(tmp_path, monkeypatch):
    vault = _repo_whose_env_names_a_mirror(tmp_path, monkeypatch)
    assert os.environ.get("BRIEFING_MIRROR_DIR") == ""
    assert push_run.briefing_mirror_dir() is None
    # the resolver itself still works when handed the value explicitly
    assert push_run.briefing_mirror_dir(environ={"BRIEFING_MIRROR_DIR": str(vault)}) == vault


def test_a_default_briefing_run_writes_nothing_into_the_mirror(push_db, tmp_path, monkeypatch):
    vault = _repo_whose_env_names_a_mirror(tmp_path, monkeypatch)
    monkeypatch.setattr(push_run, "BRIEFINGS_DIR", tmp_path / "briefings")
    for tid, name in [(1, "My Squad"), (2, "Rivals Inc")]:  # the outbound scrub needs a snapshot
        push_db.execute(
            "INSERT INTO league_teams (season, team_id, name, abbrev, primary_owner, "
            "retrieved_as_of, knowable_as_of) VALUES (2026, ?, ?, ?, ?, '2026-09-01', '2026-09-01')",
            (tid, name, f"AB{tid}", f"{{OWNER-{tid}}}"),
        )
    push_db.commit()
    sent = []
    r = push_run.run_briefing(
        push_db, as_of="2026-09-10", season=2026, own_team_id=1,
        now="2026-09-10T06:00:00", week=2,
        config=outbound.NtfyConfig(server="https://ntfy.sh", topic="zig-secret", token=None),
        poster=lambda u, b, h, t: (sent.append(b) or 200),
    )  # no mirror_dir: the default path, as the real briefing tests call it
    assert r["mirror"] is None and r["status"] == runs.STATUS_OK, r["error"]
    assert not vault.exists() and not vault.parent.exists()
    assert (tmp_path / "briefings" / "2026-w02-briefing.md").exists()
    assert len(sent) == 1 and b"Obsidian" not in sent[0]
