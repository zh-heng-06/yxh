from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "local-server"))
import server  # noqa: E402


def main() -> int:
    before = datetime(2026, 7, 18, 18, 59, tzinfo=timezone(timedelta(hours=8)))
    after = datetime(2026, 7, 18, 19, 1, tzinfo=timezone(timedelta(hours=8)))
    assert server.next_daily_backup_at(before) == before.replace(hour=19, minute=0)
    assert server.next_daily_backup_at(after) == after.replace(day=19, hour=19, minute=0)

    with tempfile.TemporaryDirectory(prefix="zhanggui-backup-") as directory:
        server.DB_PATH = Path(directory) / "store.db"
        server.init_db()
        backup = server.daily_backup(True)
        assert backup.exists()
        assert server.database_file_check(backup) == "ok"
        state = server.backup_state()
        assert state["schedule"] == "每天19:00"
        assert state["verified"] is True
        assert state["lastBackup"] == backup.name

    print("PASS | 每天19:00调度、在线备份和备份自检")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
