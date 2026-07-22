"""Room-prior calibration from the 2025 ESPN draft (item 2.2).

Pure functions that recompute the mock-draft opponent-model priors (RoomPriors)
from the raw 2025 recon artifacts. Everything here is *fitting* code: it reads
immutable historical draft data and emits weak, evidence-cited priors that
``priors.py`` seeds its defaults from. Nothing here runs during a live sim.

The single prior draft (2025, the league's inaugural season) yields **aggregate**
room tendencies only — reach spread, autodraft share, position-run curve, K/DST
round windows, board-adherence — per the 2.2 recon verdict (§3c). Per-manager
personalization is not fittable at n=1; the ESPN-rank+noise backbone stays the
model's spine.

Design rules honored here:
  * Every function accepts EXPLICIT artifact paths (the module-level ``DEFAULT_*``
    constants only supply the real 2025 pulls under gitignored ``data/recon-2.2/``);
    tests pass tiny SYNTHETIC fixtures, so the stats are provable offline.
  * Position is ALWAYS joined from the kona universe's ``defaultPositionId`` — the
    pick's ``lineupSlotId`` is NEVER read (it conflates FLEX/bench and is the
    destination slot, not the player's position — recon §2).
  * Reach is measured for HUMAN picks only (``autoDraftTypeId == 0``) and SKILL
    positions only (K/DST excluded — the whole room defers them structurally, so
    "reach" against an overall board is a roster-rule artifact, not aggression).
  * Reach is scored against BOTH the IDP-filtered, re-ranked db_fpecr ECR board
    (PRIMARY reference — the same FantasyPros lineage as the 2.1 live board, so
    2025-fit and 2026-live boards are commensurable) and the 2025 ESPN editorial
    PPR board (cross-check).

Rule 8: this module lives under ``ziggurat/draft/`` (deletable); nothing outside
``draft/`` imports it. It MAY read ``ziggurat/data`` helpers (team aliasing).
Rule 5: no colleague names / owner GUIDs / real team abbrevs are emitted — the
fitted output keys players by public name and reports only aggregate room stats.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from importlib import import_module
from pathlib import Path

import numpy as np

from ziggurat.data.nfl import base

# --------------------------------------------------------------------- constants

# defaultPositionId -> canonical league position (recon §2; matches espn_ranks.DEFPOS
# and scoring's position sets). D/ST label normalized to "DST" here for the priors.
DEFPOS: dict[int, str] = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
SKILL_POSITIONS = frozenset({"QB", "RB", "WR", "TE"})
DRAFTABLE_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DST"})

# reach = board_rank - overall_pick; POSITIVE => the player was taken EARLIER than
# the board had them (a "reach up"). Recon §3b convention.
REACH_SIGN_CONVENTION = "reach = board_rank - overall_pick_number; positive => reached up (taken earlier than the board)"

# Default real 2025 artifacts (gitignored). Resolved relative to the repo root so
# a CLI/loader edge can call with no arguments; tests always pass explicit paths.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RECON = _REPO_ROOT / "data" / "recon-2.2"
DEFAULT_PICKS_PATH = _RECON / "leagueHistory_2025_mDraftDetail_mTeam_mSettings.json"
DEFAULT_KONA_PATH = _RECON / "seasons2025_players_wl_ownership_all.json"
DEFAULT_EDITORIAL_PATH = _RECON / "seasons2025_editorial_ppr_board.json"
DEFAULT_FPECR_PATH = _RECON / "fpecr_2025_ro_redraft_overall_2025-08-29.parquet"

# --------------------------------------------------------------- normalization


_SUFFIX_RE = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?")


def normalize_name(name) -> str:
    """Lowercase, strip suffixes/punctuation/apostrophes for cross-source joins.

    "Ja'Marr Chase" -> "jamarr chase"; "Marvin Harrison Jr." -> "marvin harrison".
    """
    if not name:
        return ""
    s = str(name).lower().replace(".", "").replace("'", "").replace("-", " ")
    s = _SUFFIX_RE.sub("", s)
    s = re.sub(r"[^a-z ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_team(abbr) -> str | None:
    """Normalize a team abbr through ``base.TEAM_ALIASES`` (JAC->JAX, WSH->WAS,
    LAR->LA); FA/None/empty -> None so free agents never form a join key."""
    if abbr in (None, "None", "FA", "", 0):
        return None
    a = str(abbr).strip().upper()
    if a in ("FA", "NONE", ""):
        return None
    return base.TEAM_ALIASES.get(a, a)


def _pro_team_map() -> dict:
    """Lazy import of espn_api's proTeamId->abbr table (kept off the module import
    path, mirroring ``espn_ranks._pro_team_map``)."""
    return import_module("espn_api.football.constant").PRO_TEAM_MAP


# ------------------------------------------------------------------- loaders


def load_picks(path=DEFAULT_PICKS_PATH) -> list[dict]:
    """Load the 2025 draft picks as a flat list of normalized pick dicts.

    Reads ``[0].draftDetail.picks`` (the ``leagueHistory`` array-of-one form).
    Each returned dict: ``overall`` / ``round`` / ``pick_in_round`` / ``team_id``
    / ``player_id`` / ``auto_type`` / ``is_human`` (``auto_type == 0``). The
    pick's ``lineupSlotId`` is deliberately NOT surfaced (recon §2)."""
    raw = json.loads(Path(path).read_text())
    node = raw[0] if isinstance(raw, list) else raw
    picks = node["draftDetail"]["picks"]
    out = []
    for p in picks:
        auto = p.get("autoDraftTypeId", 0)
        out.append(
            {
                "overall": p["overallPickNumber"],
                "round": p["roundId"],
                "pick_in_round": p.get("roundPickNumber"),
                "team_id": p.get("teamId"),
                "player_id": p.get("playerId"),
                "auto_type": auto,
                "is_human": auto == 0,
            }
        )
    return out


