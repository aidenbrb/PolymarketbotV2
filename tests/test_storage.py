import json
import threading

import pytest

from polymarket_bot import storage


def test_save_json_atomically_replaces_existing_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"old": true}', encoding="utf-8")

    storage.save_json(path, {"new": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_failed_atomic_replace_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(storage.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError, match="disk"):
        storage.save_json(path, {"new": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_transient_permission_error_retries_atomic_replace(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"old": true}', encoding="utf-8")
    real_replace = storage.os.replace
    attempts = []

    def flaky_replace(source, destination):
        attempts.append((source, destination))
        if len(attempts) < 3:
            raise PermissionError("temporarily locked")
        real_replace(source, destination)

    monkeypatch.setattr(storage.os, "replace", flaky_replace)
    monkeypatch.setattr(storage.time, "sleep", lambda _delay: None)

    storage.save_json(path, {"new": True})

    assert len(attempts) == 3
    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_persistent_permission_error_is_bounded_and_preserves_old_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"old": true}', encoding="utf-8")
    attempts = []

    def locked_replace(*_args):
        attempts.append(1)
        raise PermissionError("still locked")

    monkeypatch.setattr(storage.os, "replace", locked_replace)
    monkeypatch.setattr(storage.time, "sleep", lambda _delay: None)

    with pytest.raises(PermissionError, match="still locked"):
        storage.save_json(path, {"new": True})

    assert len(attempts) == 4
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_concurrent_json_list_appends_do_not_lose_rows(tmp_path):
    path = tmp_path / "rows.json"
    threads = [
        threading.Thread(target=storage.append_json_list, args=(path, {"id": index}))
        for index in range(20)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rows = storage.load_json(path)
    assert {row["id"] for row in rows} == set(range(20))
