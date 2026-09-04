"""週次でウォークフォワード検証を実行し、成績の推移を記録する。

モデルは predict.py が毎日その場で再学習しているため、データが増えれば
学習内容は自動で新しくなる。ただし「それで良くなったのか」は測らないと
分からないので、週1回まとめて検証し、結果を DB に積む。

    python src/track_performance.py            # 月曜のみ実行（日次バッチ用）
    python src/track_performance.py --force    # 曜日に関係なく実行

記録した推移は prediction.json の performance として配信し、アプリで見られる。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402

LOG = setup_logger("track_performance")

HORIZON = 7
TOP_K = 10
INITIAL_TRAIN_DAYS = 180
TEST_WINDOW = 30

# 比較対象にするベースライン（この名前で backtest が結果を返す）
MODEL_LABEL = "model(GBDT)"


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS performance_log (
            measured_on   TEXT PRIMARY KEY,   -- 検証を回した日 (UTC)
            horizon_days  INTEGER NOT NULL,
            n_folds       INTEGER,
            n_features    INTEGER,
            train_rows    INTEGER,
            mean_ic       REAL,               -- モデルの平均IC
            t_stat        REAL,
            hit_rate      REAL,
            top_k_return  REAL,               -- 上位K銘柄の平均リターン(1回あたり)
            vs_universe   REAL,               -- ユニバース等加重との差
            best_baseline TEXT,               -- 最も強かったベースライン名
            best_baseline_return REAL,
            beats_baseline INTEGER,           -- モデルがベースラインに勝ったか
            detail_json   TEXT,
            created_at    TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _clean_detail(results: dict) -> dict:
    """DataFrame を含む項目を落として JSON にできる形にする。"""
    out = {}
    for label, summ in results.items():
        item = {k: v for k, v in summ.items() if k != "portfolio"}
        portfolio = summ.get("portfolio") or {}
        item["portfolio"] = {
            k: v for k, v in portfolio.items() if k != "periods"
        }
        out[label] = item
    return out


def _is_monday() -> bool:
    return datetime.now(timezone.utc).weekday() == 0


def run(force: bool = False) -> dict | None:
    if not force and not _is_monday():
        LOG.info("月曜以外のため検証をスキップします")
        return None

    init_db()
    measured_on = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with connect() as conn:
        _ensure_table(conn)
        done = conn.execute(
            "SELECT 1 FROM performance_log WHERE measured_on = ?", (measured_on,)
        ).fetchone()
        if done and not force:
            LOG.info("%s は既に検証済みです", measured_on)
            return None

    LOG.info("ウォークフォワード検証を開始します（時間がかかります）")

    import backtest

    bt = backtest.run_backtest(
        horizon=HORIZON,
        top_k=TOP_K,
        initial_train_days=INITIAL_TRAIN_DAYS,
        test_window=TEST_WINDOW,
    )

    results = bt.get("results") or {}
    model = results.get(MODEL_LABEL)
    if not model:
        LOG.warning("モデルの検証結果が取得できませんでした")
        return None

    portfolio = model.get("portfolio") or {}
    model_return = portfolio.get("top_mean")
    excess = portfolio.get("excess_vs_universe")

    # モデル以外で最も成績が良かったベースラインを拾う
    best_label, best_return = None, None
    for label, summ in results.items():
        if label == MODEL_LABEL:
            continue
        ret = (summ.get("portfolio") or {}).get("top_mean")
        if ret is None:
            continue
        if best_return is None or ret > best_return:
            best_label, best_return = label, ret

    row = {
        "measured_on": measured_on,
        "horizon_days": HORIZON,
        "n_folds": bt.get("n_folds"),
        "n_features": bt.get("n_features"),
        "train_rows": int(len(bt.get("oos", []))) if bt.get("oos") is not None else None,
        "mean_ic": model.get("mean_ic"),
        "t_stat": model.get("t_stat"),
        "hit_rate": model.get("hit_rate"),
        "top_k_return": model_return,
        "vs_universe": excess,
        "best_baseline": best_label,
        "best_baseline_return": best_return,
        "beats_baseline": (
            1
            if (model_return is not None and best_return is not None and model_return > best_return)
            else 0
        ),
    }

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO performance_log
              (measured_on, horizon_days, n_folds, n_features, train_rows,
               mean_ic, t_stat, hit_rate, top_k_return, vs_universe,
               best_baseline, best_baseline_return, beats_baseline,
               detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(measured_on) DO UPDATE SET
              mean_ic=excluded.mean_ic, t_stat=excluded.t_stat,
              hit_rate=excluded.hit_rate, top_k_return=excluded.top_k_return,
              vs_universe=excluded.vs_universe,
              best_baseline=excluded.best_baseline,
              best_baseline_return=excluded.best_baseline_return,
              beats_baseline=excluded.beats_baseline,
              detail_json=excluded.detail_json, created_at=datetime('now')
            """,
            (
                row["measured_on"], row["horizon_days"], row["n_folds"],
                row["n_features"], row["train_rows"], row["mean_ic"],
                row["t_stat"], row["hit_rate"], row["top_k_return"],
                row["vs_universe"], row["best_baseline"],
                row["best_baseline_return"], row["beats_baseline"],
                json.dumps(_clean_detail(results), ensure_ascii=False, default=str),
            ),
        )
        conn.commit()

    LOG.info(
        "検証結果を記録: IC=%.4f / 上位%d平均=%.2f%% / ベースライン最良=%s",
        row["mean_ic"] or 0, TOP_K,
        (row["top_k_return"] or 0) * 100, row["best_baseline"],
    )
    return row


def history(limit: int = 12) -> list[dict]:
    """記録済みの成績推移（新しい順）。"""
    with connect() as conn:
        try:
            rows = conn.execute(
                "SELECT measured_on, mean_ic, t_stat, hit_rate, top_k_return, "
                "vs_universe, best_baseline, best_baseline_return, beats_baseline "
                "FROM performance_log ORDER BY measured_on DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except Exception:  # noqa: BLE001
            return []

    return [
        {
            "measuredOn": r[0],
            "meanIc": r[1],
            "tStat": r[2],
            "hitRate": r[3],
            "topKReturn": r[4],
            "vsUniverse": r[5],
            "bestBaseline": r[6],
            "bestBaselineReturn": r[7],
            "beatsBaseline": bool(r[8]),
        }
        for r in rows
    ]


def main() -> int:
    p = argparse.ArgumentParser(description="週次でモデルの成績を検証・記録する")
    p.add_argument("--force", action="store_true", help="曜日に関係なく実行")
    args = p.parse_args()
    run(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
