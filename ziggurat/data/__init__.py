"""Ingestion clients and data access (ESPN, NFL data, projections, odds, podcasts).

Standing rule: every read accessor takes a keyword-only `as_of` and returns only
what was knowable at that moment. See ziggurat/data/asof.py for the convention.
"""
