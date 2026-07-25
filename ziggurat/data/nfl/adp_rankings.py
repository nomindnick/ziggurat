"""Market consensus rankings ingestion (import_ff_rankings) — item 1.5.

FantasyPros Expert Consensus Rank (ECR) is the point-in-time *market* rank of a
player: the single free signal that spans the backtest window (spike 1.2). One
row = a player's rank for one ``ecr_type`` on one ``scrape_date``. The frame
carries several ranking flavors (``ro`` redraft-overall, ``rp`` redraft-
positional, dynasty/best-ball/superflex variants); every flavor is ingested and
the accessor filters by ``ecr_type`` (default ``ro``).

knowable_as_of is the FantasyPros ``scrape_date`` — a Friday scrape is knowable
before that week's games, so stamping the scrape day is leakage-safe.

Crosswalk: ECR is FantasyPros-keyed. ``base.ids_by_fantasypros`` resolves the
FantasyPros id to (gsis_id, espn_id) from the players table. DST rows carry a
FantasyPros *team* id that is absent from the player crosswalk, so they keep a
NULL gsis_id and are joined downstream by normalized team abbr (LAR->LA,
JAC->JAX via TEAM_ALIASES). ~6% of even the draftable range is unresolved for
skill players too; those rows are KEPT with a NULL gsis_id (never dropped) — the
count is surfaced via ``note_incomplete`` (NOT ``note_drops``: nothing was lost,
and reporting it as a drop inflated the ratio with rows that are in the table)
rather than silently assumed irrelevant.

IDP rows (DB/DL/LB) contaminate the overall ECR (a LB can rank top-5 overall) but
our league scores none of them, so they are dropped at ingest. ``pos_rank`` is
derived here by ECR order over the LEAGUE positions only, so a positional rank is
never polluted by an IDP sitting "ahead" of a startable player.

UPSTREAM SHIPS THE SAME PLAYER TWICE (fixed 2026-07-25, item 3.2c follow-up).
Dual-eligibility players appear more than once in one ``ecr_type`` list on one
``scrape_date`` — i.e. more than once on this table's PRIMARY KEY. Measured on
the live 2026-07-24 scrape: 179 such key groups across the whole frame, and
exactly ONE survives the IDP filter (Travis Hunter, WR/CB, fantasypros_id 26034,
``rp``). The consequence was not just a lost row: ``_assign_pos_rank`` numbered
240 WRs, ``INSERT OR REPLACE`` stored 239, and the published board had **no
rank 64** — every WR below 63 in ``rp`` read one rank better than the truth,
which is the number ``core/divergence.py`` turns into the delta its report leads
with. Duplicates are therefore folded BEFORE ranking (``_dedupe_on_key``), the
survivor is chosen by a stated rule rather than by upstream's row order, the loss
is reported through ``base.note_collapsed``, and the resulting board is asserted
contiguous before it is stored.
"""

from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

# Positions our league actually starts. Everything else (DB/DL/LB IDP) is dropped.
LEAGUE_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DST"})

#: The stored PRIMARY KEY (migration 003). Passed to ``base.upsert`` so the count
#: it returns is DISTINCT KEYS WRITTEN rather than rows offered — and so that
#: ``base.upsert`` validates this tuple against the declared key on every ingest,
#: which is what stops ``_dedupe_on_key`` from silently drifting off the key
#: SQLite actually enforces.
_PK_COLS = ("fantasypros_id", "ecr_type", "scrape_date", "retrieved_as_of")

# Source columns we require (fail loud if an upstream release drops one).
_REQUIRED = (
    "id", "player", "pos", "team", "ecr_type",
    "ecr", "sd", "best", "worst", "player_owned_avg", "scrape_date",
)

# db_column -> source_column for the straight-through fields (derived columns
# gsis_id/espn_id/season/pos_rank are added per-row after the crosswalk join).
_COLMAP = {
    "fantasypros_id": "id",
    "player": "player",
    "position": "pos",
    "team": "team",
    "ecr_type": "ecr_type",
    "ecr": "ecr",
    "sd": "sd",
    "best": "best",
    "worst": "worst",
    "player_owned_avg": "player_owned_avg",
    "scrape_date": "scrape_date",
}


def _coerce_fp_id(value):
    """FantasyPros id arrives as an int64; store the bare digit string so it
    matches players.fantasypros_id (players.py normalizes its side the same way)."""
    if value is None:
        return None
    if isinstance(value, float):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


class PosRankDiscontinuity(ValueError):
    """The derived positional board has a hole in it (see ``_check_pos_rank_contiguous``)."""


