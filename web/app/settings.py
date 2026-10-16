from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    max_request_bytes: int
    offline_after_seconds: int
    source_inactive_after_seconds: int
    history_retention_days: int

    @classmethod
    def from_environment(cls) -> "Settings":
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        return cls(
            database_path=Path(os.environ.get("DATABASE_PATH", str(data_dir / "naftamon.sqlite3"))),
            max_request_bytes=max(1, int(os.environ.get("MAX_REQUEST_BYTES", str(8 * 1024 * 1024)))),
            offline_after_seconds=max(1, int(os.environ.get("OFFLINE_AFTER_SECONDS", "900"))),
            source_inactive_after_seconds=max(1, int(os.environ.get("SOURCE_INACTIVE_AFTER_SECONDS", "86400"))),
            history_retention_days=max(0, int(os.environ.get("HISTORY_RETENTION_DAYS", "90"))),
        )
