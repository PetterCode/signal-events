import pytest

from signal_events import config, db


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path / "attachments")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()
