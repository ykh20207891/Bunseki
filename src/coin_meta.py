"""銘柄のチェーンと取扱取引所を取得して保存する。

CoinGecko の /coins/{id} は 1銘柄ずつしか取れずレート制限も厳しいので、
**予測ランキングに載る上位数銘柄だけ**を対象にする。
取得済みのものは既定で7日間キャッシュし、無駄な呼び出しを避ける。

    python src/coin_meta.py --top 10
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect import _base_and_headers, http_get_text  # noqa: E402
from common import load_config, setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402

LOG = setup_logger("coin_meta")

CACHE_DAYS = 7
# 無料APIは体感 10-30req/分。10銘柄なら余裕を見てこの間隔で回す。
SLEEP_SEC = 6.0
MAX_EXCHANGES = 5

# 表示用のチェーン名（CoinGecko の platform id → 一般的な呼び名）
CHAIN_LABELS = {
    "ethereum": "Ethereum",
    "binance-smart-chain": "BNB Chain",
    "solana": "Solana",
    "polygon-pos": "Polygon",
    "arbitrum-one": "Arbitrum",
    "optimistic-ethereum": "Optimism",
    "avalanche": "Avalanche",
    "base": "Base",
    "tron": "Tron",
    "sui": "Sui",
    "aptos": "Aptos",
    "the-open-network": "TON",
    "cardano": "Cardano",
    "polkadot": "Polkadot",
    # 自分自身がチェーンの基盤通貨（platforms が空になる）
    "bitcoin": "Bitcoin",
    "ripple": "XRP Ledger",
    "litecoin": "Litecoin",
    "dogecoin": "Dogecoin",
    "monero": "Monero",
    "cosmos": "Cosmos",
    "near": "NEAR",
    "algorand": "Algorand",
    "stellar": "Stellar",
    "hedera-hashgraph": "Hedera",
    "internet-computer": "ICP",
    "filecoin": "Filecoin",
    "celo": "Celo",
    "flow": "Flow",
    "harmony": "Harmony",
}

# 日本から使いやすい/主要な取引所を優先して表示する
PREFERRED_EXCHANGES = [
    "Binance", "Bybit", "OKX", "Coinbase Exchange", "Kraken",
    "KuCoin", "Gate.io", "MEXC", "HTX", "Upbit", "Bitget",
]


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coin_meta (
            coin_id    TEXT PRIMARY KEY,
            chains     TEXT,   -- JSON 配列（表示名）
            exchanges  TEXT,   -- JSON 配列（取引所名）
            fetched_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _needs_refresh(conn, coin_id: str) -> bool:
    row = conn.execute(
        "SELECT fetched_at FROM coin_meta WHERE coin_id = ? "
        "AND fetched_at > datetime('now', ?)",
        (coin_id, f"-{CACHE_DAYS} day"),
    ).fetchone()
    return row is None


def _target_coins(conn, top_n: int) -> list[str]:
    """直近の予測（月曜優先）の上位 top_n 銘柄。"""
    row = conn.execute(
        "SELECT predicted_on, model_tag FROM predictions "
        "WHERE horizon_days = 7 ORDER BY predicted_on DESC LIMIT 1"
    ).fetchone()
    if not row:
        return []

    return [
        r[0]
        for r in conn.execute(
            "SELECT coin_id FROM predictions "
            "WHERE horizon_days = 7 AND predicted_on = ? AND model_tag = ? "
            "ORDER BY rank ASC LIMIT ?",
            (row[0], row[1], top_n),
        )
    ]


def _pick_exchanges(tickers: list[dict]) -> list[str]:
    """取扱取引所を、主要どころ優先で最大 MAX_EXCHANGES 件返す。"""
    names = []
    for t in tickers or []:
        name = ((t.get("market") or {}).get("name") or "").strip()
        if name and name not in names:
            names.append(name)

    preferred = [n for n in PREFERRED_EXCHANGES if n in names]
    others = [n for n in names if n not in preferred]
    return (preferred + others)[:MAX_EXCHANGES]


def _pick_chains(platforms: dict, payload: dict) -> list[str]:
    """発行チェーン。トークンでない（自分自身がチェーン）場合はそれを返す。"""
    chains = []
    for key in (platforms or {}):
        if not key:
            continue
        chains.append(CHAIN_LABELS.get(key, key.replace("-", " ").title()))

    if chains:
        return chains[:3]

    # BTC/ETH/SOL のような基盤通貨は platforms が空になる。
    # asset_platform_id が無い＝トークンではないので、自分自身をチェーンとする。
    if not payload.get("asset_platform_id"):
        own = payload.get("id") or ""
        label = CHAIN_LABELS.get(own)
        if label:
            return [label]
        name = (payload.get("name") or "").strip()
        if name:
            return [name]

    return []


def run(top_n: int = 10) -> int:
    init_db()
    cfg = load_config()
    base, headers = _base_and_headers(cfg)

    fetched = 0
    with connect() as conn:
        _ensure_table(conn)
        targets = [c for c in _target_coins(conn, top_n) if _needs_refresh(conn, c)]

        if not targets:
            LOG.info("チェーン/取引所の再取得が必要な銘柄はありません")
            return 0

        LOG.info("チェーン/取引所を取得: %d件", len(targets))

        for coin_id in targets:
            url = (
                f"{base}/coins/{coin_id}?localization=false&tickers=true"
                "&market_data=false&community_data=false&developer_data=false"
            )
            try:
                payload = json.loads(http_get_text(url, headers, 30, 2))
            except Exception as e:  # noqa: BLE001
                # 保存しないので、次回の実行で再挑戦される
                LOG.warning("%s の取得に失敗: %s", coin_id, e)
                time.sleep(SLEEP_SEC)
                continue

            chains = _pick_chains(payload.get("platforms") or {}, payload)
            exchanges = _pick_exchanges(payload.get("tickers") or [])

            conn.execute(
                "INSERT INTO coin_meta (coin_id, chains, exchanges, fetched_at) "
                "VALUES (?, ?, ?, datetime('now')) "
                "ON CONFLICT(coin_id) DO UPDATE SET "
                "chains=excluded.chains, exchanges=excluded.exchanges, "
                "fetched_at=excluded.fetched_at",
                (
                    coin_id,
                    json.dumps(chains, ensure_ascii=False),
                    json.dumps(exchanges, ensure_ascii=False),
                ),
            )
            conn.commit()
            fetched += 1
            time.sleep(SLEEP_SEC)

    LOG.info("チェーン/取引所の取得が完了: %d件", fetched)
    return fetched


def main() -> int:
    p = argparse.ArgumentParser(description="銘柄のチェーンと取扱取引所を取得")
    p.add_argument("--top", type=int, default=10)
    args = p.parse_args()
    run(top_n=args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
