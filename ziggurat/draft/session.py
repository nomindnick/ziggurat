"""Headless draft-session controller for the item-2.4 board TUI.

DELETABLE package (Rule 8): lives under ``ziggurat/draft/`` and nothing outside
that package imports it. This is the pure, terminal-I/O-free state machine the
Rich render loop (``app.py``) drives: it owns snake bookkeeping, the crash-safe
JSONL journal, the recommendation call into the 2.3 engine, snake-turn
contingencies, and the live-recalibration honesty status. No ``print``, no
``input`` — every method is directly testable on a synthetic board (Rule 5).

Design anchors (``intel/research/tui-2.4-recon.md`` §2/§3):

* **Draft-position space keeps survival correct under a non-identity pick_order.**
  ``pick_order`` maps a round-1 draft-order position to a seat id. The 2.3
  survival rollout (``survival.upcoming_opponent_picks`` / ``_snake_team_at``)
  works purely in draft-position space assuming ``pick_order == range(teams)``.
  So when we build a :class:`PickContext` for the engine we key
  ``opponent_rosters`` by DRAFT POSITION (not seat id) and pass the operator's
  draft position as ``team_slot``. Because ``_snake_team_at`` is geometric on
  draft positions, ``picks_until_next`` and the intervening-pick set come out
  right for ANY permutation — the operator's real picks land at the right
  overalls (recon §3 MUST, regression-tested).

* **Fresh, state-seeded ctx per compute.** ``recommend()`` builds a brand-new
  ``PickContext`` with ``random.Random(session_seed ^ overall_pick)`` on EVERY
  call and never re-invokes the engine on a held ctx — reusing a ctx mutates
  ``ctx.rng`` and flickers survival numbers (recon §1, the refuted idempotence
  claim). Two calls on the same state are therefore bit-identical.

* **Append-only journal, fsync before ack, resume = replay.** ``append_pick``
  writes and ``os.fsync``s the pick line BEFORE it returns (before the UI
  acknowledges the pick); ``undo_last`` / ``edit_pick`` rewrite the whole log
  atomically (temp + fsync + ``os.replace``) and rebuild state by a full replay.
  ``resume`` validates the header against the passed board and replays to
  reproduce bit-identical state. One mechanism for correction and recovery.

Rule 1: no DB accessor here — the board is loaded once at the ``simulator.load_board``
edge and handed in. Rule 2: no scoring constant — value comes from the board's VOR.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure
from ziggurat.draft.bots import AutodraftBot, BoardEntry, PickContext
from ziggurat.draft.engine import PickEngine, PickRec
from ziggurat.draft.priors import RoomPriors
from ziggurat.draft.simulator import ROUNDS, snake_sequence
from ziggurat.draft.survival import (
    LIVE_RECAL_MIN_PICKS,
    LiveRecalibration,
    recalibrate_from_pick_log,
)

# ---------------------------------------------------- journal errors + discovery


class JournalExistsError(RuntimeError):
    """A fresh :meth:`DraftSession.start` would overwrite an existing journal.

    The journal is opened exclusive-create (``open(..., "x")`` → O_EXCL), so the
    classic draft-day footgun — terminal dies mid-draft, operator re-runs
    ``ziggurat draft-board`` WITHOUT ``--resume`` — refuses to truncate the
    confirmed picks instead of silently destroying them. The message is
    novice-legible and points at recovery (the app surfaces it verbatim).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        super().__init__(
            f"a draft journal already exists at {self.path} — refusing to "
            "overwrite it and lose the picks recorded there. To continue that "
            "draft, re-run with --resume; to start a brand-new draft, pass a "
            "different --journal path."
        )


