import os
import tempfile
from pathlib import Path

# Set these before importing any application module: never load operator secrets.
_session_dir = tempfile.TemporaryDirectory(prefix="nobitex-tests-")
os.environ["TRADER_DATA_DIR"] = _session_dir.name
os.environ["TRADER_ENV_FILE"] = str(Path(_session_dir.name) / ".env")
os.environ["TRADER_OFFLINE"] = "true"
for _key in tuple(os.environ):
    if _key.startswith("NOBITEX_"):
        os.environ.pop(_key)

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADER_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("TRADER_OFFLINE", "true")
    protected = {Path(__file__).resolve().parents[1] / name for name in (".env", "bots.json", "optimizer.json", "tester.json")}
    import builtins
    import io
    real_open, real_io_open, real_replace = builtins.open, io.open, os.replace
    def guard(opener):
        def checked(file, mode="r", *args, **kwargs):
            if not isinstance(file, int) and Path(file).resolve() in protected and any(flag in mode for flag in "wax+"):
                raise AssertionError("Test attempted to modify operator configuration")
            return opener(file, mode, *args, **kwargs)
        return checked
    def checked_replace(source, target, *args, **kwargs):
        if Path(target).resolve() in protected:
            raise AssertionError("Test attempted to replace operator configuration")
        return real_replace(source, target, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", guard(real_open))
    monkeypatch.setattr(io, "open", guard(real_io_open))
    monkeypatch.setattr(os, "replace", checked_replace)
    # Network is never a test dependency; fakes replace requests methods locally.
    import requests
    def blocked(*args, **kwargs):
        raise AssertionError("Network access forbidden in tests")
    monkeypatch.setattr(requests.Session, "request", blocked)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    # This module keeps a direct imported alias. Block it too; individual rule
    # parser/refresh tests inject their own response rather than reaching HTTP.
    import position_limits
    monkeypatch.setattr(position_limits, "urlopen", blocked)