def _iter_player_dicts(raw):
    """Yield inner player dicts from either the flat list form
    (``[{id, fullName, defaultPositionId, ...}]``) or the nested kona form
    (``{"players": [{"player": {...}}, ...]}``)."""
    if isinstance(raw, dict) and "players" in raw:
        for entry in raw["players"]:
            yield entry.get("player", entry)
    elif isinstance(raw, list):
        for entry in raw:
            yield entry
    else:  # pragma: no cover - malformed artifact
        raise ValueError("unrecognized kona-universe JSON shape")


def load_kona_universe(path=DEFAULT_KONA_PATH) -> dict[int, dict]:
    """player_id -> {name, position, team} from the kona/ownership universe.

    Position comes from ``defaultPositionId`` via ``DEFPOS`` (recon §2 — the
    position source, never the pick's lineupSlotId). Team comes from an explicit
    ``team`` abbr if present, else ``proTeamId`` via ``PRO_TEAM_MAP``, then
    ``normalize_team``. Players outside DEFPOS (IDP/FB/punter) still load (their
    position is None) so a caller can see them dropped rather than silently gone.
    """
    raw = json.loads(Path(path).read_text())
    out: dict[int, dict] = {}
    ptm = None
    for pl in _iter_player_dicts(raw):
        pid = pl.get("id")
        if pid is None:
            continue
        pos = DEFPOS.get(pl.get("defaultPositionId"))
        if "team" in pl and pl["team"] is not None:
            team = normalize_team(pl["team"])
        else:
            if ptm is None:
                ptm = _pro_team_map()
            team = normalize_team(ptm.get(pl.get("proTeamId")))
        out[pid] = {
            "name": pl.get("fullName") or pl.get("name"),
            "position": pos,
            "team": team,
        }
    return out


def load_editorial_board(path=DEFAULT_EDITORIAL_PATH) -> dict[int, dict]:
    """player_id -> {rank, position, name} from the 2025 ESPN editorial PPR board.

    The flattened board carries ``{id, name, pos (defaultPositionId), ppr_rank}``;
    the rank is ESPN's own overall editorial recommendation (rankSourceId=0)."""
    raw = json.loads(Path(path).read_text())
    out: dict[int, dict] = {}
    for r in raw:
        pid = r.get("id")
        if pid is None:
            continue
        out[pid] = {
            "rank": r.get("ppr_rank"),
            "position": DEFPOS.get(r.get("pos")),
            "name": r.get("name"),
        }
    return out