def find_latest_journal(directory: Path) -> Path | None:
    """The newest ``session-*.jsonl`` in ``directory`` by name, or None if none.

    The default journal name is timestamped (``session-YYYYMMDD-HHMMSS.jsonl``),
    which sorts chronologically as a plain string, so the lexicographically-last
    match is the most recent session — the one a bare ``--resume`` should recover.
    Keyed on the name (not the calendar day), so a crash/resume across a midnight
    rollover still finds the right journal (recon §crash NEW-1).
    """
    directory = Path(directory)
    if not directory.is_dir():
        return None
    candidates = sorted(directory.glob("session-*.jsonl"))
    return candidates[-1] if candidates else None


def read_journal_header(path: Path) -> dict:
    """Parse and return a journal's session-header line (loud on missing/corrupt).

    The header is the first line of the JSONL journal (a ``{"kind": "header", …}``
    object carrying ``as_of`` / ``season`` / provenance). Errors are raised as
    novice-legible ``ValueError``s so the CLI resume path surfaces a sentence, not
    a traceback (recon §arch NEW-1).
    """
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline()
    except OSError as exc:
        raise ValueError(
            f"cannot read draft journal {path}: {exc} — there is no session to resume"
        ) from exc
    first = first.strip()
    if not first:
        raise ValueError(f"draft journal {path} is empty; there is no session to resume")
    try:
        header = json.loads(first)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"draft journal {path} has a corrupt header line; cannot resume"
        ) from exc
    if not isinstance(header, dict) or header.get("kind") != "header":
        raise ValueError(
            f"draft journal {path} does not start with a session header; cannot resume"
        )
    return header