def _survivor_rank(row) -> tuple:
    """Sort key deciding WHICH of several rows on one primary key survives.
    Lowest tuple wins, and the order is TOTAL (the last element is a canonical
    rendering of the whole row), so the winner never depends on the order
    upstream happened to ship the rows in — the ``base.gsis_by_pfr`` failure
    class, where SQLite's scan order silently picked the answer.

    THE RULE: keep the row that reports the WIDER expert dispersion. Measured on
    the live 2026-07-24 scrape, the one surviving collision looks like this::

        26034 Travis Hunter WR JAC rp  ecr=66.00  sd= 1.00  best=65  worst= 67
        26034 Travis Hunter WR JAC rp  ecr=66.09  sd=12.36  best=38  worst=112

    Three reasons for the wide row, in the order they matter:

    1. INFORMATION. ``sd``/``best``/``worst`` describe how far apart the expert
       panel is. A 65-67 band on a rookie two-way player is a handful of graders;
       38-112 is the full panel disagreeing, which is the true state of that
       market. The narrow row is a partial aggregation of the same consensus.
    2. RULE 6 — fail toward disclosure. If this rule ever picks wrong, the wide
       row merely over-states uncertainty; the narrow one under-states it. An
       ``sd`` of 1.00 on the single most contested player on the board is exactly
       the confident-sounding number a novice cannot smell.
    3. CONTINUITY. The rows already published for 2026-07-24/25 carry ecr 66.09,
       so this rule does not silently restate a market fact already stored. (The
       two ``ecr`` values differ by 0.09, so ``pos_rank`` order is identical
       either way — the choice is entirely about the uncertainty columns.)
    """
    sd = row.get("sd")
    best, worst = row.get("best"), row.get("worst")
    spread = (worst - best) if (best is not None and worst is not None) else None
    ecr = row.get("ecr")
    return (
        0 if sd is not None else 1,                   # reporting dispersion at all beats not
        -(sd if sd is not None else 0.0),             # then the wider sd
        -(spread if spread is not None else 0.0),     # then the wider best..worst band
        ecr if ecr is not None else float("inf"),     # then the better (lower) ECR
        repr(sorted((k, repr(v)) for k, v in row.items())),   # then: a total order
    )


def _dedupe_on_key(rows: list[dict]) -> list[dict]:
    """Fold rows that share this table's PRIMARY KEY down to one, BEFORE ranking.

    Order matters here and is the whole point. ``INSERT OR REPLACE`` would fold
    them anyway — silently, after ``_assign_pos_rank`` had already numbered the
    doomed row — which is how the stored board ended up missing a positional
    rank. Folding first means ``_assign_pos_rank`` numbers exactly the rows that
    will exist. The loss is reported on ``base.note_collapsed``'s channel (it
    reaches ``run_ingest``'s drop ceiling and the run log), not on ``note_drops``:
    nothing failed to stamp, a real market fact was discarded.

    Rows with a NULL in any key column pass through untouched — SQLite's PK index
    treats every NULL as distinct and stores them all, so folding them here would
    delete rows the database would have kept (see ``base.upsert``). That branch is
    UNREACHABLE through the shipped path today, and deliberately kept anyway: three
    of the four key columns are NOT NULL, and the fourth (``scrape_date``, which IS
    nullable in the DDL) is saved only by ``knowable_as_of`` — a DIFFERENT column —
    being NOT NULL and derived from it. That is precisely the coupling a later
    migration severs without noticing.

    Relative row order is preserved: a group's survivor is emitted where the
    group's first member was.
    """
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = tuple(row[c] for c in _PK_COLS)
        if any(v is None for v in key):
            continue
        groups.setdefault(key, []).append(row)

    winners = {key: min(group, key=_survivor_rank) for key, group in groups.items()}
    collapsed = duplicated = 0
    for key, group in groups.items():
        for row in group:
            if row is winners[key]:
                continue
            # Full-row equality separates "upstream shipped the row twice"
            # (nothing lost) from "two different facts, one discarded".
            if dict(row) == dict(winners[key]):
                duplicated += 1
            else:
                collapsed += 1
    base.note_collapsed("adp_rankings", collapsed, duplicated, len(rows))

    kept: list[dict] = []
    emitted: set[tuple] = set()
    for row in rows:
        key = tuple(row[c] for c in _PK_COLS)
        if any(v is None for v in key):
            kept.append(row)
            continue
        if key in emitted:
            continue
        emitted.add(key)
        kept.append(winners[key])
    return kept


def _check_pos_rank_contiguous(rows) -> None:
    """Post-condition: every (ecr_type, scrape_date, position) board is 1..n.

    A hole in a positional board is a Rule 6 problem, not an accounting one:
    ``core/divergence.py`` differences ``pos_rank`` against the ESPN board and
    leads its report with that number, so one missing rank shifts every player
    below it by one and the report reads perfectly normal while being wrong.

    RAISES rather than storing a discontinuous board, and that is a deliberate
    trade against this source's perishability (FantasyPros serves today's scrape
    only, so a refused run loses the day permanently). The reasoning: after
    ``_dedupe_on_key`` the property holds by construction — every ranked row is a
    distinct primary key and every distinct key is stored — so this can only fire
    on a future code defect, where publishing a wrong board is worse than losing
    one day of a market signal that moves slowly. It is also not the permanent-nag
    shape: tomorrow's pull is an independent scrape, so a bad day does not poison
    the next one.
    """
    boards: dict[tuple, list[int]] = {}
    for row in rows:
        boards.setdefault(
            (row["ecr_type"], row["scrape_date"], row["position"]), []
        ).append(row["pos_rank"])
    for key, ranks in boards.items():
        expected = list(range(1, len(ranks) + 1))
        if sorted(ranks) != expected:
            missing = sorted(set(expected) - set(ranks))
            raise PosRankDiscontinuity(
                f"adp_rankings: positional board {key} holds {len(ranks)} rows but its "
                f"pos_rank values are not 1..{len(ranks)} (missing {missing}, "
                f"max {max(ranks)}) — a board with a hole in it shifts every player "
                "below the hole by one in the divergence report; refusing to store it"
            )


