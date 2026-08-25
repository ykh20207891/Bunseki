"""SQLite スキーマ定義と接続ヘルパ（標準ライブラリのみ）。"""
from __future__ import annotations

import sqlite3

from common import DB_PATH, ensure_dirs

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 日次スナップショット（1日1銘柄1行）
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_date TEXT    NOT NULL,   -- UTC の YYYY-MM-DD
    fetched_at    TEXT    NOT NULL,   -- 実際に取得した UTC 時刻 (ISO8601)
    source        TEXT    NOT NULL,   -- 'coingecko'
    coin_id       TEXT    NOT NULL,   -- CoinGecko の id (例: bitcoin)
    symbol        TEXT,
    name          TEXT,
    mcap_rank     INTEGER,
    price         REAL,
    market_cap    REAL,
    fdv           REAL,               -- fully diluted valuation
    total_volume  REAL,
    high_24h      REAL,
    low_24h       REAL,
    pct_1h        REAL,
    pct_24h       REAL,
    pct_7d        REAL,
    pct_14d       REAL,
    pct_30d       REAL,
    pct_200d      REAL,
    pct_1y        REAL,
    circulating_supply REAL,
    total_supply       REAL,
    max_supply         REAL,
    ath           REAL,
    ath_change_pct REAL,
    ath_date      TEXT,
    atl           REAL,
    atl_change_pct REAL,
    atl_date      TEXT,
    image_url     TEXT,               -- CoinGecko のロゴ画像 URL
    PRIMARY KEY (snapshot_date, coin_id)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_coin ON snapshots(coin_id, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(snapshot_date);

-- 市場全体のスナップショット（BTCドミナンス等の地合い特徴量用）
CREATE TABLE IF NOT EXISTS global_snapshots (
    snapshot_date       TEXT PRIMARY KEY,
    fetched_at          TEXT NOT NULL,
    total_market_cap    REAL,
    total_volume        REAL,
    btc_dominance       REAL,
    eth_dominance       REAL,
    active_cryptos      INTEGER,
    mcap_change_24h_pct REAL
);

-- 収集ジョブの実行記録（欠損日の把握・リカバリ判定に使う）
CREATE TABLE IF NOT EXISTS collect_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,      -- 'ok' | 'partial' | 'error'
    rows_written  INTEGER DEFAULT 0,
    message       TEXT
);
CREATE INDEX IF NOT EXISTS idx_collect_log_date ON collect_log(snapshot_date);

-- 過去データのバックフィル（/coins/{id}/market_chart から取得した日次系列）
-- snapshots より項目は少ないが、価格・時価総額・出来高が揃うので学習には十分。
-- 注意: バックフィル対象は「今日の上位500銘柄」なので生存者バイアスがある。
CREATE TABLE IF NOT EXISTS price_history (
    coin_id      TEXT NOT NULL,
    date         TEXT NOT NULL,   -- UTC の YYYY-MM-DD
    price        REAL,
    market_cap   REAL,
    total_volume REAL,
    source       TEXT NOT NULL DEFAULT 'coingecko_market_chart',
    PRIMARY KEY (coin_id, date)
);
CREATE INDEX IF NOT EXISTS idx_ph_date ON price_history(date);

-- バックフィルの進捗（中断しても再開できるようにする）
CREATE TABLE IF NOT EXISTS backfill_progress (
    coin_id    TEXT PRIMARY KEY,
    done_at    TEXT,
    days       INTEGER,
    rows       INTEGER,
    status     TEXT,              -- 'ok' | 'error'
    message    TEXT
);

-- ニュース記事（RSS から日次収集）
CREATE TABLE IF NOT EXISTS news_articles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    link         TEXT UNIQUE NOT NULL,
    published_at TEXT,               -- ISO8601 UTC
    pub_date     TEXT,               -- UTC の YYYY-MM-DD
    source       TEXT,               -- 媒体名
    title        TEXT,
    summary      TEXT,
    sentiment    REAL,               -- -1(弱気) 〜 +1(強気)
    lang         TEXT,               -- 'en' | 'ja'（元記事の言語）
    title_ja     TEXT,               -- 日本語見出し（ja記事は原文、en記事は機械翻訳）
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_date ON news_articles(pub_date);