def load_fpecr_board(path=DEFAULT_FPECR_PATH) -> dict:
    """Load the db_fpecr 'ro' redraft-overall board, IDP-filtered and RE-RANKED.

    The raw 'ro' board is IDP-contaminated (LB/DL/DB rows, e.g. LB Zaire Franklin
    at overall 2), so it is filtered to {QB,RB,WR,TE,K,DST} and re-ranked 1..n by
    ECR before use as a draftable-universe board (recon §3c build nuance).

    Returns a dict with:
      * ``by_name_team``: (norm_name, norm_team) -> best (lowest) re-rank
      * ``by_name``: norm_name -> best re-rank, only for names UNIQUE in the board
        (a safe fallback for picks whose pick-side team drifted post-draft)
      * ``rows``: list of {name, position, team, ecr, rank}
      * ``coverage``: {rows_before, rows_after_idp_filter, idp_removed,
        dup_name_team_keys, ambiguous_names}
    """
    import pandas as pd

    df = pd.read_parquet(path)
    before = len(df)
    df = df[df["pos"].isin(DRAFTABLE_POSITIONS)].copy()
    df = df.sort_values("ecr", kind="stable").reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)

    by_name_team: dict[tuple, int] = {}
    dup_name_team = 0
    name_ranks: dict[str, list[int]] = defaultdict(list)
    rows = []
    for _, r in df.iterrows():
        nm = normalize_name(r["player"])
        tm = normalize_team(r["team"])
        rank = int(r["rank"])
        rows.append(
            {"name": r["player"], "position": r["pos"], "team": tm, "ecr": float(r["ecr"]), "rank": rank}
        )
        key = (nm, tm)
        if key in by_name_team:
            dup_name_team += 1
            by_name_team[key] = min(by_name_team[key], rank)  # keep best rank
        else:
            by_name_team[key] = rank
        name_ranks[nm].append(rank)

    # name-only fallback: keep only names that appear exactly once (unambiguous).
    by_name = {nm: rs[0] for nm, rs in name_ranks.items() if len(rs) == 1}
    ambiguous = sum(1 for rs in name_ranks.values() if len(rs) > 1)

    return {
        "by_name_team": by_name_team,
        "by_name": by_name,
        "rows": rows,
        "coverage": {
            "rows_before": before,
            "rows_after_idp_filter": len(df),
            "idp_removed": before - len(df),
            "dup_name_team_keys": dup_name_team,
            "ambiguous_names": ambiguous,
        },
    }


# ------------------------------------------------------------------- stats


def _robust_spread(values: list[float]) -> dict:
    """mean/std/MAD/robust_sigma/percentiles for a reach sample (empty -> zeros)."""
    if not values:
        return {
            "n": 0, "mean": None, "std": None, "median": None, "mad": None,
            "robust_sigma": None, "min": None, "max": None, "percentiles": {},
        }
    arr = np.asarray(values, dtype=float)
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    pct_levels = [5, 10, 25, 50, 75, 90, 95]
    pcts = np.percentile(arr, pct_levels)
    return {
        "n": int(arr.size),
        "mean": float(arr.mean()),
        "std": float(arr.std(ddof=0)),
        "median": med,
        "mad": mad,
        # 1.4826*MAD is the normal-consistent robust sigma estimate.
        "robust_sigma": float(1.4826 * mad),
        "min": float(arr.min()),
        "max": float(arr.max()),
        "percentiles": {f"p{lvl:02d}": float(v) for lvl, v in zip(pct_levels, pcts, strict=True)},
    }


def _pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) < 2:
        return None
    return float(np.corrcoef(np.asarray(x, dtype=float), np.asarray(y, dtype=float))[0, 1])


def reach_vs_editorial(picks, universe, editorial) -> dict:
    """Human, skill-only reach distribution against the ESPN editorial board.

    Reach is joined by ``player_id`` (the editorial board joins 160/160 picks).
    Only ``is_human`` picks whose universe position is a SKILL position are scored.
    Also returns the all-picks board-adherence Pearson (overall vs board rank)."""
    reaches: list[float] = []
    n_skill_human = 0
    matched = 0
    ov_all: list[float] = []
    rank_all: list[float] = []
    for p in picks:
        info = editorial.get(p["player_id"])
        if info and info["rank"] is not None:
            ov_all.append(p["overall"])
            rank_all.append(info["rank"])
        if not p["is_human"]:
            continue
        pos = universe.get(p["player_id"], {}).get("position")
        if pos not in SKILL_POSITIONS:
            continue
        n_skill_human += 1
        if info and info["rank"] is not None:
            matched += 1
            reaches.append(info["rank"] - p["overall"])
    stats = _robust_spread(reaches)
    stats.update(
        {
            "reference": "2025 ESPN editorial PPR board (draftRanksByRankType.PPR.rank)",
            "artifact": DEFAULT_EDITORIAL_PATH.name,
            "join_key": "player_id",
            "skill_human_picks": n_skill_human,
            "join_coverage": f"{matched}/{n_skill_human}",
            "board_adherence_pearson": _pearson(ov_all, rank_all),
            "board_adherence_n": len(ov_all),
        }
    )
    return stats


