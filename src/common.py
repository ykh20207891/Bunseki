"""共通ユーティリティ: パス解決 / 設定読み込み / ロギング。

標準ライブラリのみに依存する（collect.py を確実に動かすため）。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
RAW_DIR = ROOT / "raw"
REPORT_DIR = ROOT / "reports"
DB_PATH = DATA_DIR / "crypto.db"
CONFIG_PATH = ROOT / "config.json"

DEFAULT_CONFIG = {
    "vs_currency": "usd",
    "top_n": 500,
    "per_page": 250,
    "coingecko_api_key": "",
    "coingecko_api_plan": "demo",
    "request_timeout": 45,
    "max_retry": 6,
    "sleep_between_pages": 8.0,
    "keep_raw_json": True,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as f:
            cfg.update(json.load(f))
    # 環境変数が設定されていれば API キーはそちらを優先（config.json に秘密を書かなくて済む）
    env_key = os.environ.get("COINGECKO_API_KEY", "").strip()
    if env_key:
        cfg["coingecko_api_key"] = env_key
    return cfg


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR, RAW_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def setup_logger(name: str) -> logging.Logger:
    ensure_dirs()
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(LOG_DIR / f"{name}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger
