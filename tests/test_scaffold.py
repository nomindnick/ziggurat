"""intel/ bootstrap tests: creates the skeleton, never clobbers the operator's notes."""

import pytest

from ziggurat.scaffold import ensure_intel_tree


@pytest.fixture()
def fake_repo(tmp_path):
    templates = tmp_path / "templates" / "intel"
    (templates / "opponents").mkdir(parents=True)
    (templates / "README.md").write_text("template readme")
    (templates / "opponents" / "README.md").write_text("opponents readme")
    return tmp_path


def test_creates_missing_files(fake_repo):
    created = ensure_intel_tree(fake_repo)
    assert sorted(str(p) for p in created) == [
        "intel/README.md",
        "intel/opponents/README.md",
    ]
    assert (fake_repo / "intel" / "opponents" / "README.md").read_text() == "opponents readme"


def test_never_overwrites_operator_notes(fake_repo):
    ensure_intel_tree(fake_repo)
    live = fake_repo / "intel" / "README.md"
    live.write_text("season notes the operator wrote")
    assert ensure_intel_tree(fake_repo) == []  # nothing to create
    assert live.read_text() == "season notes the operator wrote"


def test_backfills_new_templates_only(fake_repo):
    ensure_intel_tree(fake_repo)
    (fake_repo / "templates" / "intel" / "new_area.md").write_text("later addition")
    created = ensure_intel_tree(fake_repo)
    assert [str(p) for p in created] == ["intel/new_area.md"]


def test_missing_templates_dir_is_loud(tmp_path):
    with pytest.raises(FileNotFoundError):
        ensure_intel_tree(tmp_path)