def reach_vs_fpecr(picks, universe, fpecr, *, name_fallback: bool = True) -> dict:
    """Human, skill-only reach distribution against the IDP-filtered db_fpecr board.

    Joined by (normalized name, normalized team). ``name_fallback`` recovers a
    pick that misses on team but whose name is UNIQUE in the board (pick-side team
    drift — e.g. a player traded after the board's draft-day snapshot). Reports
    strict name+team coverage and the count recovered by the name fallback. Also
    returns all-draftable board-adherence Pearson."""
    reaches: list[float] = []
    n_skill_human = 0
    matched_nameteam = 0
    matched_namefallback = 0
    ov_all: list[float] = []
    rank_all: list[float] = []
    by_nt = fpecr["by_name_team"]
    by_n = fpecr["by_name"]
    for p in picks:
        info = universe.get(p["player_id"], {})
        nm = normalize_name(info.get("name"))
        tm = info.get("team")
        rank = by_nt.get((nm, tm))
        used_fallback = False
        if rank is None and name_fallback:
            rank = by_n.get(nm)
            used_fallback = rank is not None
        if rank is not None:
            ov_all.append(p["overall"])
            rank_all.append(rank)
        if not p["is_human"]:
            continue
        pos = info.get("position")
        if pos not in SKILL_POSITIONS:
            continue
        n_skill_human += 1
        if rank is None:
            continue
        if used_fallback:
            matched_namefallback += 1
        else:
            matched_nameteam += 1
        reaches.append(rank - p["overall"])
    total_matched = matched_nameteam + matched_namefallback
    stats = _robust_spread(reaches)
    stats.update(
        {
            "reference": "db_fpecr 'ro' redraft-overall board, IDP-filtered + re-ranked",
            "artifact": DEFAULT_FPECR_PATH.name,
            "join_key": "normalized name + team (name-only fallback for unique names)",
            "skill_human_picks": n_skill_human,
            "matched_name_team": matched_nameteam,
            "matched_name_fallback": matched_namefallback,
            "join_coverage": f"{total_matched}/{n_skill_human}",
            "join_coverage_name_team_only": f"{matched_nameteam}/{n_skill_human}",
            "board_adherence_pearson": _pearson(ov_all, rank_all),
            "board_adherence_n": len(ov_all),
            "board_coverage": fpecr["coverage"],
        }
    )
    return stats


def autodraft_share(picks) -> dict:
    """Full-autodraft seat share + scattered single-pick autopick rate.

    A seat is full-autodraft when ALL its picks are ``autoDraftTypeId == 3``; the
    autodraft_fraction prior = full-auto seats / total seats. Type-2 picks are
    scattered single autopicks inside otherwise-human drafts (a separate, small
    noise rate)."""
    counts = Counter(p["auto_type"] for p in picks)
    per_seat: dict[int, Counter] = defaultdict(Counter)
    for p in picks:
        per_seat[p["team_id"]][p["auto_type"]] += 1
    total_seats = len(per_seat)
    full_auto = [t for t, c in per_seat.items() if c and set(c) == {3}]
    n_type2 = counts.get(2, 0)
    return {
        "auto_type_counts": {str(k): v for k, v in sorted(counts.items())},
        "total_seats": total_seats,
        "full_auto_seats": len(full_auto),
        "autodraft_fraction": (len(full_auto) / total_seats) if total_seats else None,
        "type2_scatter_picks": n_type2,
        "type2_rate": (n_type2 / len(picks)) if picks else None,
        "artifact": DEFAULT_PICKS_PATH.name,
    }


def position_run_curve(picks, universe) -> dict:
    """Per-round position frequencies from HUMAN picks only (recon §3c).

    Position is joined from the universe's ``defaultPositionId`` — NEVER the pick's
    lineupSlotId. Returns raw per-round counts and within-round fractions."""
    per_round: dict[int, Counter] = defaultdict(Counter)
    n_human = 0
    for p in picks:
        if not p["is_human"]:
            continue
        pos = universe.get(p["player_id"], {}).get("position")
        if pos is None:
            continue
        per_round[p["round"]][pos] += 1
        n_human += 1

    counts = {r: dict(per_round[r]) for r in sorted(per_round)}
    fractions = {}
    for r, c in counts.items():
        tot = sum(c.values())
        fractions[r] = {pos: round(n / tot, 4) for pos, n in c.items()}
    return {
        "human_only": True,
        "position_source": "defaultPositionId (kona universe); lineupSlotId NOT used",
        "n_human_picks": n_human,
        "counts_by_round": counts,
        "fractions_by_round": fractions,
        "artifact": f"{DEFAULT_PICKS_PATH.name} + {DEFAULT_KONA_PATH.name}",
    }


