"""Pure alignment tests for the ESPN-vs-market divergence report (item 1.5 §6).

The core is pure: fixed market rows (shaped like ``get_adp_rankings`` output) +
fixed ESPN-side positional-rank rows -> asserted ``pos_rank_delta`` and ``flag``,
including the DST team-abbr join path (LAR->LA normalization) and the sd-based
confidence gate. A final integration test proves ``get_adp_rankings`` output
feeds ``build_divergence`` unchanged (sqlite3.Row -> dict). No network, no
league-private data.
"""

import pandas as pd

from ziggurat.core.divergence import (
    CONTESTED,
    ESPN_HIGHER,
    MARKET_HIGHER,
    build_divergence,
    format_report,
)
from ziggurat.data.nfl import adp_rankings


def _market(espn_id=None, team=None, position="RB", pos_rank=1, ecr=1.0,
            sd=1.0, best=1, worst=3, player="Player"):
    """A market row shaped like a get_adp_rankings result dict."""
    return {
        "espn_id": espn_id, "team": team, "position": position, "pos_rank": pos_rank,
        "ecr": ecr, "sd": sd, "best": best, "worst": worst, "player": player,
    }


def _espn(espn_id=None, team=None, position="RB", espn_pos_rank=1, player=None):
    return {
        "espn_id": espn_id, "team": team, "position": position,
        "espn_pos_rank": espn_pos_rank, "player": player,
    }


def _by_player(rows):
    return {r.player: r for r in rows}


def test_market_higher_flag_and_delta():
    # Market ranks Allen QB2; ESPN ranks him QB5 -> market values him higher.
    market = [_market(espn_id="3918298", position="QB", pos_rank=2, sd=1.16,
                      best=21, worst=31, ecr=25.0, player="Josh Allen")]
    espn = [_espn(espn_id="3918298", position="QB", espn_pos_rank=5, player="Josh Allen")]
    rows = build_divergence(market, espn)
    assert len(rows) == 1
    row = rows[0]
    assert row.pos_rank_delta == 3          # 5 - 2
    assert row.flag == MARKET_HIGHER        # |3| > sd 1.16
    assert row.espn_pos_rank == 5
    assert row.market_pos_rank == 2
    assert row.market_ecr == 25.0
    assert row.market_sd == 1.16
    assert row.spread == 10                 # worst 31 - best 21


def test_espn_higher_flag():
    # ESPN ranks Chase WR1; market has him WR3 -> ESPN values him higher.
    market = [_market(espn_id="4362628", position="WR", pos_rank=3, sd=0.95,
                      best=1, worst=4, player="Ja'Marr Chase")]
    espn = [_espn(espn_id="4362628", position="WR", espn_pos_rank=1, player="Ja'Marr Chase")]
    row = build_divergence(market, espn)[0]
    assert row.pos_rank_delta == -2         # 1 - 3
    assert row.flag == ESPN_HIGHER          # |2| > sd 0.95


def test_contested_zero_delta_within_sd():
    # Identical ranks -> delta 0 -> inside the noise floor.
    market = [_market(espn_id="4430807", position="RB", pos_rank=1, sd=0.8,
                      player="Bijan Robinson")]
    espn = [_espn(espn_id="4430807", position="RB", espn_pos_rank=1, player="Bijan Robinson")]
    row = build_divergence(market, espn)[0]
    assert row.pos_rank_delta == 0
    assert row.flag == CONTESTED


def test_contested_nonzero_delta_within_sd_gate():
    # Nonzero delta but smaller than the market's dispersion -> still CONTESTED.
    market = [_market(espn_id="9999", position="RB", pos_rank=2, sd=1.9,
                      best=2, worst=8, player="Saquon Barkley")]
    espn = [_espn(espn_id="9999", position="RB", espn_pos_rank=3, player="Saquon Barkley")]
    row = build_divergence(market, espn)[0]
    assert row.pos_rank_delta == 1          # 3 - 2
    assert row.flag == CONTESTED            # |1| <= sd 1.9 (the sd gate)


def test_gate_multiplier_tightens_the_gate():
    # The same delta 1 vs sd 1.9: a multiplier of 0 collapses the gate so any
    # nonzero delta becomes directional.
    market = [_market(espn_id="9999", position="RB", pos_rank=2, sd=1.9, player="Saquon Barkley")]
    espn = [_espn(espn_id="9999", position="RB", espn_pos_rank=3, player="Saquon Barkley")]
    row = build_divergence(market, espn, gate_multiplier=0.0)[0]
    assert row.flag == MARKET_HIGHER        # gate 0 -> delta 1 > 0


def test_dst_joins_by_normalized_team_abbr():
    # DST carry no espn_id; the market side is normalized to LA (LAR->LA at
    # ingest), the ESPN side still says LAR. The abbr join must bridge them.
    market = [_market(espn_id=None, team="LA", position="DST", pos_rank=12,
                      sd=6.0, best=145, worst=180, ecr=150.0, player="Los Angeles Rams")]
    espn = [_espn(espn_id=None, team="LAR", position="DST", espn_pos_rank=20)]
    rows = build_divergence(market, espn)
    assert len(rows) == 1
    row = rows[0]
    assert row.team == "LA"
    assert row.pos_rank_delta == 8          # 20 - 12
    assert row.flag == MARKET_HIGHER        # |8| > sd 6.0
    assert row.player == "Los Angeles Rams"  # falls back to the market name
    assert row.spread == 35                 # worst 180 - best 145


