import os

from plastic_promise.core.paths import default_db_path, get_db_path


def test_default_db_path_is_canonical_data_db(monkeypatch):
    monkeypatch.delenv("PLASTIC_DB_PATH", raising=False)

    path = default_db_path()
    assert path.endswith(os.path.join("data", "db", "plastic_memory.db"))
    assert get_db_path() == path