-- 記事と銘柄の紐付け（1記事が複数銘柄に言及しうる）
CREATE TABLE IF NOT EXISTS news_mentions (
    article_id INTEGER NOT NULL,
    coin_id    TEXT    NOT NULL,
    pub_date   TEXT    NOT NULL,
    matched_by TEXT,                 -- 'name' | 'symbol'
    PRIMARY KEY (article_id, coin_id)
);
CREATE INDEX IF NOT EXISTS idx_nm_coin_date ON news_mentions(coin_id, pub_date);

-- 外部ソースから取った日次のニュース集計（GDELT など。記事本体は持たない）
CREATE TABLE IF NOT EXISTS news_daily (
    coin_id       TEXT NOT NULL,
    date          TEXT NOT NULL,
    source        TEXT NOT NULL,     -- 'gdelt'
    article_count REAL,
    tone          REAL,              -- 平均トーン（GDELT は概ね -10〜+10）
    PRIMARY KEY (coin_id, date, source)
);
CREATE INDEX IF NOT EXISTS idx_nd_date ON news_daily(date);

CREATE TABLE IF NOT EXISTS news_backfill_progress (
    coin_id TEXT PRIMARY KEY,
    done_at TEXT,
    rows    INTEGER,
    status  TEXT,
    message TEXT
);

-- 銘柄アイコン。画像そのものは icons/ にファイルとして置き、ここでは所在と状態を管理する。
-- （DB に BLOB で持つと WAL が肥大化するのと、他用途で使い回しにくいためファイルにした）
CREATE TABLE IF NOT EXISTS coin_icons (
    coin_id    TEXT PRIMARY KEY,
    symbol     TEXT,
    source_url TEXT,
    filename   TEXT,               -- icons/ 配下のファイル名
    mime       TEXT,
    bytes      INTEGER,
    size       TEXT,               -- 'thumb' | 'small' | 'large'
    fetched_at TEXT,
    status     TEXT,               -- 'ok' | 'error'
    message    TEXT
);

-- ニュース媒体のサイトアイコン（ファビコン）
CREATE TABLE IF NOT EXISTS source_icons (
    source     TEXT PRIMARY KEY,
    site_url   TEXT,
    filename   TEXT,               -- icons/_sources/ 配下のファイル名
    mime       TEXT,
    bytes      INTEGER,
    fetched_at TEXT,
    status     TEXT,
    message    TEXT
);

-- 予測の記録（後日、実際の結果と突き合わせて答え合わせするため）
CREATE TABLE IF NOT EXISTS predictions (
    predicted_on  TEXT NOT NULL,      -- 予測を出した基準日 (snapshot_date)
    horizon_days  INTEGER NOT NULL,   -- 予測期間
    model_tag     TEXT NOT NULL,      -- モデル識別子
    coin_id       TEXT NOT NULL,
    symbol        TEXT,
    score         REAL,               -- モデル出力スコア
    rank          INTEGER,            -- スコア降順の順位
    price_at_pred REAL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (predicted_on, horizon_days, model_tag, coin_id)
);
CREATE INDEX IF NOT EXISTS idx_pred_date ON predictions(predicted_on, horizon_days);
"""


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


# 既存 DB に後から足した列。CREATE TABLE は既存テーブルには効かないので明示的に追加する。
MIGRATIONS = [
    ("snapshots", "image_url", "ALTER TABLE snapshots ADD COLUMN image_url TEXT"),
    ("news_articles", "lang", "ALTER TABLE news_articles ADD COLUMN lang TEXT"),
    ("news_articles", "title_ja", "ALTER TABLE news_articles ADD COLUMN title_ja TEXT"),
]


def _migrate(conn) -> None:
    for table, column, sql in MIGRATIONS:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if cols and column not in cols:
            conn.execute(sql)


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


if __name__ == "__main__":
    init_db()
    with connect() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("DB:", DB_PATH)
    print("tables:", tables)
