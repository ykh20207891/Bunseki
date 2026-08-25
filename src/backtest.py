"""ウォークフォワード検証。

やっていること:
  - 学習期間 → エンバーゴ（horizon 日）→ 検証期間 を前へずらしながら繰り返す。
    目的変数が t→t+h の重なりを持つのでエンバーゴを入れないとリークする。
  - 評価は「日次スピアマンIC（予測順位と実際のリターンの順位相関）」を主指標にする。
  - 必ずベースライン（単純な7日モメンタム等）と比較する。
    機械学習が単純ルールに勝てていないなら、それは勝てていないと明記する。
  - ポートフォリオ検証は重複しない週次リバランス（7日ごと）で行い、
    BTC およびユニバース等加重と比較する。

使い方:
    python src/backtest.py
    python src/backtest.py --horizon 7 --top-k 10 --initial-train-days 120
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import REPORT_DIR, ensure_dirs, setup_logger  # noqa: E402
from db import connect  # noqa: E402
from features import build_dataset  # noqa: E402

LOG = setup_logger("backtest")

MODEL_PARAMS = dict(
    loss="squared_error",
    max_iter=300,
    learning_rate=0.05,
    max_depth=4,
    min_samples_leaf=60,
    l2_regularization=1.0,
    max_features=0.7,
    early_stopping=False,
    random_state=42,
)


def make_model() -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(**MODEL_PARAMS)


# ---------------------------------------------------------------- 分割
def walk_forward_splits(dates: np.ndarray, initial_train_days: int,
                        test_window: int, embargo: int):
    """(train_dates, test_dates) を順に生成する。学習は expanding window。"""
    i = initial_train_days
    while i + embargo < len(dates):
        train = dates[:i]
        start = i + embargo
        test = dates[start:start + test_window]
        if len(test) == 0:
            break
        yield train, test
        i += test_window


# ---------------------------------------------------------------- 評価指標
def daily_ic(df: pd.DataFrame, score_col: str) -> pd.Series:
    """日ごとのスピアマン順位相関。"""
    def _ic(g):
        if g[score_col].notna().sum() < 10 or g["fwd_ret"].notna().sum() < 10:
            return np.nan
        return g[score_col].corr(g["fwd_ret"], method="spearman")
    return df.groupby("date", observed=True).apply(_ic, include_groups=False)


def summarize_ic(ic: pd.Series, horizon: int) -> dict:
    ic = ic.dropna()
    if len(ic) < 3:
        return {"n_days": len(ic), "mean_ic": np.nan, "ic_std": np.nan,
                "ic_ir": np.nan, "t_stat": np.nan, "hit_rate": np.nan,
                "n_independent": 0}
    # 目的変数が horizon 日ぶん重なるので日次ICは自己相関する。
    # 標準誤差は「有効サンプル数 = 日数 / horizon」で計算する（重複日で割ると過大評価になる）。
    std = ic.std(ddof=1)
    n_eff = max(2, int(np.ceil(len(ic) / horizon)))
    t = (ic.mean() / (std / np.sqrt(n_eff))) if std > 0 else np.nan
    return {
        "n_days": int(len(ic)),
        "mean_ic": float(ic.mean()),
        "ic_std": float(std),
        "ic_ir": float(ic.mean() / std) if std > 0 else np.nan,
        "t_stat": float(t) if t == t else np.nan,
        "hit_rate": float((ic > 0).mean()),
        "n_independent": n_eff,
    }


def portfolio_sim(df: pd.DataFrame, score_col: str, top_k: int, horizon: int) -> dict:
    """重複しない horizon 日ごとのリバランスで、上位K銘柄等加重の累積リターンを出す。"""
    dates = np.sort(df["date"].unique())[::horizon]
    rows = []
    for d in dates:
        g = df[(df["date"] == d) & df[score_col].notna() & df["fwd_ret"].notna()]
        if len(g) < top_k * 2:
            continue
        top = g.nlargest(top_k, score_col)
        bottom = g.nsmallest(top_k, score_col)
        rows.append({
            "date": d,
            "top": float(np.expm1(top["fwd_ret"]).mean()),
            "bottom": float(np.expm1(bottom["fwd_ret"]).mean()),
            "universe": float(np.expm1(g["fwd_ret"]).mean()),
            "n": len(g),
        })
    if not rows:
        return {}
    p = pd.DataFrame(rows)
    return {
        "n_periods": len(p),
        "top_mean": float(p["top"].mean()),
        "universe_mean": float(p["universe"].mean()),
        "spread_mean": float((p["top"] - p["bottom"]).mean()),
        "excess_vs_universe": float((p["top"] - p["universe"]).mean()),
        "top_cum": float((1 + p["top"]).prod() - 1),
        "universe_cum": float((1 + p["universe"]).prod() - 1),
        "top_win_rate": float((p["top"] > p["universe"]).mean()),
        "periods": p,
    }


# ---------------------------------------------------------------- 生存者バイアス診断
def survivorship_diagnostic(ds: pd.DataFrame, cs_cols: list[str], horizon: int,
                            top_k: int, initial_train_days: int,
                            test_window: int) -> list[dict]:
    """「昔から大きかった銘柄」だけに絞ったときに成績がどれだけ落ちるかを測る。

    バックフィルの対象は実行時点の上位500銘柄なので、当時小さくてその後に急騰し
    上位に入ってきた銘柄が過剰に含まれている。データ開始時点で既に大きかった銘柄に
    限定すると、この「後から入ってきたコホート」が消える。ここで超過リターンが
    大きく落ちるなら、元の成績は実力ではなく生存者バイアスだったということ。
    """
    with connect() as conn:
        d0 = conn.execute("SELECT MIN(date) FROM price_history").fetchone()[0]
        if not d0:
            return []
        first = pd.read_sql_query(
            "SELECT coin_id, market_cap AS mcap0 FROM price_history WHERE date = ?",
            conn, params=(d0,))
    if first.empty:
        return []

    ds = ds.merge(first, on="coin_id", how="left")
    scenarios = [
        ("全銘柄（元の検証）", None),
        ("%s 時点で 5,000万ドル以上" % d0, 50e6),
        ("%s 時点で 2億ドル以上" % d0, 200e6),
    ]

    out = []
    for label, floor in scenarios:
        sub = ds if floor is None else ds[ds["mcap0"] >= floor]
        dates = np.sort(sub["date"].unique())
        oos = _run_folds(sub, cs_cols, dates, initial_train_days, test_window,
                         horizon, log_folds=False)
        if oos is None:
            continue
        summ = summarize_ic(daily_ic(oos, "score"), horizon)
        p = portfolio_sim(oos, "score", top_k, horizon)
        out.append({
            "label": label,
            "n_coins": int(sub["coin_id"].nunique()),
            "mean_ic": summ["mean_ic"],
            "t_stat": summ["t_stat"],
            "excess": (p or {}).get("excess_vs_universe", float("nan")),
        })
    return out


# ---------------------------------------------------------------- 本体
def _run_folds(ds: pd.DataFrame, cs_cols: list[str], dates: np.ndarray,
               initial_train_days: int, test_window: int, horizon: int,
               log_folds: bool = True, keep_features: bool = False):
    """ウォークフォワードで各 fold を学習・予測し、アウトオブサンプル予測を返す。"""
    preds = []
    n = 0
    for train_dates, test_dates in walk_forward_splits(
            dates, initial_train_days, test_window, horizon):
        tr = ds[ds["date"].isin(train_dates)]
        te = ds[ds["date"].isin(test_dates)]
        if len(tr) < 500 or len(te) == 0:
            continue
        model = make_model()
        model.fit(tr[cs_cols].to_numpy(dtype=float), tr["y_rank"].to_numpy(dtype=float))
        cols = ["date", "coin_id", "symbol", "fwd_ret"] + (cs_cols if keep_features else [])
        out = te[cols].copy()
        out["score"] = model.predict(te[cs_cols].to_numpy(dtype=float))
        preds.append(out)
        n += 1
        if log_folds:
            LOG.info("fold %2d: 学習 %s〜%s (%s行) → 検証 %s〜%s (%s行)",
                     n, pd.Timestamp(train_dates[0]).date(),
                     pd.Timestamp(train_dates[-1]).date(), format(len(tr), ","),
                     pd.Timestamp(test_dates[0]).date(),
                     pd.Timestamp(test_dates[-1]).date(), format(len(te), ","))
    if not preds:
        return None
    result = pd.concat(preds, ignore_index=True)
    result.attrs["n_folds"] = n
    return result


def run_backtest(horizon: int, top_k: int, initial_train_days: int,
                 test_window: int) -> dict:
    ds, cs_cols = build_dataset(horizon=horizon)
    ds = ds.dropna(subset=["y_rank"]).copy()

    dates = np.sort(ds["date"].unique())
    LOG.info("使用可能な日付: %d 日 (%s 〜 %s)", len(dates),
             pd.Timestamp(dates[0]).date(), pd.Timestamp(dates[-1]).date())
    LOG.info("特徴量: %d 個 / 総行数: %s", len(cs_cols), format(len(ds), ","))

    if len(dates) < initial_train_days + horizon + test_window:
        raise RuntimeError(
            "データが足りません（%d 日）。initial_train_days=%d を減らすか、"
            "backfill.py で過去データを取得してください。" % (len(dates), initial_train_days))

    oos = _run_folds(ds, cs_cols, dates, initial_train_days, test_window,
                     horizon, log_folds=True, keep_features=True)
    if oos is None:
        raise RuntimeError("有効な fold がありませんでした。")
    n_folds = oos.attrs["n_folds"]

    # ベースライン（クロスセクション順位そのものをスコアとして使う）
    candidates = {"model(GBDT)": "score"}
    for base, label in (("cs_ret_7", "baseline: 7日モメンタム"),
                        ("cs_ret_30", "baseline: 30日モメンタム"),
                        ("cs_reversal_1", "baseline: 1日リバーサル"),
                        ("cs_turnover_surge", "baseline: 出来高サージ"),
                        ("cs_log_mcap", "baseline: 時価総額（大型ほど上位）")):
        if base in oos.columns:
            candidates[label] = base

    results = {}
    for label, col in candidates.items():
        ic = daily_ic(oos, col)
        summ = summarize_ic(ic, horizon)
        summ["portfolio"] = portfolio_sim(oos, col, top_k, horizon)
        results[label] = summ

    LOG.info("生存者バイアスの診断を実行中...")
    surv = survivorship_diagnostic(ds, cs_cols, horizon, top_k,
                                   initial_train_days, test_window)

    return {"results": results, "oos": oos, "n_folds": n_folds,
            "horizon": horizon, "top_k": top_k, "n_features": len(cs_cols),
            "survivorship": surv}


# ---------------------------------------------------------------- 出力
def format_report(bt: dict) -> str:
    lines = []
    add = lines.append
    add("=" * 78)
    add(" ウォークフォワード検証レポート")
    add("=" * 78)
    add(" 予測期間 : %d 日先リターンのクロスセクション順位" % bt["horizon"])
    add(" fold 数   : %d / 特徴量 %d 個 / 上位K = %d" % (bt["n_folds"], bt["n_features"], bt["top_k"]))
    oos = bt["oos"]
    add(" 検証期間 : %s 〜 %s"
        % (pd.Timestamp(oos["date"].min()).date(), pd.Timestamp(oos["date"].max()).date()))
    add("")
    add("-" * 78)
    add(" [1] 予測力（日次スピアマンIC = 予測順位と実リターン順位の相関）")
    add("-" * 78)
    add("   %-34s %8s %8s %8s %8s" % ("手法", "平均IC", "ICIR", "t値", "勝率"))
    for label, r in bt["results"].items():
        add("   %-34s %8.4f %8.3f %8.2f %7.1f%%"
            % (label, r["mean_ic"], r["ic_ir"], r["t_stat"], r["hit_rate"] * 100))
    add("")
    add("   ※ t値は重複を補正した有効サンプル数 n=%s で計算。|t| > 2 が一応の目安。"
        % bt["results"]["model(GBDT)"].get("n_independent", "?"))
    add("   ※ ICIR = 平均IC / ICの標準偏差（日次ベース）。安定性の指標。")
    add("")
    add("-" * 78)
    add(" [2] 上位%d銘柄・等加重ポートフォリオ（%d日ごとリバランス・重複なし）" % (bt["top_k"], bt["horizon"]))
    add("-" * 78)
    add("   %-34s %10s %10s %10s %8s" % ("手法", "平均/回", "対ユニバース", "累積", "勝率"))
    for label, r in bt["results"].items():
        p = r.get("portfolio") or {}
        if not p:
            continue
        add("   %-34s %9.2f%% %9.2f%% %9.1f%% %7.1f%%"
            % (label, p["top_mean"] * 100, p["excess_vs_universe"] * 100,
               p["top_cum"] * 100, p["top_win_rate"] * 100))
    base_p = bt["results"]["model(GBDT)"].get("portfolio") or {}
    if base_p:
        add("   %-34s %9.2f%% %9s %9.1f%% %8s"
            % ("(参考) ユニバース等加重", base_p["universe_mean"] * 100, "-",
               base_p["universe_cum"] * 100, "-"))
        add("")
        add("   期間数: %d 回" % base_p["n_periods"])
    surv = bt.get("survivorship") or []
    if surv:
        add("")
        add("-" * 78)
        add(" [3] 生存者バイアスの診断")
        add("-" * 78)
        add("   バックフィル対象は「今日の上位500銘柄」なので、当時小さくてその後に")
        add("   急騰し上位に入ってきた銘柄が過剰に含まれています。データ開始時点で")
        add("   すでに大きかった銘柄に絞ると、そのコホートが消えます。")
        add("")
        add("   %-32s %6s %9s %8s %12s" % ("対象", "銘柄数", "平均IC", "t値", "対ユニバース"))
        for r in surv:
            add("   %-32s %6d %+9.4f %+8.2f %+11.2f%%"
                % (r["label"], r["n_coins"], r["mean_ic"], r["t_stat"], r["excess"] * 100))
        if len(surv) >= 2:
            drop = surv[0]["excess"] - surv[-1]["excess"]
            add("")
            if abs(surv[0]["excess"]) > 1e-9 and drop > 0.5 * abs(surv[0]["excess"]):
                add("   → 超過リターンが %.2f%% ぶん落ちました。元の成績の大部分は"
                    % (drop * 100))
                add("     実力ではなく生存者バイアスです。上の [2] の数字は割り引いて見てください。")
            else:
                add("   → 超過リターンの低下は限定的でした。バイアスの影響は比較的小さいようです。")

    add("")
    add("-" * 78)
    add(" [4] 解釈上の注意")
    add("-" * 78)
    add("   ・手数料・スリッページ・板の薄さは一切考慮していません。")
    add("     小型銘柄は往復数%かかることもあり、それだけで超過リターンは消え得ます。")
    add("   ・平均ICが 0.02〜0.05 程度でも、統計的に安定していれば有意な部類です。")
    add("     逆にベースラインに勝てていなければ、モデルは何も足していません。")
    add("   ・最終的に信用してよいのは evaluate.py の実運用記録だけです。")
    add("=" * 78)
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="ウォークフォワード検証")
    p.add_argument("--horizon", type=int, default=7)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--initial-train-days", type=int, default=120)
    p.add_argument("--test-window", type=int, default=30)
    args = p.parse_args()

    bt = run_backtest(args.horizon, args.top_k, args.initial_train_days, args.test_window)
    report = format_report(bt)
    print("")
    print(report)

    ensure_dirs()
    out = REPORT_DIR / ("backtest_h%d.txt" % args.horizon)
    out.write_text(report, encoding="utf-8")
    bt["oos"][["date", "coin_id", "symbol", "score", "fwd_ret"]].to_csv(
        REPORT_DIR / ("backtest_oos_h%d.csv" % args.horizon), index=False, encoding="utf-8-sig")
    LOG.info("レポートを保存: %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
