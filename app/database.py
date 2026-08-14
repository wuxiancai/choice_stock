from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_quotes (
  trade_date TEXT NOT NULL, ts_code TEXT NOT NULL, name TEXT, open REAL, high REAL,
  low REAL, close REAL, pct_chg REAL, vol REAL, amount REAL, turnover_rate REAL,
  volume_ratio REAL, total_mv REAL, pe REAL, pb REAL, source TEXT NOT NULL,
  PRIMARY KEY(trade_date, ts_code)
);
CREATE TABLE IF NOT EXISTS sector_snapshots (
  trade_date TEXT NOT NULL, sector_code TEXT NOT NULL, sector_name TEXT NOT NULL,
  pct_chg REAL, amount REAL, main_net_inflow REAL, source TEXT NOT NULL,
  PRIMARY KEY(trade_date, sector_code)
);
CREATE TABLE IF NOT EXISTS stock_signals (
  trade_date TEXT NOT NULL, ts_code TEXT NOT NULL, name TEXT, score REAL NOT NULL,
  macd REAL, kdj_j REAL, rsi14 REAL, boll_position REAL, nine_turn INTEGER,
  main_net_inflow REAL, volume_ratio REAL, turnover_rate REAL, amount REAL,
  total_mv REAL, pe REAL, pb REAL, pct_chg REAL,
  reasons TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(trade_date, ts_code)
);
CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
  trade_date TEXT, status TEXT NOT NULL, source TEXT, message TEXT, quote_count INTEGER DEFAULT 0,
  sector_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS system_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, level TEXT NOT NULL,
  source TEXT NOT NULL, message TEXT NOT NULL
);
"""


def initialize() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        quote_columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_quotes)")}
        for column, definition in {
            "main_net_inflow": "REAL DEFAULT 0", "turnover_rate": "REAL",
            "volume_ratio": "REAL", "total_mv": "REAL", "pe": "REAL", "pb": "REAL",
        }.items():
            if column not in quote_columns:
                conn.execute(f"ALTER TABLE daily_quotes ADD COLUMN {column} {definition}")
        signal_columns = {row[1] for row in conn.execute("PRAGMA table_info(stock_signals)")}
        for column, definition in {
            "main_net_inflow": "REAL", "volume_ratio": "REAL", "turnover_rate": "REAL",
            "amount": "REAL", "total_mv": "REAL", "pe": "REAL", "pb": "REAL", "pct_chg": "REAL",
        }.items():
            if column not in signal_columns:
                conn.execute(f"ALTER TABLE stock_signals ADD COLUMN {column} {definition}")


@contextmanager
def connect():
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
