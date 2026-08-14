from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", "data"))
    tushare_token: str = os.getenv("TUSHARE_TOKEN", "")
    timezone: str = os.getenv("TIMEZONE", "Asia/Shanghai")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "choice_stock.db"


settings = Settings()
