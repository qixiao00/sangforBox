from pathlib import Path

from af_app.portable import prepare_directories


def test_prepare_directories_honors_overrides_and_migrates_legacy_state(tmp_path, monkeypatch):
    installation = tmp_path / "installation"
    state = tmp_path / "state"
    database = tmp_path / "custom" / "af.db"
    results = tmp_path / "exports"
    state.mkdir(parents=True)
    (state / "af.db").write_bytes(b"main")
    (state / "policy-review.db").write_bytes(b"review")
    (state / "results").mkdir()
    (state / "results" / "review.csv").write_text("result", encoding="utf-8")
    monkeypatch.setenv("AF_STATE_DIR", str(state))
    monkeypatch.setenv("AF_DATABASE_PATH", str(database))
    monkeypatch.setenv("AF_OUTPUT_DIR", str(results))

    actual_database, actual_results, logs = prepare_directories(installation)

    assert actual_database == database
    assert actual_results == results
    assert logs == state / "logs"
    assert database.read_bytes() == b"main"
    assert database.with_name("policy-review.db").read_bytes() == b"review"
    assert (results / "review.csv").read_text(encoding="utf-8") == "result"


def test_prepare_directories_defaults_to_portable_folder_and_migrates_local_state(tmp_path, monkeypatch):
    installation = tmp_path / "SangforBox-Portable"
    legacy = tmp_path / "LocalAppData" / "SangforBox"
    legacy.mkdir(parents=True)
    (legacy / "af.db").write_bytes(b"legacy-main")
    (legacy / "policy-review.db").write_bytes(b"legacy-review")
    (legacy / "results").mkdir()
    (legacy / "results" / "review.csv").write_text("legacy-result", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.delenv("AF_STATE_DIR", raising=False)
    monkeypatch.delenv("AF_DATABASE_PATH", raising=False)
    monkeypatch.delenv("AF_OUTPUT_DIR", raising=False)

    database, results, logs = prepare_directories(installation)

    assert database == installation / "data" / "af.db"
    assert results == installation / "results"
    assert logs == installation / "logs"
    assert database.read_bytes() == b"legacy-main"
    assert database.with_name("policy-review.db").read_bytes() == b"legacy-review"
    assert (results / "review.csv").read_text(encoding="utf-8") == "legacy-result"


def test_prepare_directories_does_not_overwrite_existing_state(tmp_path, monkeypatch):
    installation = tmp_path / "installation"
    state = tmp_path / "state"
    (installation / "data").mkdir(parents=True)
    (installation / "results").mkdir()
    (installation / "data" / "af.db").write_bytes(b"legacy-main")
    (installation / "data" / "policy-review.db").write_bytes(b"legacy-review")
    (installation / "results" / "old.csv").write_text("legacy", encoding="utf-8")
    state.mkdir()
    (state / "af.db").write_bytes(b"current-main")
    (state / "policy-review.db").write_bytes(b"current-review")
    (state / "results").mkdir()
    (state / "results" / "current.csv").write_text("current", encoding="utf-8")
    monkeypatch.setenv("AF_STATE_DIR", str(state))
    monkeypatch.delenv("AF_DATABASE_PATH", raising=False)
    monkeypatch.delenv("AF_OUTPUT_DIR", raising=False)

    prepare_directories(installation)

    assert (state / "af.db").read_bytes() == b"current-main"
    assert (state / "policy-review.db").read_bytes() == b"current-review"
    assert not (state / "results" / "old.csv").exists()
