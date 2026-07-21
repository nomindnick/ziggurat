"""Room-behavior priors for the mock-draft opponent model (item 2.2).

DELETABLE package (Rule 8). ``RoomPriors`` is the single frozen knob-bag the
bots read: the reach-noise spread, how tightly the room hugs the ESPN board, the
autodraft share, the K/DST round window, the round->position run curves, and an
(off-by-default) per-bot positional lean.

CALIBRATION PROVENANCE — read this before touching the numbers.
``ROOM_PRIORS_2025`` holds the values FITTED from the league's single prior draft
(2025, the inaugural season, n=1) by ``ziggurat.draft.calibration.fit_room_priors``
run over the raw recon artifacts under ``data/recon-2.2/`` (gitignored). The full
fit is archived at ``scratchpad/priors_fit_2025.json``. These are deliberately
WEAK priors — one season, 2/10 seats fully autodrafted (8 human seats / 125 human
picks / 109 human skill-position picks for the reach spread) — aggregate room
tendencies only, never per-manager strategy (recon §3c). Every default below cites
its artifact + n. ``calibration.py`` recomputes them from source and its
real-artifact anchor test proves this module still agrees with the fit.

Reach convention (recon §3b): reach = board_rank - overall_pick; positive = the
room took a player EARLIER than the board (a "reach up").
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

# The room defers kickers/defenses then clusters them at the end. In 2025 the
# first HUMAN K went at overall pick 86 (round 9), the first human DST at 93
# (round 10); K/DST ranked hundreds deep on the ESPN editorial board, so an
# "overall reach" for them is meaningless — it is a structural roster rule, not
# aggression. kdst_earliest_round = min(first-human-K round, first-human-DST
# round) = 9. (priors_fit_2025.json kdst_windows.human; recon §3b/§3c;
# artifact leagueHistory_2025_mDraftDetail_mTeam_mSettings.json + kona universe)
DEFAULT_KDST_EARLIEST_ROUND = 9

# Round -> {position: within-round propensity in [0,1]} the room "runs" that
# round. FITTED, human picks only, position from the kona universe's
# defaultPositionId (never the pick's lineupSlotId). Each round's fractions sum
# to 1 over that round's n human picks (n=7-8/round; a single inaugural draft, so
# these are noisy weak priors). The K/DST propensities in rounds 9+ reinforce the
# late clustering the round-window rule already enforces structurally.
# (priors_fit_2025.json position_run_curve.fractions_by_round; artifact
# leagueHistory_2025_mDraftDetail_mTeam_mSettings.json + kona universe; n_human=125)
_FITTED_POSITION_RUN: Mapping[int, Mapping[str, float]] = MappingProxyType(
    {
        1: MappingProxyType({"QB": 0.125, "RB": 0.5, "WR": 0.375}),                        # n=8: QB1 RB4 WR3
        2: MappingProxyType({"RB": 0.375, "WR": 0.625}),                                   # n=8: RB3 WR5
        3: MappingProxyType({"QB": 0.25, "RB": 0.25, "WR": 0.25, "TE": 0.25}),             # n=8: QB2 RB2 WR2 TE2
        4: MappingProxyType({"QB": 0.25, "RB": 0.25, "WR": 0.5}),                          # n=8: QB2 RB2 WR4
        5: MappingProxyType({"QB": 0.125, "RB": 0.625, "WR": 0.25}),                       # n=8: QB1 RB5 WR2
        6: MappingProxyType({"RB": 0.125, "WR": 0.75, "TE": 0.125}),                       # n=8: RB1 WR6 TE1
        7: MappingProxyType({"QB": 0.25, "RB": 0.125, "WR": 0.25, "TE": 0.375}),           # n=8: QB2 RB1 WR2 TE3
        8: MappingProxyType({"RB": 0.2857, "WR": 0.4286, "TE": 0.2857}),                   # n=7: RB2 WR3 TE2
        9: MappingProxyType({"RB": 0.5714, "WR": 0.2857, "K": 0.1429}),                    # n=7: RB4 WR2 K1
        10: MappingProxyType({"QB": 0.25, "RB": 0.25, "WR": 0.375, "DST": 0.125}),         # n=8: QB2 RB2 WR3 DST1
        11: MappingProxyType({"QB": 0.125, "RB": 0.375, "WR": 0.25, "TE": 0.125, "DST": 0.125}),  # n=8: QB1 RB3 WR2 TE1 DST1
        12: MappingProxyType({"RB": 0.375, "WR": 0.5, "K": 0.125}),                        # n=8: RB3 WR4 K1
        13: MappingProxyType({"QB": 0.125, "RB": 0.25, "WR": 0.25, "TE": 0.375}),          # n=8: QB1 RB2 WR2 TE3
        14: MappingProxyType({"QB": 0.2857, "RB": 0.1429, "WR": 0.1429, "TE": 0.1429, "DST": 0.1429, "K": 0.1429}),  # n=7: QB2 RB1 WR1 TE1 DST1 K1
        15: MappingProxyType({"QB": 0.125, "DST": 0.625, "K": 0.25}),                      # n=8: QB1 DST5 K2
        16: MappingProxyType({"RB": 0.375, "WR": 0.125, "TE": 0.125, "K": 0.375}),         # n=8: RB3 WR1 TE1 K3
    }
)


@dataclass(frozen=True)
class RoomPriors:
    """Weak, tunable room-behavior priors. All defaults FITTED from the 2025 draft
    (``calibration.fit_room_priors``; archived in ``scratchpad/priors_fit_2025.json``)
    and cited to their artifact + n.

    Fields (the 2.3 pick engine and the calibration harness both key off these):

    * ``reach_sigma`` — spread (in ESPN-rank slots) of the Gaussian reach-noise a
      ``RankNoiseBot`` adds to a candidate's board rank. Larger => the room reaches
      further off the board. FIT = 17.78 = the population std of the 2025 human
      skill-only reach distribution scored against the (IDP-filtered, re-ranked)
      db_fpecr draft-day ECR board (n=109; mean +1.17, median +1.0 => ~symmetric,
      which is WHY the zero-mean Gaussian below is faithful and why fpecr — not the
      ESPN editorial board, whose reach carries a systematic +15 editorial-vs-market
      offset — is the reference). Robust MAD-sigma = 16.31 is the conservative
      alternative. (priors_fit_2025.json reach.fpecr; recon §3b/§3c;
      artifact fpecr_2025_ro_redraft_overall_2025-08-29.parquet)

    * ``board_adherence`` — divides ``reach_sigma`` (effective sigma =
      ``reach_sigma / board_adherence``). >1 hugs the ESPN board tighter; <1
      loosens it. Held NEUTRAL at 1.0 by design: ``reach_sigma`` is ALREADY the
      empirical spread of board-deviations (it embeds the room's adherence), so
      folding the fitted pick/board-rank Pearson (0.907 fpecr, n=150) in as a
      divisor would double-count — and, because that correlation is <1, would
      perversely LOOSEN the noise when a high correlation means the room hugged the
      board tightly. So the fitted Pearson is RECORDED (below / design note) as the
      empirical adherence but not composed into the draw; board_adherence stays a
      clean tuning lever for item 2.3. (priors_fit_2025.json reach.fpecr
      .board_adherence_pearson = 0.907)

    * ``autodraft_fraction`` — probability an individual non-operator seat drafts
      fully on autopilot (pure board order) for a given sim run. FIT = 0.2 = 2/10
      seats fully autodrafted in 2025 (autoDraftTypeId==3 for all 16 picks).
      (priors_fit_2025.json autodraft; artifact leagueHistory_2025_...json)

    * ``kdst_earliest_round`` — no bot takes a K or DST before this round unless
      roster legality forces it; completion is forced by the final round anyway.
      FIT = 9 (first human K overall 86 R9, first human DST overall 93 R10).

    * ``position_run`` — round -> {position: propensity} run curve; a gentle nudge
      toward the position the room tends to cluster on that round. FITTED, all 16
      rounds, human picks only (see ``_FITTED_POSITION_RUN``).

    * ``position_lean`` — OPTIONAL per-round positional lean for a *personalized*
      bot. SHIPPED but default OFF (``None``): n=1 season is too thin to fit a
      trustworthy per-manager tendency, and the 2 fully-autodrafted seats leave no
      human signal (recon §3c). Left here so a later season can turn it on without a
      redesign; consumed nowhere yet (a small wire-in inside ``RankNoiseBot.pick``).
    """

    reach_sigma: float = 17.78
    board_adherence: float = 1.0
    autodraft_fraction: float = 0.2
    kdst_earliest_round: int = DEFAULT_KDST_EARLIEST_ROUND
    position_run: Mapping[int, Mapping[str, float]] = _FITTED_POSITION_RUN
    position_lean: Mapping[int, Mapping[str, float]] | None = None


# The empirical pick/board-rank correlation from the 2025 fit (fpecr, n=150).
# Recorded for the design note / 2.3 tuning; NOT folded into the noise draw (see
# RoomPriors.board_adherence). (priors_fit_2025.json reach.fpecr.board_adherence_pearson)
EMPIRICAL_BOARD_ADHERENCE_2025 = 0.907

# The default room the simulator drafts against: the 2025-fitted priors.
ROOM_PRIORS_2025 = RoomPriors()
