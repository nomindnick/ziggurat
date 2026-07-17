"""nfl_data_py / nflverse ingestion (item 1.4).

Each source has a thin client that wraps exactly one `nfl.import_*` call (the
patch seam cached-fixture tests replace) and upserts into SQLite with two
knowledge-time columns, plus a keyword-only `as_of` accessor with a leakage
test. See ziggurat/data/nfl/base.py for the shared machinery and
ziggurat/data/nfl/players.py for the exemplar every source copies.
"""
