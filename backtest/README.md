# backtest/ — experiments & replay harness

Lands in Phase 4 (item 4.1). Imports `ziggurat/` directly and replays history
through the *production* code paths — that's the point of the repo-wide `as_of`
discipline.

Standing methodology for every experiment: strict as-of cuts on all inputs
(including podcast publish dates), train on 2021–23 / validate on 2024–25,
grade decisions not outcomes, precision@k for k ≤ 3 (the realistic weekly
claim budget).
