"""Consistent SQLite backup (all users) plus encryption key, never plaintext .env.

Run daily: python scripts/backup.py /secure/external/backup-directory
Store the target on a persistent disk; copy it off-host using your backup system.
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import load_config


def backup(destination: Path) -> Path:
    cfg = load_config()
    target = destination / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target.mkdir(parents=True, mode=0o700)
    paths = [cfg.db_path, *sorted((cfg.db_path.parent / "users").glob("*.db"))]
    for source in paths:
        if not source.exists():
            continue
        relative = source.relative_to(cfg.db_path.parent)
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(output) as dst:
            src.backup(dst)
        os.chmod(output, 0o600)
    key = cfg.data_dir / "secret.key"
    if key.exists():
        (target / "secret.key").write_bytes(key.read_bytes())
        os.chmod(target / "secret.key", 0o600)
    return target


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/backup.py /secure/backup-directory")
    print(backup(Path(sys.argv[1])))
