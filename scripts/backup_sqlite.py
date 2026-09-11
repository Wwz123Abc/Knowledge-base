from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


def backup_database(source: Path, output: Path) -> dict:
    source = source.resolve()
    output = output.resolve()
    if source == output:
        raise ValueError("Backup output must differ from the source database")
    if not source.exists():
        raise FileNotFoundError(source)
    output.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(source) as source_db, sqlite3.connect(output) as output_db:
        source_db.backup(output_db)
        integrity = output_db.execute("PRAGMA integrity_check").fetchone()[0]
        tables = output_db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"Backup integrity check failed: {integrity}")
    return {
        "source": str(source),
        "output": str(output),
        "size_bytes": output.stat().st_size,
        "table_count": tables,
        "integrity_check": integrity,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an online-safe SQLite development backup")
    parser.add_argument("--source", type=Path, default=Path("data/app.db"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path("backups", f"app-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
    print(json.dumps(backup_database(args.source, output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
