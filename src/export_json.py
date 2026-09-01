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

DEFAULT_TOP_N = 10

# ウォークフォワード検証(backtest_oos_h7.csv / 39,268件・142期間)から集計した
# 「その順位帯の銘柄が7日後に上昇していた実績割合」。
# モデルは上昇/下落そのものを予測しないため、断定の代わりにこの実績を示す。
BACKTEST_CSV = "backtest_oos_h7.csv"
DEFAULT_BANDS = {
    "top3": {"upRate": 0.522, "avgReturn": -0.0026},
    "top10": {"upRate": 0.499, "avgReturn": 0.0041},
    "top25": {"upRate": 0.467, "avgReturn": -0.0004},
    "mid": {"upRate": 0.464, "avgReturn": -0.0042},
    "low": {"upRate": 0.460, "avgReturn": -0.0050},
    "bottom": {"upRate": 0.440, "avgReturn": -0.0128},
    "overall": {"upRate": 0.462, "avgReturn": -0.0054},
}


def _band_key(rank: int, total: int) -> str:
    """順位を検証済みの順位帯に対応づける。"""
    if total <= 0:
        return "overall"
    pct = rank / total
    if pct <= 0.03:
        return "top3"
    if pct <= 0.10:
        return "top10"
    if pct <= 0.25:
        return "top25"
    if pct <= 0.50:
        return "mid"
    if pct <= 0.75:
        return "low"
    return "bottom"


def compute_bands() -> dict:
    """検証結果CSVがあれば実測から集計し、無ければ既定値を使う。"""
    path = REPORT_DIR / BACKTEST_CSV
    if not path.exists():
        return DEFAULT_BANDS

    try:
        import pandas as pd

        df = pd.read_csv(path, encoding="utf-8-sig")
        df["rank"] = df.groupby("date")["score"].rank(ascending=False, method="first")
        df["pct"] = df["rank"] / df.groupby("date")["score"].transform("size")

        bands = {}
        edges = [(0, .03, "top3"), (.03, .10, "top10"), (.10, .25, "top25"),
                 (.25, .50, "mid"), (.50, .75, "low"), (.75, 1.01, "bottom")]
        for lo, hi, key in edges:
            s = df[(df["pct"] > lo) & (df["pct"] <= hi)]["fwd_ret"]
            if len(s) == 0:
                continue
            bands[key] = {
                "upRate": round(float((s > 0).mean()), 4),
                "avgReturn": round(float(s.mean()), 5),
                "samples": int(len(s)),
            }
        bands["overall"] = {
            "upRate": round(float((df["fwd_ret"] > 0).mean()), 4),
            "avgReturn": round(float(df["fwd_ret"].mean()), 5),
            "samples": int(len(df)),
            "periods": int(df["date"].nunique()),
        }
        return bands
    except Exception as e:  # noqa: BLE001
        LOG.warning("検証結果の集計に失敗したため既定値を使います: %s", e)
        return DEFAULT_BANDS


def _latest_monday(conn, horizon: int) -> tuple[str, str] | None:
    """直近の「月曜日に出した予測」を返す。無ければ最新の予測で代替する。

    データ収集は毎日続けるが、表示するランキングは週1回（月曜）に固定する。
    毎日順位が入れ替わると、7日後の答え合わせができないため。
    """
    row = conn.execute(
        """
        SELECT predicted_on, model_tag
        FROM predictions
        WHERE horizon_days = ?
          AND CAST(strftime('%w', predicted_on) AS INTEGER) = 1
        ORDER BY predicted_on DESC
        LIMIT 1
        """,
        (horizon,),
    ).fetchone()
    if row:
        return row[0], row[1]

    # まだ月曜の予測が無い運用初期は、最新の予測をそのまま使う
    row = conn.execute(
        "SELECT predicted_on, model_tag FROM predictions "
        "WHERE horizon_days = ? ORDER BY predicted_on DESC LIMIT 1",
        (horizon,),
    ).fetchone()
    return (row[0], row[1]) if row else None


