"""A relative SIGNAL_EVENTS_DATA_DIR (or _DB_PATH/_ATTACHMENTS_DIR) used to
be stored as-is, relative to whatever the process's CWD happened to be at
startup -- fine for writing files, but webapp/routes.py's attachment_file
route (send_file(attachment["file_path"])) resolves a relative path
against the Flask app's root_path, not the CWD, so every attachment
silently 500'd despite the file genuinely existing on disk. Caught while
testing the training-scenario image attachments with a relative
SIGNAL_EVENTS_DATA_DIR, same as this test does."""

import importlib


def test_relative_data_dir_env_var_still_resolves_to_an_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIGNAL_EVENTS_DATA_DIR", "./relative_data")
    monkeypatch.delenv("SIGNAL_EVENTS_DB_PATH", raising=False)
    monkeypatch.delenv("SIGNAL_EVENTS_ATTACHMENTS_DIR", raising=False)

    from signal_events import config
    importlib.reload(config)

    assert config.DATA_DIR.is_absolute()
    assert config.DB_PATH.is_absolute()
    assert config.ATTACHMENTS_DIR.is_absolute()
    assert config.DATA_DIR == tmp_path / "relative_data"