def kdst_windows(picks, universe) -> dict:
    """First-K / first-DST rounds and per-round K/DST clustering.

    ``all_picks`` includes autodrafted seats (K/DST deferral is a structural
    roster rule the whole room obeys, autodraft included); ``human`` restricts to
    human picks. ``kdst_earliest_round`` = earliest round a HUMAN took a K or DST
    (the no-K/DST-before-this floor prior)."""

    def _scan(subset):
        k_rounds, dst_rounds = [], []
        first_k = first_dst = None
        for p in sorted(subset, key=lambda x: x["overall"]):
            pos = universe.get(p["player_id"], {}).get("position")
            if pos == "K":
                k_rounds.append(p["round"])
                if first_k is None:
                    first_k = {"overall": p["overall"], "round": p["round"]}
            elif pos == "DST":
                dst_rounds.append(p["round"])
                if first_dst is None:
                    first_dst = {"overall": p["overall"], "round": p["round"]}
        return {
            "first_k": first_k,
            "first_dst": first_dst,
            "k_by_round": {r: n for r, n in sorted(Counter(k_rounds).items())},
            "dst_by_round": {r: n for r, n in sorted(Counter(dst_rounds).items())},
        }

    all_scan = _scan(picks)
    human_scan = _scan([p for p in picks if p["is_human"]])
    human_k_first = human_scan["first_k"]["round"] if human_scan["first_k"] else None
    human_dst_first = human_scan["first_dst"]["round"] if human_scan["first_dst"] else None
    earliest = [r for r in (human_k_first, human_dst_first) if r is not None]
    return {
        "all_picks": all_scan,
        "human": human_scan,
        "kdst_earliest_round": min(earliest) if earliest else None,
        "artifact": f"{DEFAULT_PICKS_PATH.name} + {DEFAULT_KONA_PATH.name}",
    }


# ---------------------------------------------------------------- orchestrator


def fit_room_priors(
    *,
    picks_path=DEFAULT_PICKS_PATH,
    kona_path=DEFAULT_KONA_PATH,
    editorial_path=DEFAULT_EDITORIAL_PATH,
    fpecr_path=DEFAULT_FPECR_PATH,
    primary_reference: str = "fpecr",
) -> dict:
    """Recompute the full RoomPriors fit from the raw 2025 artifacts.

    Returns a nested dict of every prior with its n, join coverage, and artifact
    citation — suitable for dumping to JSON for the integrator and for seeding
    ``priors.py`` defaults. ``primary_reference`` (``"fpecr"`` recommended, recon
    §3c) records which reach reference the shipped defaults should read.
    """
    picks = load_picks(picks_path)
    universe = load_kona_universe(kona_path)
    editorial = load_editorial_board(editorial_path)
    fpecr = load_fpecr_board(fpecr_path)

    reach_fpecr = reach_vs_fpecr(picks, universe, fpecr)
    reach_editorial = reach_vs_editorial(picks, universe, editorial)
    auto = autodraft_share(picks)
    curve = position_run_curve(picks, universe)
    windows = kdst_windows(picks, universe)

    primary = reach_fpecr if primary_reference == "fpecr" else reach_editorial

    return {
        "meta": {
            "source_draft": "2025 inaugural season (n=1 prior draft)",
            "n_picks": len(picks),
            "primary_reach_reference": primary_reference,
            "reach_sign_convention": REACH_SIGN_CONVENTION,
            "artifacts": {
                "picks": str(picks_path),
                "kona_universe": str(kona_path),
                "editorial_board": str(editorial_path),
                "fpecr_board": str(fpecr_path),
            },
            "caveat": (
                "Weak priors from a single prior draft with 2/10 seats fully "
                "autodrafted (8 human seats / 125 human picks). Aggregate room "
                "tendencies only; NOT per-manager strategies (recon §3c)."
            ),
        },
        "reach": {"fpecr": reach_fpecr, "editorial": reach_editorial},
        "recommended_priors": {
            # zero-centered noise: fpecr reach is ~symmetric (mean~0), so a
            # reach_sigma seed reads directly off its spread. editorial carries a
            # systematic +offset (ESPN-editorial-vs-market bias, not aggression).
            "reach_sigma": primary.get("std"),
            "reach_sigma_robust": primary.get("robust_sigma"),
            "reach_center": primary.get("mean"),
            "board_adherence": primary.get("board_adherence_pearson"),
            "autodraft_fraction": auto.get("autodraft_fraction"),
            "type2_rate": auto.get("type2_rate"),
            "kdst_earliest_round": windows.get("kdst_earliest_round"),
        },
        "autodraft": auto,
        "position_run_curve": curve,
        "kdst_windows": windows,
    }