def _fsync_dir(path: Path) -> None:
    """fsync the directory containing ``path`` so a create/rename is power-durable.

    POSIX: a file create or an ``os.replace`` rename is not durable across power
    loss until the parent directory itself is fsync'd (recon §crash F5). Best
    effort — silently skips where a directory fd cannot be opened/synced (some
    platforms/filesystems), which is the standard guard.
    """
    dir_path = Path(path).parent
    try:
        fd = os.open(dir_path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# --------------------------------------------------------------- journal records


@dataclass(frozen=True)
class PickRecord:
    """One confirmed pick as journalled and replayed.

    ``seat`` is the drafting seat id, derived from the snake position at entry. In
    a snake draft the seat is fully determined by ``overall``, so it is never
    edited independently (audit state F2 — a seat override could only silently
    desync the rosters from the snake order); it is stored explicitly to key the
    roster attribution on replay and to keep the journal human-readable. ``name``
    is the board name at entry time, carried for a human-readable journal.
    """

    overall: int
    seat: int
    player_id: str
    name: str | None


@dataclass(frozen=True)
class Contingency:
    """A snake-turn "if X now, then Y on the wheel" 2-ply branch (recon §3 MUST).

    ``first`` is a candidate for the current pick; ``wheel`` is the engine's best
    recommendation for the operator's immediate next (back-to-back) pick assuming
    ``first`` is taken. Both carry their own novice-legible ``reasons``.
    """

    first: PickRec
    wheel: PickRec

    @property
    def message(self) -> str:
        fn = self.first.name or self.first.player_id
        wn = self.wheel.name or self.wheel.player_id
        return (
            f"If you take {fn} now, {wn} is the likely best value waiting for you "
            f"on your very next pick."
        )


@dataclass(frozen=True)
class RecalibrationStatus:
    """Session-level view of the live room re-fit, with the honesty fields (Rule 6).

    Wraps :class:`~ziggurat.draft.survival.LiveRecalibration` and adds the
    threshold so the display can always say "picks seen / picks needed" — never
    imply an adaptation that has not happened. ``engaged`` is the honesty flag:
    False means the engine is still on the 2025 baseline plus kappa-widening.
    """

    engaged: bool
    n_room_picks: int
    min_room_picks: int
    picks_needed: int
    reach_sigma: float | None
    reach_center: float | None
    board_adherence_pearson: float | None
    priors: RoomPriors

    @property
    def message(self) -> str:
        if self.engaged:
            sig = f"{self.reach_sigma:.0f}" if self.reach_sigma is not None else "?"
            return (
                f"Room model: adapted from {self.n_room_picks} live picks so far "
                f"(the room reaches roughly {sig} spots off its board)."
            )
        return (
            f"Room model: still on the 2025 baseline — {self.picks_needed} more "
            f"room picks needed before it adapts (seen {self.n_room_picks} so far)."
        )


def _board_hash(board: Sequence[BoardEntry]) -> str:
    """Order-independent provenance hash of a board: (player_id, espn_rank) pairs.

    Covering the ESPN rank as well as the id set means a board that kept the same
    membership but drifted its ranks — a mid-session re-pull at a different as_of —
    is rejected loudly at resume, instead of silently replaying picks onto a
    differently-priced board (recon §arch F2). The header-driven as_of on the
    normal resume path keeps ranks identical; this hash is the backstop.
    """
    h = hashlib.sha1()
    for e in sorted(board, key=lambda e: e.player_id):
        h.update(e.player_id.encode("utf-8"))
        h.update(b"\x00")
        h.update(str(int(e.espn_overall_rank)).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:16]


# ------------------------------------------------------------------- the session


class DraftSession:
    """Headless snake-draft state machine (no terminal I/O).

    Construct via :meth:`start` (fresh session, writes a new journal header) or
    :meth:`resume` (replay an existing journal to bit-identical state). Every state
    mutation (:meth:`append_pick` / :meth:`undo_last` / :meth:`edit_pick`) persists
    to the JSONL journal before it returns and recomputes the live recalibration.
    """

    def __init__(
        self,
        *,
        board: Sequence[BoardEntry],
        operator_slot: int,
        pick_order: Sequence[int],
        season: int,
        as_of: str,
        journal_path: Path,
        roster: RosterStructure = DEFAULT_ROSTER,
        session_seed: int = 42,
        rollouts: int = 512,
        rounds: int = ROUNDS,
    ) -> None:
        teams = roster.teams
        order = tuple(int(s) for s in pick_order)
        if sorted(order) != list(range(teams)):
            raise ValueError(
                f"pick_order must be a permutation of 0..{teams - 1}; got {list(order)}"
            )
        if not 0 <= operator_slot < teams:
            raise ValueError(f"operator_slot must be in 0..{teams - 1}; got {operator_slot}")

        self.board: tuple[BoardEntry, ...] = tuple(board)
        self._by_id: dict[str, BoardEntry] = {e.player_id: e for e in self.board}
        self.operator_slot = int(operator_slot)
        self.pick_order = order
        self.operator_dp = order.index(self.operator_slot)  # operator's draft position
        self.season = season
        self.as_of = as_of
        self.journal_path = Path(journal_path)
        self.roster = roster
        self.session_seed = int(session_seed)
        self.rollouts = int(rollouts)
        self.rounds = int(rounds)
        self.teams = teams
        self.max_pick = teams * rounds
        # seq[overall-1] = seat id on the clock (snake geometry over pick_order).
        self._sequence = snake_sequence(order, rounds)

        self._picks: list[PickRecord] = []
        self._taken: set[str] = set()
        self._rosters: dict[int, list[BoardEntry]] = {t: [] for t in range(teams)}
        self._header: dict | None = None
        # Human-legible sentences about a tolerated recovery (e.g. a dropped torn
        # tail line). Empty for a fresh start; the app prints each line after a
        # resume (recon §crash F2 / Rule 6).
        self.resume_warnings: list[str] = []
        self._recal: LiveRecalibration = self._compute_recal()

    # -- constructors ------------------------------------------------------

    @classmethod
    def start(
        cls,
        board: Sequence[BoardEntry],
        *,
        operator_slot: int,
        pick_order: Sequence[int],
        season: int,
        as_of: str,
        journal_path: Path,
        roster: RosterStructure = DEFAULT_ROSTER,
        session_seed: int = 42,
        rollouts: int = 512,
    ) -> DraftSession:
        """Begin a fresh session, writing (and fsync'ing) a new journal header.

        The journal is opened exclusive-create (``"x"`` → O_EXCL): if a journal
        already exists at this path — the "terminal died, operator re-ran without
        --resume" footgun — start() raises :class:`JournalExistsError` instead of
        truncating every confirmed pick (recon §crash F1). The parent directory is
        created if absent (``data/draft/`` is gitignored and missing on a fresh
        clone — recon §crash F3), and the directory is fsync'd so the create is
        power-durable (§crash F5).
        """
        sess = cls(
            board=board,
            operator_slot=operator_slot,
            pick_order=pick_order,
            season=season,
            as_of=as_of,
            journal_path=journal_path,
            roster=roster,
            session_seed=session_seed,
            rollouts=rollouts,
        )
        sess._header = sess._build_header()
        sess.journal_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            f = open(sess.journal_path, "x", encoding="utf-8")
        except FileExistsError as exc:
            raise JournalExistsError(sess.journal_path) from exc
        try:
            f.write(sess._header_line())
            f.flush()
            os.fsync(f.fileno())
        finally:
            f.close()
        _fsync_dir(sess.journal_path)
        return sess

    @classmethod
    def resume(cls, journal_path: Path, board: Sequence[BoardEntry]) -> DraftSession:
        """Replay an existing journal to bit-identical state (crash recovery).

        Validates the journalled board provenance (count + hash) against ``board``
        and reconstructs session config from the header. Does NOT rewrite the file
        — subsequent picks append to it.
        """
        journal_path = Path(journal_path)
        lines = journal_path.read_text(encoding="utf-8").splitlines()
        if not lines:
            raise ValueError(f"journal {journal_path} is empty; cannot resume")
        header = json.loads(lines[0])
        if header.get("kind") != "header":
            raise ValueError(f"journal {journal_path} first line is not a session header")

        board = tuple(board)
        if header["board_count"] != len(board) or header["board_hash"] != _board_hash(board):
            raise ValueError(
                "resume board does not match the journalled session (count/hash "
                "mismatch) — re-load the board with the same as_of/season the "
                "session was started with"
            )

        rd = header["roster"]
        roster = RosterStructure(
            teams=rd["teams"],
            starters=dict(rd["starters"]),
            flex_slots=rd["flex_slots"],
            flex_positions=frozenset(rd["flex_positions"]),
        )
        sess = cls(
            board=board,
            operator_slot=header["operator_slot"],
            pick_order=header["pick_order"],
            season=header["season"],
            as_of=header["as_of"],
            journal_path=journal_path,
            roster=roster,
            session_seed=header["session_seed"],
            rollouts=header["rollouts"],
            rounds=header.get("rounds", ROUNDS),
        )
        sess._header = header

        # Torn-tail tolerance (recon §crash F2): a partial/corrupt FINAL line — the
        # single pick being written when power was lost — is dropped and recorded as
        # a human-legible resume_warning. Corruption ANYWHERE ELSE stays a loud error
        # (it cannot be safely dropped without losing durable confirmed picks).
        body = lines[1:]
        last_nonempty = max((i for i, r in enumerate(body) if r.strip()), default=-1)
        records: list[PickRecord] = []
        warnings: list[str] = []
        for i, raw in enumerate(body):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if obj.get("kind") != "pick":
                    continue
                rec = PickRecord(
                    overall=int(obj["overall"]),
                    seat=int(obj["seat"]),
                    player_id=str(obj["player_id"]),
                    name=obj.get("name"),
                )
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                if i == last_nonempty:
                    warnings.append(
                        "The draft journal's last line was only partially written "
                        "(likely a crash or power loss mid-write); it was dropped and "
                        f"the {len(records)} fully-saved picks before it were recovered."
                    )
                    break
                raise ValueError(
                    f"journal {journal_path} is corrupt at line {i + 2} (not the final "
                    "line, so it cannot be safely dropped); cannot resume"
                ) from exc
            records.append(rec)

        for i, rec in enumerate(records, start=1):
            if rec.overall != i:
                raise ValueError(
                    f"journal picks are not contiguous: overall {rec.overall} where "
                    f"{i} was expected"
                )
        sess._rebuild_from_records(records)  # revalidates duplicate player_ids (loud)
        sess.resume_warnings = warnings
        return sess

    # -- read-only state ---------------------------------------------------

    @property
    def overall_pick(self) -> int:
        """1-based overall of the NEXT pick (== confirmed picks + 1)."""
        return len(self._picks) + 1

    @property
    def complete(self) -> bool:
        return len(self._picks) >= self.max_pick

    @property
    def current_seat(self) -> int | None:
        """Seat id on the clock for the next pick, or None when the draft is done."""
        if self.complete:
            return None
        return self._sequence[self.overall_pick - 1]

    @property
    def is_operator_turn(self) -> bool:
        return (not self.complete) and self.current_seat == self.operator_slot

    @property
    def taken(self) -> set[str]:
        return set(self._taken)

    @property
    def picks(self) -> tuple[PickRecord, ...]:
        return tuple(self._picks)

    @property
    def own_roster(self) -> tuple[BoardEntry, ...]:
        return tuple(self._rosters[self.operator_slot])

    @property
    def opponent_rosters(self) -> dict[int, tuple[BoardEntry, ...]]:
        """Seat id -> that rival's drafted entries (excludes the operator)."""
        return {
            t: tuple(entries)
            for t, entries in self._rosters.items()
            if t != self.operator_slot
        }

    @property
    def round(self) -> int:
        """1-based round of the next pick."""
        return (self.overall_pick - 1) // self.teams + 1

    # -- posture-comparator surface (recon §2 / posture.PostureSession) ------
    #
    # The default posture comparator (posture.project_postures) reads a slightly
    # wider structural surface than the render layer. These read-only views expose
    # it off the same live state so the posture check engages with the session's
    # real config (rounds, the live-recalibrated room model) instead of silently
    # falling back to library defaults. posture.py never imports session.py — it
    # duck-types this surface via a structural Protocol.

    @property
    def rounds_total(self) -> int:
        """Total draft rounds (the name the posture comparator reads)."""
        return self.rounds

    @property
    def engine(self) -> PickEngine:
        """The operator's live pick engine (live-recalibrated priors when engaged).

        The posture comparator clones this per archetype (swapping ``need_schedule``
        and neutralising survival), so it reflects the same weights/priors the
        on-clock recommendation uses.
        """
        return self._engine()

    @property
    def room_priors(self) -> RoomPriors | None:
        """Live-recalibrated room priors when the re-fit is engaged, else None.

        None lets the comparator fall back to the 2025 baseline (``ROOM_PRIORS_2025``)
        — the honest cold-start room model, matching the recalibration display.
        """
        return self._recal.priors if self._recal.engaged else None

    # -- mutations (all persist before returning) --------------------------

    def append_pick(self, player_id: str) -> None:
        """Confirm the next pick for the seat on the clock; fsync BEFORE returning.

        The seat is inferred from the snake position (the operator never confirms a
        rival's pick silently — the caller supplies the id). The journal line is
        durable before this returns, so a crash after the UI ack cannot lose it.
        """
        if self.complete:
            raise RuntimeError("draft is complete; no more picks to append")
        entry = self._by_id.get(player_id)
        if entry is None:
            raise ValueError(f"player_id {player_id!r} is not on the board")
        if player_id in self._taken:
            raise ValueError(f"player_id {player_id!r} is already drafted")

        overall = self.overall_pick
        seat = self._sequence[overall - 1]
        rec = PickRecord(overall=overall, seat=seat, player_id=player_id, name=entry.name)

        # Journal-first, fsync before the in-memory ack, so a resume never lags the
        # displayed state (recon §2 "Crash recovery").
        self._append_journal_line(self._pick_line(rec))
        self._apply(rec)
        self._recal = self._compute_recal()

    def undo_last(self) -> None:
        """Remove the most recent confirmed pick (log rewrite + full replay)."""
        if not self._picks:
            raise RuntimeError("no picks to undo")
        self._commit_records(list(self._picks[:-1]))

    def edit_pick(self, overall: int, *, player_id: str) -> None:
        """Correct an arbitrary earlier pick's PLAYER (rewrite + replay).

        The seat is NOT editable (audit state F2): in a snake draft the drafting
        seat is fully determined by ``overall``, so a seat override could only ever
        introduce a silent desync between the journalled seat and the true snake
        order. Only the player at ``overall`` is swapped; its snake-derived seat is
        preserved. Later picks keep their journalled ids.
        """
        records = list(self._picks)
        idx = next((i for i, r in enumerate(records) if r.overall == overall), None)
        if idx is None:
            raise ValueError(f"no confirmed pick at overall {overall}")

        old = records[idx]
        entry = self._by_id.get(player_id)
        if entry is None:
            raise ValueError(f"player_id {player_id!r} is not on the board")
        for i, r in enumerate(records):
            if i != idx and r.player_id == player_id:
                raise ValueError(
                    f"player_id {player_id!r} is already drafted at overall {r.overall}"
                )

        records[idx] = PickRecord(
            overall=old.overall, seat=old.seat, player_id=player_id, name=entry.name
        )
        self._commit_records(records)

    # -- recommendation surface (operator turn only) -----------------------

    def recommend(self, top: int = 5) -> Sequence[PickRec]:
        """Top-``top`` engine recommendations for the operator's current pick.

        Builds a FRESH state-seeded ctx every call (recon §1) and configures the
        engine with the live-recalibrated room priors when the re-fit is engaged.
        """
        if not self.is_operator_turn:
            raise RuntimeError(
                "recommend() is only valid on the operator's turn "
                f"(seat {self.operator_slot}); the clock is on seat {self.current_seat}"
            )
        ctx = self._operator_context(
            overall=self.overall_pick,
            own_roster=self.own_roster,
            taken=self._taken,
        )
        return self._engine().recommend(ctx, top=top)

    def next_operator_overall(self) -> int | None:
        """1-based overall of the operator's NEXT pick, or None when none remain.

        On the operator's turn this is :attr:`overall_pick` itself. None means
        either the draft is complete or every remaining pick belongs to rivals
        (the operator's roster is already fully drafted) — there is nothing left
        to queue for.
        """
        for o in range(self.overall_pick, self.max_pick + 1):
            if self._sequence[o - 1] == self.operator_slot:
                return o
        return None

    def recommend_upcoming(self, top: int = 8) -> Sequence[PickRec]:
        """Engine recommendations for the operator's NEXT pick, valid off-turn.

        The queue-writer seam (auto-entry spec §6): ESPN's Pick Queue must be
        populated BETWEEN operator turns, so this builds the ctx at
        :meth:`next_operator_overall` with today's roster and taken set — the
        same future-overall pattern :meth:`contingencies` uses for the wheel.
        On the operator's own turn the ctx (and its ``session_seed ^ overall``
        rng) is identical to :meth:`recommend`'s, so the two agree bit-for-bit
        — the queue head IS the on-clock recommendation. Off-turn the board is
        slightly richer than it will be by that pick (the intervening picks
        haven't happened yet); each refresh after an observed pick converges
        the estimate, and depth-K absorbs the snipes in between.

        Raises when no operator pick remains — the caller must distinguish
        "nothing to queue" (an explicit state) from an empty recommendation
        list, never conflate them.
        """
        target = self.next_operator_overall()
        if target is None:
            raise RuntimeError(
                "no operator picks remain in this draft; there is nothing to queue"
            )
        ctx = self._operator_context(
            overall=target, own_roster=self.own_roster, taken=self._taken
        )
        return self._engine().recommend(ctx, top=top)

    def contingencies(self) -> Sequence[Contingency]:
        """Snake-turn "if X now -> then Y on the wheel" branches (top-3, 2-ply).

        Empty unless the operator is on the clock AND picks again at the very next
        overall (a wheel). For each of the top-3 first picks X, take X hypothetically
        and recommend the wheel pick Y — no opponent picks intervene (0 short-circuit).
        """
        if not self._is_snake_turn():
            return ()
        firsts = self.recommend(top=3)
        engine = self._engine()
        own = self.own_roster
        wheel_overall = self.overall_pick + 1
        out: list[Contingency] = []
        for rec in firsts:
            x = rec.player
            hypo_own = own + (x,)
            hypo_taken = set(self._taken)
            hypo_taken.add(x.player_id)
            ctx = self._operator_context(
                overall=wheel_overall, own_roster=hypo_own, taken=hypo_taken
            )
            wheel = engine.recommend(ctx, top=1)[0]
            out.append(Contingency(first=rec, wheel=wheel))
        return tuple(out)

    def recalibration(self) -> RecalibrationStatus:
        """Live room re-fit status with the honesty fields (seen / needed)."""
        lr = self._recal
        picks_needed = max(0, LIVE_RECAL_MIN_PICKS - lr.n_room_picks)
        return RecalibrationStatus(
            engaged=lr.engaged,
            n_room_picks=lr.n_room_picks,
            min_room_picks=LIVE_RECAL_MIN_PICKS,
            picks_needed=picks_needed,
            reach_sigma=lr.reach_sigma,
            reach_center=lr.reach_center,
            board_adherence_pearson=lr.board_adherence_pearson,
            priors=lr.priors,
        )

    def suggest_autodraft(self, seat: int) -> str:
        """The ESPN-top-available legal pick an autodrafting ``seat`` would take.

        The :class:`AutodraftBot` rule (bots.py). The pick is still entered through
        :meth:`append_pick` by the operator — this never self-enters a rival's pick.
        """
        if self.complete:
            raise RuntimeError("draft is complete; nothing to autodraft")
        if not 0 <= seat < self.teams:
            raise ValueError(f"seat {seat} out of range 0..{self.teams - 1}")
        overall = self.overall_pick
        round_num = (overall - 1) // self.teams + 1
        ctx = PickContext.from_board(
            self.board,
            own_roster=tuple(self._rosters[seat]),
            taken=self._taken,
            team_slot=seat,
            round=round_num,
            overall_pick=overall,
            rounds_total=self.rounds,
            roster=self.roster,
            rng=random.Random(self.session_seed ^ overall),
        )
        return AutodraftBot().pick(ctx)

    # -- internals ---------------------------------------------------------

    def _is_snake_turn(self) -> bool:
        """True when the operator is on the clock and picks again next overall."""
        if not self.is_operator_turn:
            return False
        nxt = self.overall_pick + 1
        if nxt > self.max_pick:
            return False
        return self._sequence[nxt - 1] == self.operator_slot

    def _engine(self) -> PickEngine:
        recal = self._recal
        priors = recal.priors if recal.engaged else None
        return PickEngine(rollouts=self.rollouts, room_priors=priors)

    def _operator_context(
        self,
        *,
        overall: int,
        own_roster: Sequence[BoardEntry],
        taken: Iterable[str],
    ) -> PickContext:
        """Fresh engine ctx (recon §1) with opponent rosters keyed by DRAFT POSITION.

        Survival works in draft-position space (``_snake_team_at`` assumes an
        identity pick_order), so keying rivals by draft position and passing the
        operator's draft position as ``team_slot`` makes the intervening-pick set
        and ``picks_until_next`` correct for any pick_order permutation (recon §3).
        """
        dp_rosters = {
            dp: tuple(self._rosters[self.pick_order[dp]])
            for dp in range(self.teams)
            if dp != self.operator_dp
        }
        round_num = (overall - 1) // self.teams + 1
        return PickContext.from_board(
            self.board,
            own_roster=tuple(own_roster),
            taken=set(taken),
            team_slot=self.operator_dp,
            round=round_num,
            overall_pick=overall,
            rounds_total=self.rounds,
            roster=self.roster,
            rng=random.Random(self.session_seed ^ overall),
            opponent_rosters=dp_rosters,
        )

    def _apply(self, rec: PickRecord) -> None:
        self._picks.append(rec)
        self._taken.add(rec.player_id)
        self._rosters[rec.seat].append(self._by_id[rec.player_id])

    def _rebuild_from_records(self, records: Sequence[PickRecord]) -> None:
        """Rebuild in-memory state from scratch by replaying ``records`` in order.

        Revalidates on replay (recon §crash F6): an unknown player_id, or the SAME
        player drafted twice (a tampered/corrupt journal), fails loudly here rather
        than silently leaving ``taken`` short of ``picks`` and a player on two rosters.
        """
        self._picks = []
        self._taken = set()
        self._rosters = {t: [] for t in range(self.teams)}
        for rec in records:
            if rec.player_id not in self._by_id:
                raise ValueError(f"journalled player_id {rec.player_id!r} is not on the board")
            if rec.player_id in self._taken:
                raise ValueError(
                    f"journalled player_id {rec.player_id!r} is drafted more than once "
                    f"(again at overall {rec.overall}); the journal is corrupt"
                )
            self._apply(rec)
        self._recal = self._compute_recal()

    def _commit_records(self, records: Sequence[PickRecord]) -> None:
        """Atomic log rewrite + full replay (the correction/recovery path)."""
        self._rewrite_journal(records)
        self._rebuild_from_records(records)

    def _compute_recal(self) -> LiveRecalibration:
        pick_log = [(p.overall, p.seat, p.player_id) for p in self._picks]
        return recalibrate_from_pick_log(
            pick_log, self.board, operator_slot=self.operator_slot
        )

    # -- journal I/O -------------------------------------------------------

    def _build_header(self) -> dict:
        return {
            "kind": "header",
            "season": self.season,
            "as_of": self.as_of,
            "operator_slot": self.operator_slot,
            "pick_order": list(self.pick_order),
            "session_seed": self.session_seed,
            "rollouts": self.rollouts,
            "rounds": self.rounds,
            "roster": {
                "teams": self.roster.teams,
                "starters": dict(self.roster.starters),
                "flex_slots": self.roster.flex_slots,
                "flex_positions": sorted(self.roster.flex_positions),
            },
            "board_count": len(self.board),
            "board_hash": _board_hash(self.board),
        }

    def _header_line(self) -> str:
        if self._header is None:  # pragma: no cover - start/resume always set it
            self._header = self._build_header()
        return json.dumps(self._header, sort_keys=True) + "\n"

    @staticmethod
    def _pick_line(rec: PickRecord) -> str:
        return (
            json.dumps(
                {
                    "kind": "pick",
                    "overall": rec.overall,
                    "seat": rec.seat,
                    "player_id": rec.player_id,
                    "name": rec.name,
                }
            )
            + "\n"
        )

    def _append_journal_line(self, line: str) -> None:
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def _rewrite_journal(self, records: Sequence[PickRecord]) -> None:
        tmp = self.journal_path.with_name(self.journal_path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(self._header_line())
                for rec in records:
                    f.write(self._pick_line(rec))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.journal_path)
            # Dir fsync makes the rename power-durable (recon §crash F5).
            _fsync_dir(self.journal_path)
        except BaseException:
            # A failed rewrite must not orphan a partial <journal>.tmp (recon §crash
            # F7); the original journal is untouched (os.replace is atomic), so a
            # subsequent resume/rewrite recovers cleanly.
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
