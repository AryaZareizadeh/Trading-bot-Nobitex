"""Runtime paths and atomic credential-file updates, independent of cwd."""
import os
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_env_lock = threading.RLock()


def env_path() -> Path:
    return Path(os.environ.get("TRADER_ENV_FILE", str(ROOT / ".env"))).resolve()


def load_environment() -> None:
    from dotenv import load_dotenv
    load_dotenv(env_path(), override=False)


def data_dir() -> Path:
    return Path(os.environ.get("TRADER_DATA_DIR", str(ROOT / ".runtime"))).resolve()


def update_environment(values: dict[str, str | None]) -> None:
    from dotenv import dotenv_values
    with _env_lock:
        path = env_path()
        existing = dict(dotenv_values(path)) if path.exists() else {}
        for key, value in values.items():
            if not key.replace("_", "").isalnum() or (value is not None and any(c in value for c in "\r\n\x00")):
                raise ValueError("Invalid environment value")
            if value is None:
                existing.pop(key, None)
            else:
                existing[key] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                for key, value in existing.items():
                    quoted = str(value or "").replace("\\", "\\\\").replace("'", "\\'")
                    stream.write(f"{key}='{quoted}'\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
