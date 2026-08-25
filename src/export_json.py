"""最新のランキング予測を JSON で書き出す。

資産管理アプリ（Cloudflare Workers）が GitHub Pages 経由で取得して表示するための
軽量な受け渡しファイル。ダッシュボード HTML とは別に、データだけを提供する。

    python src/export_json.py --out site/prediction.json

出力例:
    {
      "predictedOn": "2026-08-25",
      "horizonDays": 7,
      "modelTag": "gbdt_h7_v1",
      "generatedAt": "2026-08-26T00:10:00Z",
      "items": [
        {"rank":1,"symbol":"GWEI","name":"ETHGas","score":0.6689,
         "price":0.0224929,"change7d":-5.5,"change30d":-9.1,"marketCap":45000000}
      ]
    }
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import REPORT_DIR, ensure_dirs, setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402

LOG = setup_logger("export_json")

DEFAULT_TOP_N = 30


def gather_latest(conn, horizon: int, top_n: int) -> dict:
    """predictions テーブルの最新予測を、表示に必要な情報とともに取り出す。"""
    head = conn.execute(
        """
        SELECT predicted_on, model_tag
        FROM predictions
        WHERE horizon_days = ?
        ORDER BY predicted_on DESC
        LIMIT 1
        """,
        (horizon,),
    ).fetchone()

    if not head:
        return {"predictedOn": None, "horizonDays": horizon, "items": []}

    predicted_on, model_tag = head[0], head[1]

    # 最新スナップショットから価格変化率・時価総額・銘柄名を補う
    rows = conn.execute(
        """
        SELECT p.rank, p.symbol, p.score, p.price_at_pred, p.coin_id,
               s.name, s.pct_7d, s.pct_30d, s.market_cap, s.image_url
        FROM predictions p
        LEFT JOIN snapshots s
          ON s.coin_id = p.coin_id
         AND s.snapshot_date = p.predicted_on
        WHERE p.horizon_days = ?
          AND p.predicted_on = ?
          AND p.model_tag = ?
        ORDER BY p.rank ASC
        LIMIT ?
        """,
        (horizon, predicted_on, model_tag, top_n),
    ).fetchall()

    items = []
    for r in rows:
        items.append(
            {
                "rank": r[0],
                "symbol": (r[1] or "").upper(),
                "coinId": r[4],
                "name": r[5] or r[1],
                "score": round(r[2], 4) if r[2] is not None else None,
                "price": r[3],
                "change7d": r[6],
                "change30d": r[7],
                "marketCap": r[8],
                # アプリ側はランキング銘柄のアイコンを持っていないため、
                # CoinGecko のロゴURLをそのまま渡す
                "iconUrl": r[9],
            }
        )

    return {
        "predictedOn": predicted_on,
        "horizonDays": horizon,
        "modelTag": model_tag,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": items,
    }


def run_export(out_path: str | None, horizon: int = 7, top_n: int = DEFAULT_TOP_N) -> Path:
    init_db()
    ensure_dirs()

    with connect() as conn:
        data = gather_latest(conn, horizon, top_n)

    target = Path(out_path) if out_path else (REPORT_DIR / "prediction.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    LOG.info(
        "予測 JSON を生成: %s (%d件 / 基準日 %s)",
        target,
        len(data["items"]),
        data.get("predictedOn"),
    )
    return target


def main() -> int:
    p = argparse.ArgumentParser(description="ランキング予測を JSON で書き出す")
    p.add_argument("--out", default=None, help="出力先（既定: reports/prediction.json）")
    p.add_argument("--horizon", type=int, default=7)
    p.add_argument("--top", type=int, default=DEFAULT_TOP_N)
    args = p.parse_args()

    run_export(args.out, horizon=args.horizon, top_n=args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