def test_dst_within_sd_gate_is_contested():
    # A DST delta inside the (wide) DST dispersion band is CONTESTED.
    market = [_market(espn_id=None, team="JAX", position="DST", pos_rank=18,
                      sd=8.0, best=190, worst=240, player="Jacksonville Jaguars")]
    espn = [_espn(espn_id=None, team="JAC", position="DST", espn_pos_rank=22)]
    row = build_divergence(market, espn)[0]
    assert row.team == "JAX"                # JAC->JAX normalized on the ESPN side too
    assert row.pos_rank_delta == 4          # 22 - 18
    assert row.flag == CONTESTED            # |4| <= sd 8.0


def test_unmatched_espn_rows_are_skipped():
    market = [_market(espn_id="1", position="QB", pos_rank=1, player="A")]
    espn = [
        _espn(espn_id="1", position="QB", espn_pos_rank=1, player="A"),
        _espn(espn_id="does-not-exist", position="QB", espn_pos_rank=2, player="B"),
        _espn(team="ZZZ", position="DST", espn_pos_rank=5),
    ]
    rows = build_divergence(market, espn)
    assert [r.player for r in rows] == ["A"]


def test_output_sorted_by_absolute_delta_desc():
    market = [
        _market(espn_id="1", position="QB", pos_rank=1, sd=0.1, player="Small"),
        _market(espn_id="2", position="RB", pos_rank=1, sd=0.1, player="Big"),
    ]
    espn = [
        _espn(espn_id="1", position="QB", espn_pos_rank=2, player="Small"),   # delta 1
        _espn(espn_id="2", position="RB", espn_pos_rank=11, player="Big"),    # delta 10
    ]
    rows = build_divergence(market, espn)
    assert [r.player for r in rows] == ["Big", "Small"]


def test_format_report_renders_header_and_rows():
    market = [_market(espn_id="1", position="QB", pos_rank=1, player="Josh Allen")]
    espn = [_espn(espn_id="1", position="QB", espn_pos_rank=4, player="Josh Allen")]
    text = format_report(build_divergence(market, espn))
    assert "player" in text and "flag" in text
    assert "Josh Allen" in text
    assert MARKET_HIGHER in text


# ------------------------------------------------------------ integration seam
def _stub_players(db, mapping, *, retrieved="2026-08-01"):
    for fp, (gsis, espn) in mapping.items():
        db.execute(
            "INSERT INTO players (gsis_id, fantasypros_id, espn_id, retrieved_as_of, knowable_as_of) "
            "VALUES (?,?,?,?,?)",
            (gsis, fp, espn, retrieved, retrieved),
        )
    db.commit()


def _adp_frame(scrape_date="2026-08-15"):
    rows = [
        (17298, "Josh Allen", "QB", "BUF", 25.19, 1.16, 21, 31, 99.5),
        (10101, "Bijan Robinson", "RB", "ATL", 1.50, 0.80, 1, 3, 99.9),
        (8130, "Los Angeles Rams", "DST", "LAR", 150.0, 6.0, 145, 180, 90.0),
    ]
    df = pd.DataFrame(
        rows,
        columns=["id", "player", "pos", "team", "ecr", "sd", "best", "worst", "player_owned_avg"],
    )
    df["ecr_type"] = "ro"
    df["scrape_date"] = scrape_date
    return df


def test_get_adp_rankings_output_feeds_build_divergence(db):
    """get_adp_rankings rows (sqlite3.Row) flow into build_divergence unchanged."""
    _stub_players(db, {
        "17298": ("00-0034857", "3918298"),   # Josh Allen
        "10101": ("00-0038542", "4430807"),   # Bijan Robinson
    })
    adp_rankings.ingest_adp_rankings(db, _adp_frame(), retrieved_as_of="2026-08-15")
    market_rows = adp_rankings.get_adp_rankings(db, as_of="2026-08-15", season=2026)

    espn = [
        _espn(espn_id="3918298", position="QB", espn_pos_rank=5, player="Josh Allen"),
        _espn(espn_id="4430807", position="RB", espn_pos_rank=1, player="Bijan Robinson"),
        _espn(team="LAR", position="DST", espn_pos_rank=20, player="Rams D/ST"),
    ]
    rows = build_divergence(market_rows, espn)
    by = _by_player(rows)

    # Skill players join on espn_id; DST joins on the normalized team abbr.
    assert set(by) == {"Josh Allen", "Bijan Robinson", "Rams D/ST"}
    assert by["Josh Allen"].flag == MARKET_HIGHER      # market QB1 vs espn 5
    assert by["Josh Allen"].pos_rank_delta == 4        # espn 5 - market pos_rank 1
    assert by["Bijan Robinson"].flag == CONTESTED      # delta 0 within sd 0.8
    rams = by["Rams D/ST"]
    assert rams.team == "LA"
    assert rams.market_pos_rank == 1                    # only DST in the frame
    assert rams.pos_rank_delta == 19                    # espn 20 - market 1
    assert rams.flag == MARKET_HIGHER
