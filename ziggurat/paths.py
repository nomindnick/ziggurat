"""Repo-anchored paths.

Valid for the editable, in-repo install this project uses (the repo is the
system's home; there is no deployed wheel).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DB_DIR = REPO_ROOT / "db"
SCHEMA_PATH = DB_DIR / "schema.sql"
DEFAULT_DB_PATH = DB_DIR / "ziggurat.sqlite"

CONFIG_DIR = REPO_ROOT / "config"
LLM_CONFIG_PATH = CONFIG_DIR / "llm.toml"

TEMPLATES_DIR = REPO_ROOT / "templates"
INTEL_TEMPLATES_DIR = TEMPLATES_DIR / "intel"
INTEL_DIR = REPO_ROOT / "intel"