def _price_on_or_after(conn, coin_id: str, date: str) -> float | None:
    """指定日以降で最も近い日の価格。snapshots に無ければ price_history を見る。"""
    row = conn.execute(
        "SELECT price FROM snapshots WHERE coin_id = ? AND snapshot_date >= ? "
        "ORDER BY snapshot_date ASC LIMIT 1",
        (coin_id, date),
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0])

    row = conn.execute(
        "SELECT price FROM price_history WHERE coin_id = ? AND date >= ? "
        "ORDER BY date ASC LIMIT 1",
        (coin_id, date),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def gather_review(conn, horizon: int, top_n: int, current_on: str | None) -> dict | None:
    """1つ前の月曜予測について、実際にどうなったかを集計する（答え合わせ）。"""
    row = conn.execute(
        """
        SELECT predicted_on, model_tag
        FROM predictions
        WHERE horizon_days = ?
          AND CAST(strftime('%w', predicted_on) AS INTEGER) = 1
          AND predicted_on < COALESCE(?, '9999-12-31')
        ORDER BY predicted_on DESC
        LIMIT 1
        """,
        (horizon, current_on),
    ).fetchone()
    if not row:
        return None

    predicted_on, model_tag = row[0], row[1]

    # 予測から horizon 日後の日付
    target = conn.execute(
        "SELECT date(?, ?)", (predicted_on, f"+{horizon} day")
    ).fetchone()[0]

    rows = conn.execute(
        """
        SELECT p.rank, p.symbol, p.coin_id, p.price_at_pred, s.name, s.image_url
        FROM predictions p
        LEFT JOIN (
            SELECT coin_id, name, image_url, MAX(snapshot_date) FROM snapshots
            GROUP BY coin_id
        ) s ON s.coin_id = p.coin_id
        WHERE p.horizon_days = ? AND p.predicted_on = ? AND p.model_tag = ?
        ORDER BY p.rank ASC
        LIMIT ?
        """,
        (horizon, predicted_on, model_tag, top_n),
    ).fetchall()

    items = []
    ups = 0
    total_ret = 0.0
    scored = 0

    for r in rows:
        rank, symbol, coin_id, price_before = r[0], r[1], r[2], r[3]
        price_after = _price_on_or_after(conn, coin_id, target)

        change = None
        if price_before and price_after and price_before > 0:
            change = (price_after - price_before) / price_before * 100
            scored += 1
            total_ret += change
            if change > 0:
                ups += 1

        items.append(
            {
                "rank": rank,
                "symbol": (symbol or "").upper(),
                "name": r[4] or symbol,
                "iconUrl": r[5],
                "priceBefore": price_before,
                "priceAfter": price_after,
                "changePct": round(change, 2) if change is not None else None,
            }
        )

    if not items:
        return None

    return {
        "predictedOn": predicted_on,
        "evaluatedOn": target,
        "items": items,
        "upCount": ups,
        "scored": scored,
        "upRate": round(ups / scored, 4) if scored else None,
        "avgChangePct": round(total_ret / scored, 2) if scored else None,
    }


def gather_latest(conn, horizon: int, top_n: int) -> dict:
    """表示用の予測（直近の月曜分）を、必要な情報とともに取り出す。"""
    head = _latest_monday(conn, horizon)

    if not head:
        return {"predictedOn": None, "horizonDays": horizon, "items": []}

    predicted_on, model_tag = head[0], head[1]

    # その日の予測対象の総数（順位帯の判定に使う）
    total = conn.execute(
        """
        SELECT COUNT(*) FROM predictions
        WHERE horizon_days = ? AND predicted_on = ? AND model_tag = ?
        """,
        (horizon, predicted_on, model_tag),
    ).fetchone()[0]

    bands = compute_bands()

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
        band_key = _band_key(r[0] or 0, total)
        band = bands.get(band_key, bands.get("overall", {}))
        items.append(
            {
                "rank": r[0],
                # 順位帯と、その帯の過去実績（7日後に上昇していた割合・平均リターン）
                "band": band_key,
                "upRate": band.get("upRate"),
                "bandAvgReturn": band.get("avgReturn"),
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

    # AI が生成した当日の市場サマリー（無ければ省略）
    summary = None
    try:
        row = conn.execute(
            "SELECT summary_date, summary FROM ai_daily_summary "
            "ORDER BY summary_date DESC LIMIT 1"
        ).fetchone()
        if row:
            summary = {"date": row[0], "text": row[1]}
    except Exception:  # noqa: BLE001
        pass  # テーブル未作成（AI未利用）なら単に省略する

    return {
        "predictedOn": predicted_on,
        "horizonDays": horizon,
        "modelTag": model_tag,
        "summary": summary,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "universeSize": total,
        # 画面で「実績ではこうだった」と示すための基準値
        "baseline": bands.get("overall", {}),
        # 先週の予測が実際どうなったかの答え合わせ
        "review": gather_review(conn, horizon, len(items) or top_n, predicted_on),
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
