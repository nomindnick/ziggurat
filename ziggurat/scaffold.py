"""Local-only tree bootstrap.

intel/ is gitignored (league-private, never committed), so a fresh clone lacks
it. `ziggurat intel init` recreates the skeleton from the committed templates
under templates/intel/. Existing files are NEVER overwritten — the live tree is
the operator's memory, not a build artifact.
"""

import shutil
from pathlib import Path

from ziggurat.paths import REPO_ROOT


def ensure_intel_tree(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Copy any missing files from templates/intel/ into intel/.

    Returns the list of created files (repo-relative). Never overwrites.
    """
    src = repo_root / "templates" / "intel"
    if not src.is_dir():
        raise FileNotFoundError(f"intel templates not found at {src}")
    dest_root = repo_root / "intel"
    created: list[Path] = []
    for template in sorted(src.rglob("*")):
        if not template.is_file():
            continue
        dest = dest_root / template.relative_to(src)
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, dest)
        created.append(dest.relative_to(repo_root))
    return created
