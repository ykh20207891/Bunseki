"""最新日のデータでモデルを学習し、次の horizon 日のランキング予測を出す。

出力は「スコアの高い順に並べたランキング」であって、売買推奨ではない。
予測は predictions テーブルに必ず記録し、後日 evaluate.py で答え合わせできるようにする。

使い方:
    python src/predict.py
    python src/predict.py --horizon 7 --top-n 20
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import make_model  # noqa: E402
from common import REPORT_DIR, ensure_dirs, setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402
from features import build_dataset  # noqa: E402

LOG = setup_logger("predict")

# 予測を出すのに最低限必要な規模（これ未満だと結果が無意味なので止める）
MIN_TRAIN_ROWS = 1000
MIN_TARGET_COINS = 30


def feature_importance(model, X: np.ndarray, y: np.ndarray, cols: list[str],
                       n_repeats: int = 5, top: int = 12) -> pd.DataFrame:
    """パーミュテーション重要度（重い場合はサンプリングして計算）。"""
    from sklearn.inspection import permutation_importance
    idx = np.random.default_rng(0).choice(len(X), size=min(4000, len(X)), replace=False)
    r = permutation_importance(model, X[idx], y[idx], n_repeats=n_repeats,
                               random_state=0, scoring="r2")
    return (pd.DataFrame({"feature": cols, "importance": r.importances_mean})
              .sort_values("importance", ascending=False).head(top).reset_index(drop=True))


def run_predict(horizon: int, top_n: int, model_tag: str | None,
                skip_importance: bool) -> int:
    init_db()
    ds, cs_cols = build_dataset(horizon=horizon)

    all_dates = np.sort(ds["date"].unique())
    as_of = pd.Timestamp(all_dates[-1])

    train = ds.dropna(subset=["y_rank"])
    target = ds[ds["date"] == as_of].copy()

    LOG.info("基準日   : %s", as_of.date())
    LOG.info("学習データ: %s 行 (%s 〜 %s)", format(len(train), ","),
             pd.Timestamp(train["date"].min()).date(), pd.Timestamp(train["date"].max()).date())
    LOG.info("予測対象 : %d 銘柄", len(target))

    if len(train) < MIN_TRAIN_ROWS or len(target) < MIN_TARGET_COINS:
        raise RuntimeError("データが不足しています（学習 %d 行 / 対象 %d 銘柄）。"
                           % (len(train), len(target)))

    X = train[cs_cols].to_numpy(dtype=float)
    y = train["y_rank"].to_numpy(dtype=float)
    model = make_model()
    model.fit(X, y)

    target["score"] = model.predict(target[cs_cols].to_numpy(dtype=float))
    target = target.sort_values("score", ascending=False).reset_index(drop=True)
    target["rank"] = np.arange(1, len(target) + 1)

    tag = model_tag or ("gbdt_h%d_v1" % horizon)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    as_of_str = as_of.strftime("%Y-%m-%d")

    with connect() as conn:
        # ユニバース条件を変えて再実行したとき、対象外になった銘柄の古い行が
        # 残らないように、同じ (基準日, 期間, モデル) の予測は一度消してから入れ直す。
        conn.execute(
            "DELETE FROM predictions WHERE predicted_on=? AND horizon_days=? AND model_tag=?",
            (as_of_str, horizon, tag))
        conn.executemany(
            "INSERT OR REPLACE INTO predictions"
            " (predicted_on, horizon_days, model_tag, coin_id, symbol, score, rank,"
            "  price_at_pred, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(as_of_str, horizon, tag, r.coin_id, r.symbol, float(r.score), int(r.rank),
              float(r.price) if pd.notna(r.price) else None, created)
             for r in target.itertuples()])
        conn.commit()
    LOG.info("predictions テーブルに %d 件記録しました (model_tag=%s)", len(target), tag)

    imp = None
    if not skip_importance:
        try:
            imp = feature_importance(model, X, y, cs_cols)
        except Exception as e:
            LOG.warning("重要度の計算に失敗: %s", e)

    report = format_report(target, as_of, horizon, top_n, len(train), tag, imp)
    print("")
    print(report)

    ensure_dirs()
    (REPORT_DIR / ("prediction_%s_h%d.txt" % (as_of_str, horizon))).write_text(
        report, encoding="utf-8")
    cols = ["rank", "symbol", "name", "coin_id", "score", "price", "market_cap",
            "ret_7", "ret_30", "vol_30", "turnover"]
    target[cols].to_csv(
        REPORT_DIR / ("prediction_%s_h%d.csv" % (as_of_str, horizon)),
        index=False, encoding="utf-8-sig")
    LOG.info("レポートを保存しました: reports/prediction_%s_h%d.txt", as_of_str, horizon)
    return 0


def _txt(v, width: int) -> str:
    """欠損（NaN）を含みうる文字列カラムを安全に切り詰める。

    上位500から外れた銘柄はシンボルが欠損しうるので、NaN を素で slice すると
    'float' object is not subscriptable で落ちる。
    """
    if v is None or (isinstance(v, float) and v != v):
        return ""
    return str(v)[:width]


def format_report(df: pd.DataFrame, as_of, horizon: int, top_n: int,
                  n_train: int, tag: str, imp) -> str:
    lines = []
    add = lines.append
    add("=" * 88)
    add(" %d日先リターン ランキング予測   基準日: %s (UTC)" % (horizon, as_of.date()))
    add("=" * 88)
    add(" モデル: %s / 学習 %s 行 / 対象 %d 銘柄" % (tag, format(n_train, ","), len(df)))
    add("")
    add(" ── 上位 %d 銘柄 ──" % top_n)
    add(" %4s %-10s %-22s %9s %12s %9s %9s %8s"
        % ("順位", "シンボル", "名称", "スコア", "価格($)", "7日(%)", "30日(%)", "時価総額"))
    add(" " + "-" * 86)
    for r in df.head(top_n).itertuples():
        mcap = r.market_cap if pd.notna(r.market_cap) else 0.0
        mcap_s = ("%.1fB" % (mcap / 1e9)) if mcap >= 1e9 else ("%.0fM" % (mcap / 1e6))
        add(" %4d %-10s %-22s %9.4f %12.6g %+8.1f %+8.1f %8s"
            % (r.rank, _txt(r.symbol, 10), _txt(r.name, 22), r.score,
               r.price, (np.expm1(r.ret_7) * 100) if pd.notna(r.ret_7) else 0,
               (np.expm1(r.ret_30) * 100) if pd.notna(r.ret_30) else 0, mcap_s))
    add("")
    add(" ── 下位 5 銘柄（参考: モデルが弱いと見ている） ──")
    for r in df.tail(5).itertuples():
        add(" %4d %-10s %-22s %9.4f" % (r.rank, _txt(r.symbol, 10), _txt(r.name, 22), r.score))

    if imp is not None and len(imp):
        add("")
        add(" ── モデルが重視した特徴量 ──")
        for r in imp.itertuples():
            add("   %-26s %8.5f" % (r.feature.replace("cs_", ""), r.importance))

    add("")
    add("-" * 88)
    add(" 注意")
    add("-" * 88)
    add(" ・これは統計モデルの出力（相対順位スコア）であり、売買の推奨ではありません。")
    add(" ・スコアは『このユニバースの中での相対的な期待順位』を表すもので、")
    add("   上昇そのものを保証しません。市場全体が下げれば上位銘柄も下がります。")
    add(" ・実際の成績は evaluate.py で後日必ず答え合わせしてください。")
    add("=" * 88)
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="ランキング予測の実行")
    p.add_argument("--horizon", type=int, default=7)
    p.add_argument("--top-n", type=int, default=20)
    p.add_argument("--model-tag", type=str, default=None)
    p.add_argument("--skip-importance", action="store_true")
    args = p.parse_args()
    return run_predict(args.horizon, args.top_n, args.model_tag, args.skip_importance)


if __name__ == "__main__":
    raise SystemExit(main())
