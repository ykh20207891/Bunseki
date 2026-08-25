"""過去に出した予測の答え合わせ。

predictions テーブルに記録した予測と、その後に実際に実現したリターンを突き合わせて、
「本当に当たっていたのか」を出す。バックテストと違い、これは完全に実運用の記録なので
生存者バイアスも後知恵もない。最終的にはこの数字だけを信用すべき。

使い方:
    python src/evaluate.py
    python src/evaluate.py --top-k 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import REPORT_DIR, ensure_dirs, setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402
from features import load_panel  # noqa: E402

LOG = setup_logger("evaluate")


def realized_returns(panel: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """(coin_id, date) 起点の horizon 日先実現リターンを返す。"""
    px = (panel.pivot(index="date", columns="coin_id", values="price").sort_index())
    full = pd.date_range(px.index.min(), px.index.max(), freq="D")
    px = px.reindex(full).ffill(limit=2)
    logpx = np.log(px.where(px > 0))
    fwd = logpx.shift(-horizon) - logpx
    out = (fwd.stack(future_stack=True).rename("realized_ret")
              .rename_axis(["date", "coin_id"]).reset_index())
    return out.dropna(subset=["realized_ret"])


def evaluate(top_k: int) -> str:
    init_db()
    with connect() as conn:
        preds = pd.read_sql_query("SELECT * FROM predictions", conn)
    if preds.empty:
        return "まだ予測が記録されていません。先に python src/predict.py を実行してください。"

    panel = load_panel()
    preds["date"] = pd.to_datetime(preds["predicted_on"])

    lines = []
    add = lines.append
    add("=" * 80)
    add(" 予測の答え合わせ（実運用記録ベース）")
    add("=" * 80)

    for (horizon, tag), grp in preds.groupby(["horizon_days", "model_tag"]):
        real = realized_returns(panel, int(horizon))
        m = grp.merge(real, on=["date", "coin_id"], how="inner")

        add("")
        add(" モデル: %s / 予測期間: %d 日" % (tag, horizon))
        add(" " + "-" * 76)
        pending = grp["predicted_on"].nunique() - m["predicted_on"].nunique()
        add("  予測を出した日数     : %d 日（うち結果判明 %d 日 / 判定待ち %d 日）"
            % (grp["predicted_on"].nunique(), m["predicted_on"].nunique(), pending))

        if m.empty:
            add("  → まだ %d 日経過していないため、判定できる予測がありません。" % horizon)
            continue

        rows = []
        for d, g in m.groupby("predicted_on"):
            if len(g) < 20:
                continue
            ic = g["score"].corr(g["realized_ret"], method="spearman")
            top = g.nsmallest(top_k, "rank")
            bottom = g.nlargest(top_k, "rank")
            rows.append({
                "date": d,
                "ic": ic,
                "top_ret": float(np.expm1(top["realized_ret"]).mean()),
                "bottom_ret": float(np.expm1(bottom["realized_ret"]).mean()),
                "univ_ret": float(np.expm1(g["realized_ret"]).mean()),
                "n": len(g),
            })
        if not rows:
            add("  → 判定できる予測がまだ十分にありません。")
            continue

        r = pd.DataFrame(rows).sort_values("date")
        add("  平均IC               : %+.4f  (勝率 %.0f%%)"
            % (r["ic"].mean(), (r["ic"] > 0).mean() * 100))
        add("  上位%d平均リターン    : %+.2f%%" % (top_k, r["top_ret"].mean() * 100))
        add("  下位%d平均リターン    : %+.2f%%" % (top_k, r["bottom_ret"].mean() * 100))
        add("  ユニバース平均       : %+.2f%%" % (r["univ_ret"].mean() * 100))
        add("  超過（上位-ユニバース）: %+.2f%%  (勝率 %.0f%%)"
            % ((r["top_ret"] - r["univ_ret"]).mean() * 100,
               (r["top_ret"] > r["univ_ret"]).mean() * 100))
        add("")
        add("  日別の内訳:")
        add("    %-12s %8s %10s %10s %10s" % ("基準日", "IC", "上位%d" % top_k, "ユニバース", "超過"))
        for x in r.itertuples():
            add("    %-12s %+8.3f %+9.2f%% %+9.2f%% %+9.2f%%"
                % (x.date, x.ic, x.top_ret * 100, x.univ_ret * 100,
                   (x.top_ret - x.univ_ret) * 100))

    add("")
    add("=" * 80)
    add(" ※ サンプル数が少ないうちは偶然に大きく振れます。数十回分たまるまでは")
    add("   『当たっている / 外れている』の判断は保留してください。")
    add("=" * 80)
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="予測の答え合わせ")
    p.add_argument("--top-k", type=int, default=10)
    args = p.parse_args()
    report = evaluate(args.top_k)
    print(report)
    ensure_dirs()
    (REPORT_DIR / "evaluation.txt").write_text(report, encoding="utf-8")
    LOG.info("レポートを保存: reports/evaluation.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
