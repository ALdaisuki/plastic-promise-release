from __future__ import annotations

import json
import sqlite3

from plastic_promise.core.traceability import ensure_traceability_schema
from scripts.manage_index_outbox import main


def _database(tmp_path):
    path = tmp_path / "outbox.db"
    connection = sqlite3.connect(path)
    ensure_traceability_schema(connection)
    connection.execute(
        "INSERT INTO store_outbox ("
        "outbox_id, tool_name, status, payload_json, metadata_json, created_at, "
        "attempt_count, updated_at, next_attempt_at, error_class, error_message"
        ") VALUES (?, ?, ?, '{}', '{}', ?, ?, ?, '', ?, ?)",
        (
            "outbox-terminal",
            "memory_index",
            "blocked",
            "2026-07-23T00:00:00Z",
            3,
            "2026-07-23T00:00:00Z",
            "ProviderHTTPError",
            "secret-provider-response-must-not-print",
        ),
    )
    connection.commit()
    connection.close()
    return path


def test_requeue_is_read_only_without_apply_and_never_prints_payload_or_error(tmp_path, capsys):
    path = _database(tmp_path)

    assert main(["--db", str(path), "requeue", "outbox-terminal"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["applied"] is False
    assert payload["jobs"][0]["requeue_eligible"] is True
    assert "secret-provider-response-must-not-print" not in output
    connection = sqlite3.connect(path)
    assert (
        connection.execute(
            "SELECT status FROM store_outbox WHERE outbox_id = 'outbox-terminal'"
        ).fetchone()[0]
        == "blocked"
    )
    connection.close()


def test_requeue_requires_apply_and_resets_only_explicit_terminal_job(tmp_path, capsys):
    path = _database(tmp_path)

    assert main(["--db", str(path), "requeue", "outbox-terminal", "--apply"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "applied": True,
        "rejected_ids": [],
        "requested": 1,
        "requeued_ids": ["outbox-terminal"],
    }
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT status, attempt_count, error_class, error_message "
        "FROM store_outbox WHERE outbox_id = 'outbox-terminal'"
    ).fetchone() == ("pending", 0, "", "")
    connection.close()


def test_missing_database_is_never_created(tmp_path, capsys):
    path = tmp_path / "missing.db"

    assert main(["--db", str(path), "inspect", "outbox-terminal"]) == 2

    assert not path.exists()
    assert "database_must_be_existing_regular_file" in capsys.readouterr().err
