"""Decision-time archive (item 4.2b): freeze what the tool said, on the day.

`ziggurat waivers` produced stdout and nothing else, so the Tuesday that
produced a decision could not be reconstructed afterwards — and a missed Tuesday
is unrecoverable (`league_player_state` accumulates forward only, item 3.1; four
market sources serve the current value only, item 3.1b).

Three modules, one direction of dependency:

* :mod:`ziggurat.decisions.capture` — the writer. Turns a
  :class:`ziggurat.core.waiver.WaiverArtifacts` (the passive collector item 4.2b
  B1 added to ``build_waiver_plan``) into a sha256-manifested directory of JSONL
  under the gitignored ``data/decisions/<season>/wk<NN>/<capture_id>/``.
* :mod:`ziggurat.decisions.store` — the ``decision_freezes`` run log
  (migration 018), a ``push/runs.py``-shaped operational table.
* :mod:`ziggurat.decisions.read` — load a capture back and verify every byte
  against its manifest.

This package imports ``core`` / ``league`` / ``data`` and is imported by
``cli``. It never imports ``ziggurat.draft`` (Rule 8) and never imports
``backtest`` (the dependency runs backtest -> ziggurat). The manifest/atomic-write
discipline is COPIED from ``backtest/decisions.py`` and the run-log discipline
from ``ziggurat/push/runs.py`` — copied, as ``push/run.py`` already copies the
draft journal's fsync discipline, never imported.
"""