def _assign_pos_rank(rows) -> None:
    """Derive pos_rank in place: within each (ecr_type, scrape_date, position),
    order by ecr ascending (lower ECR = better) and number 1..n. IDP rows are
    already gone, so the positional rank spans league positions only."""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["ecr_type"], row["scrape_date"], row["position"])
        groups.setdefault(key, []).append(row)
    for group in groups.values():
        # None ECR sorts last so a ranked player always precedes an unranked one.
        group.sort(key=lambda r: (r["ecr"] is None, r["ecr"] if r["ecr"] is not None else 0.0))
        for i, row in enumerate(group, start=1):
            row["pos_rank"] = i


def ingest_adp_rankings(conn, df, *, retrieved_as_of: str) -> int:
    """Persist market ECR rankings, stamping knowable_as_of with the scrape date.

    Drops IDP rows (not startable in our league); resolves gsis_id/espn_id via the
    FantasyPros crosswalk (DST + unresolved keep NULL gsis_id); normalizes team via
    TEAM_ALIASES; derives pos_rank by ECR order over league positions only.
    """
    base.require_columns(df, _REQUIRED, source="adp_rankings")
    crosswalk = base.ids_by_fantasypros(conn)

    rows = base.frame_to_rows(
        df,
        _COLMAP,
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=lambda src: base.iso_date(src.get("scrape_date")),
    )

    kept: list[dict] = []
    for row in rows:
        if row["position"] not in LEAGUE_POSITIONS:
            continue  # IDP — dropped below (counted by kept-vs-total)
        row["fantasypros_id"] = _coerce_fp_id(row["fantasypros_id"])
        if row["team"] is not None:
            row["team"] = base.TEAM_ALIASES.get(row["team"], row["team"])
        gsis, espn = crosswalk.get(row["fantasypros_id"], (None, None))
        row["gsis_id"] = gsis
        row["espn_id"] = espn
        row["season"] = int(row["scrape_date"][:4]) if row["scrape_date"] else None
        kept.append(row)

    idp_dropped = len(rows) - len(kept)
    base.note_drops(
        "adp_rankings", idp_dropped, len(rows),
        why="IDP position (not startable)", by_design=True,
    )

    # BEFORE ranking, never after: upstream ships dual-eligibility players twice
    # on one primary key (module docstring), and a rank handed to a row that
    # INSERT OR REPLACE then discards is a hole in the published board.
    kept = _dedupe_on_key(kept)

    unresolved = sum(
        1 for row in kept if row["gsis_id"] is None and row["position"] != "DST"
    )
    base.note_incomplete(
        "adp_rankings", unresolved, len(kept),
        why="unresolved FantasyPros crosswalk id (kept, NULL gsis_id)",
    )

    _assign_pos_rank(kept)
    _check_pos_rank_contiguous(kept)
    return base.upsert(conn, "adp_rankings", kept, key_cols=_PK_COLS)


def pull_adp_rankings(conn, *, retrieved_as_of: str) -> int:
    """Pull the current FantasyPros ECR scrape and store it. ``nfl.import_ff_rankings``
    is the seam cached-fixture tests patch. The endpoint carries a single current
    scrape_date; a weekly panel accumulates by pulling every week (dedup the in-
    flight edge-week scrape) or the Phase-4 db_fpecr backfill into this table."""
    df = nfl.import_ff_rankings()
    return ingest_adp_rankings(conn, df, retrieved_as_of=retrieved_as_of)


def get_adp_rankings(
    conn,
    *,
    as_of,
    season=None,
    position=None,
    ecr_type="ro",
    view: base.AsOfView = "historical",
):
    """Market ranking rows knowable on or before ``as_of`` (keyword-only; no
    implicit now). Defaults to the redraft-overall (``ro``) ECR; pass
    ``ecr_type=None`` to read every flavor. Backtest reads go through
    ``base.latest_truth(get_adp_rankings)``."""
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if position is not None:
        clauses.append("t.position = :position")
        params["position"] = position
    if ecr_type is not None:
        clauses.append("t.ecr_type = :ecr_type")
        params["ecr_type"] = ecr_type
    return base.select_as_of(
        conn, "adp_rankings", as_of=as_of,
        key_cols=["fantasypros_id", "ecr_type", "scrape_date"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
