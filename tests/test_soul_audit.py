from plastic_promise.defense.soul_audit import SoulAuditor


def test_transparency_finds_git_repo_above_data_db(tmp_path):
    repo = tmp_path / "repo"
    db_dir = repo / "data" / "db"
    db_dir.mkdir(parents=True)
    (repo / ".git").mkdir()
    db_path = db_dir / "plastic_memory.db"
    db_path.touch()

    auditor = SoulAuditor(db_path=str(db_path))

    score, details = auditor._score_transparency()

    assert score == 0.75
    assert details["source"] == "git_dir_exists"
