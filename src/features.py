"""日次パネルの構築・ユニバース選定・特徴量生成。

設計方針:
  - 予測は「絶対リターンを当てる」のではなく「その日のユニバースの中で
    相対的にどれが強いか」を当てるクロスセクション・ランキング問題として扱う。
    暗号資産は市場全体が一緒に動くので、絶対値予測は地合いに支配されて意味が薄い。
  - よって特徴量は各日ごとに「クロスセクション順位（0〜1のパーセンタイル）」へ
    変換してからモデルに入れる。レジームが変わってもスケールが安定する。
  - 目的変数も同様に、7日先リターンのクロスセクション順位を使う。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import connect  # noqa: E402

# ---------------------------------------------------------------- ユニバース設定
DEFAULT_UNIVERSE = {
    "min_market_cap": 30_000_000.0,     # 時価総額 3000万ドル以上
    "min_dollar_volume": 1_000_000.0,   # 直近7日平均の出来高 100万ドル以上
    "min_turnover": 0.002,              # 出来高/時価総額 が 0.2% 以上（実質取引されている）
    "max_stable_vol": 0.01,             # 60日日次ボラ 1% 未満はステーブル扱いで除外
    # 60日ボラだけだと、一度きりの価格改定でボラが跳ねたステーブルコインが通過してしまう
    # （実例: JupUSD が 0.125→0.999 のジャンプで 60日ボラ 38% になっていた）。
    # 直近14日のボラでも判定して弾く。
    "max_stable_vol_recent": 0.005,
    "min_history_days": 90,             # 特徴量計算に必要な最低履歴
    "exclude_wrapped": True,
}

# 原資産の重複（ラップ/ステーキング派生）やトークナイズ株式を除くためのキーワード
_EXCLUDE_NAME_KEYWORDS = (
    "wrapped", "staked", "liquid staked", "restaked", "bridged",
    "tokenized", "xstock", " etf", "etf ", "s&p", "nasdaq",
    "binance-peg", "peg-", "compound ", "aave ", "coinbase wrapped",
)
_EXCLUDE_SYMBOLS = {
    "WBTC", "WETH", "WBETH", "STETH", "WSTETH", "RETH", "CBBTC", "CBETH",
    "WEETH", "EZETH", "RSETH", "METH", "SOLVBTC", "LBTC", "BTCB", "TBTC",
    "WBNB", "WSOL", "WHYPE", "WAVAX", "WMATIC", "WTRX", "STSOL", "MSOL",
    "JITOSOL", "BNSOL", "SUSDE", "SDAI", "WSTUSR",
    # 金連動トークン。トークナイズ株式と同じ理由で除外する（暗号資産の値動きではなく
    # 金価格の写しであり、暗号資産の銘柄選択という問題設定に混ぜるとノイズになる）。
    "XAUT", "PAXG", "XAUM", "KAU", "TXAU",
}


# ---------------------------------------------------------------- パネル構築
def load_panel(conn=None) -> pd.DataFrame:
    """price_history と snapshots を統合した日次パネルを返す。

    列: coin_id, date, price, market_cap, volume, symbol, name
    同じ (coin_id, date) が両方にある場合は日次収集した snapshots を優先する。
    """
    own = conn is None
    conn = conn or connect()
    try:
        hist = pd.read_sql_query(
            "SELECT coin_id, date, price, market_cap, total_volume AS volume,"
            " 0 AS prio FROM price_history", conn)
        snap = pd.read_sql_query(
            "SELECT coin_id, snapshot_date AS date, price, market_cap,"
            " total_volume AS volume, 1 AS prio FROM snapshots", conn)
        # シンボル・名称は「その銘柄について最後に判明した値」を使う。
        # 最新スナップショットだけを見ると、その日に上位500から外れた銘柄の
        # シンボルが NaN になってしまう（日が進むほど増える）。
        # SQLite は MAX() と同じ行の裸の列を返すので、これで最新の値が取れる。
        meta = pd.read_sql_query(
            "SELECT coin_id, symbol, name, MAX(snapshot_date) AS last_seen"
            " FROM snapshots WHERE symbol IS NOT NULL GROUP BY coin_id", conn)
        meta = meta.drop(columns="last_seen")
    finally:
        if own:
            conn.close()

    panel = pd.concat([hist, snap], ignore_index=True)
    if panel.empty:
        raise RuntimeError("データがありません。collect.py / backfill.py を先に実行してください。")

    panel = (panel.sort_values(["coin_id", "date", "prio"])
                  .drop_duplicates(["coin_id", "date"], keep="last")
                  .drop(columns="prio"))
    panel = panel.merge(meta, on="coin_id", how="left")
    panel["date"] = pd.to_datetime(panel["date"])
    return panel.reset_index(drop=True)


def load_news(conn=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(RSS由来の日次言及集計, GDELT由来の日次集計) を返す。無ければ空。"""
    own = conn is None
    conn = conn or connect()
    try:
        rss = pd.read_sql_query(
            "SELECT nm.coin_id, nm.pub_date AS date, COUNT(*) AS news_count,"
            " AVG(na.sentiment) AS news_sent"
            " FROM news_mentions nm JOIN news_articles na ON na.id = nm.article_id"
            " GROUP BY nm.coin_id, nm.pub_date", conn)
        gdelt = pd.read_sql_query(
            "SELECT coin_id, date, article_count AS gdelt_vol, tone AS gdelt_tone"
            " FROM news_daily WHERE source = 'gdelt'", conn)
    finally:
        if own:
            conn.close()
    for df in (rss, gdelt):
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
    return rss, gdelt


def _news_wide(long_df: pd.DataFrame, value: str, index, columns,
               fill_zero: bool) -> pd.DataFrame | None:
    """ニュースのロング表を (日付 × 銘柄) に整形する。

    収集を始める前の期間は「ニュースが無かった」ではなく「観測していない」なので
    0 埋めせず NaN のままにする。0 埋めすると過去に嘘の情報を与えることになる。
    """
    if long_df.empty or value not in long_df.columns:
        return None
    w = long_df.pivot(index="date", columns="coin_id", values=value)
    w = w.reindex(index=index, columns=columns)
    if fill_zero:
        start = long_df["date"].min()
        mask = w.index >= start
        w.loc[mask] = w.loc[mask].fillna(0.0)
    return w


def _wide(panel: pd.DataFrame, col: str) -> pd.DataFrame:
    """(日付 × 銘柄) のワイド表にして、欠測日を最大2日だけ前方補完する。"""
    w = panel.pivot(index="date", columns="coin_id", values=col).sort_index()
    full = pd.date_range(w.index.min(), w.index.max(), freq="D")
    return w.reindex(full).ffill(limit=2)


# ---------------------------------------------------------------- 特徴量
def _safe_log(x: pd.DataFrame) -> pd.DataFrame:
    return np.log(x.where(x > 0))


def _news_features(px: pd.DataFrame, min_news_days: int) -> dict[str, pd.DataFrame]:
    """ニュース由来の特徴量。蓄積日数が足りないうちは空を返して自動的に無効化する。"""
    rss, gdelt = load_news()
    out: dict[str, pd.DataFrame] = {}

    if not rss.empty and rss["date"].nunique() >= min_news_days:
        cnt = _news_wide(rss, "news_count", px.index, px.columns, fill_zero=True)
        sent = _news_wide(rss, "news_sent", px.index, px.columns, fill_zero=False)
        if cnt is not None:
            out["news_count_1d"] = cnt
            out["news_count_7d"] = cnt.rolling(7, min_periods=1).sum()
            out["news_count_30d"] = cnt.rolling(30, min_periods=5).sum()
            # 注目度の急上昇（過去30日平均と比べて直近7日どれだけ話題か）
            out["news_surge"] = (out["news_count_7d"] /
                                 (out["news_count_30d"] / 30 * 7).replace(0, np.nan))
            # その日の全ニュースに占める言及シェア（相対的な注目度）
            out["news_share_7d"] = out["news_count_7d"].div(
                out["news_count_7d"].sum(axis=1).replace(0, np.nan), axis=0)
        if sent is not None:
            out["news_sent_1d"] = sent
            out["news_sent_7d"] = sent.rolling(7, min_periods=1).mean()
            out["news_sent_30d"] = sent.rolling(30, min_periods=5).mean()
            out["news_sent_chg"] = out["news_sent_7d"] - out["news_sent_30d"]
    else:
        LOG_MSG.append("RSSニュースの蓄積日数が %d 日未満のため、ニュース特徴量は無効です。"
                       % min_news_days)

    if not gdelt.empty and gdelt["date"].nunique() >= min_news_days:
        vol = _news_wide(gdelt, "gdelt_vol", px.index, px.columns, fill_zero=True)
        tone = _news_wide(gdelt, "gdelt_tone", px.index, px.columns, fill_zero=False)
        if vol is not None:
            out["gdelt_vol_7d"] = vol.rolling(7, min_periods=2).mean()
            out["gdelt_vol_surge"] = (out["gdelt_vol_7d"] /
                                      vol.rolling(30, min_periods=10).mean().replace(0, np.nan))
        if tone is not None:
            out["gdelt_tone_7d"] = tone.rolling(7, min_periods=2).mean()
            out["gdelt_tone_chg"] = (out["gdelt_tone_7d"] -
                                     tone.rolling(30, min_periods=10).mean())
    return out


LOG_MSG: list[str] = []


def build_features(panel: pd.DataFrame, horizon: int = 7,
                   include_news: bool = True, min_news_days: int = 60) -> pd.DataFrame:
    """ワイド表ベースで特徴量と目的変数を作り、ロング形式で返す。

    include_news=True でも、ニュースの蓄積日数が min_news_days に満たなければ
    ニュース特徴量は自動的に除外される（過去に情報が無い列を入れても害しかないため）。
    """
    px = _wide(panel, "price")
    mc = _wide(panel, "market_cap")
    vo = _wide(panel, "volume")

    logpx = _safe_log(px)
    ret1 = logpx.diff()

    feats: dict[str, pd.DataFrame] = {}

    # --- モメンタム（対数リターン） ---
    for n in (1, 3, 7, 14, 30, 60, 90):
        feats["ret_%d" % n] = logpx.diff(n)

    # --- ボラティリティ / リスク調整モメンタム ---
    for n in (7, 14, 30, 60):
        feats["vol_%d" % n] = ret1.rolling(n, min_periods=max(3, n // 2)).std()
    feats["sharpe_30"] = feats["ret_30"] / (feats["vol_30"] * np.sqrt(30))
    feats["accel_7_30"] = feats["ret_7"] - feats["ret_30"] * (7.0 / 30.0)
    feats["reversal_1"] = -feats["ret_1"]

    # --- 規模 ---
    feats["log_mcap"] = _safe_log(mc)
    feats["log_volume"] = _safe_log(vo)

    # --- 流動性・出来高の勢い ---
    turnover = (vo / mc).replace([np.inf, -np.inf], np.nan)
    feats["turnover"] = turnover
    feats["turnover_ma7"] = turnover.rolling(7, min_periods=3).mean()
    feats["turnover_surge"] = turnover / turnover.rolling(30, min_periods=10).mean()
    feats["volume_surge_7"] = vo / vo.rolling(7, min_periods=3).mean()
    feats["volume_surge_30"] = vo / vo.rolling(30, min_periods=10).mean()
    feats["amihud"] = _safe_log((ret1.abs() / (vo / 1e6)).replace([np.inf, -np.inf], np.nan))

    # --- レンジ内での位置 ---
    for n in (14, 30, 90):
        feats["from_high_%d" % n] = px / px.rolling(n, min_periods=max(5, n // 3)).max() - 1.0
        feats["from_low_%d" % n] = px / px.rolling(n, min_periods=max(5, n // 3)).min() - 1.0
    feats["ma_ratio_7"] = px / px.rolling(7, min_periods=3).mean() - 1.0
    feats["ma_ratio_30"] = px / px.rolling(30, min_periods=10).mean() - 1.0

    # --- 時価総額順位とその変化 ---
    mcap_rank = mc.rank(axis=1, ascending=False)
    feats["mcap_rank"] = mcap_rank
    feats["mcap_rank_chg_7"] = mcap_rank.shift(7) - mcap_rank    # 正 = 順位が上がった
    feats["mcap_rank_chg_30"] = mcap_rank.shift(30) - mcap_rank

    # --- BTC 対比（超過リターン）と市場地合い ---
    if "bitcoin" in logpx.columns:
        btc_log = logpx["bitcoin"]
        for n in (7, 30):
            btc_ret = btc_log.diff(n)
            feats["exret_%d" % n] = feats["ret_%d" % n].sub(btc_ret, axis=0)
        btc_ret1 = btc_log.diff()
        feats["corr_btc_60"] = ret1.rolling(60, min_periods=30).corr(btc_ret1)
        feats["mkt_btc_ret_7"] = pd.DataFrame(
            np.repeat(btc_log.diff(7).to_numpy()[:, None], px.shape[1], axis=1),
            index=px.index, columns=px.columns)
        feats["mkt_btc_vol_30"] = pd.DataFrame(
            np.repeat(btc_ret1.rolling(30, min_periods=15).std().to_numpy()[:, None],
                      px.shape[1], axis=1),
            index=px.index, columns=px.columns)

    breadth = (logpx.diff(7) > 0).sum(axis=1) / logpx.diff(7).notna().sum(axis=1)
    feats["mkt_breadth_7"] = pd.DataFrame(
        np.repeat(breadth.to_numpy()[:, None], px.shape[1], axis=1),
        index=px.index, columns=px.columns)

    # --- ニュース特徴量（データが十分たまっている場合のみ） ---
    if include_news:
        feats.update(_news_features(px, min_news_days=min_news_days))

    # --- 目的変数: horizon 日先の対数リターン（t → t+h） ---
    fwd = logpx.shift(-horizon) - logpx

    # --- ユニバース判定用の生値 ---
    raw = {
        "price": px,
        "market_cap": mc,
        "volume": vo,
        "dollar_volume_ma7": vo.rolling(7, min_periods=3).mean(),
        "vol_60_raw": ret1.rolling(60, min_periods=30).std(),
        "vol_14_raw": ret1.rolling(14, min_periods=7).std(),
        "history_days": px.notna().cumsum(),
    }

    # --- ロング形式へ ---
    def melt(df: pd.DataFrame, name: str) -> pd.DataFrame:
        return (df.stack(future_stack=True).rename(name)
                  .rename_axis(["date", "coin_id"]).reset_index())

    out = melt(fwd, "fwd_ret")
    for name, df in {**raw, **feats}.items():
        out = out.merge(melt(df, name), on=["date", "coin_id"], how="left")

    meta = panel[["coin_id", "symbol", "name"]].drop_duplicates("coin_id")
    out = out.merge(meta, on="coin_id", how="left")
    out.attrs["feature_cols"] = list(feats.keys())
    out.attrs["horizon"] = horizon
    return out


# ---------------------------------------------------------------- ユニバース
def _is_excluded_name(symbol, name, enabled: bool) -> bool:
    if not enabled:
        return False
    if isinstance(symbol, str) and symbol.upper() in _EXCLUDE_SYMBOLS:
        return True
    if isinstance(name, str):
        low = name.lower()
        if any(k in low for k in _EXCLUDE_NAME_KEYWORDS):
            return True
    return False


def apply_universe(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """取引対象として妥当な (date, coin) だけ残す。"""
    u = dict(DEFAULT_UNIVERSE)
    if cfg:
        u.update(cfg)

    excluded = df.apply(
        lambda r: _is_excluded_name(r["symbol"], r["name"], u["exclude_wrapped"]), axis=1)

    turnover = (df["volume"] / df["market_cap"]).replace([np.inf, -np.inf], np.nan)

    keep = (
        df["price"].notna()
        & (df["market_cap"] >= u["min_market_cap"])
        & (df["dollar_volume_ma7"] >= u["min_dollar_volume"])
        & (turnover >= u["min_turnover"])
        & (df["vol_60_raw"] >= u["max_stable_vol"])       # ステーブルコイン除外
        & (df["vol_14_raw"] >= u["max_stable_vol_recent"])
        & (df["history_days"] >= u["min_history_days"])
        & ~excluded
    )
    out = df.loc[keep].copy()
    out.attrs.update(df.attrs)
    return out


# ---------------------------------------------------------------- 正規化
def cross_sectional_normalize(df: pd.DataFrame, feature_cols: list[str],
                              min_names: int = 30) -> pd.DataFrame:
    """各日ごとに特徴量と目的変数をクロスセクション順位（0〜1）へ変換する。"""
    df = df.copy()
    counts = df.groupby("date")["coin_id"].transform("size")
    df = df.loc[counts >= min_names].copy()

    g = df.groupby("date", observed=True)
    for c in feature_cols:
        df["cs_" + c] = g[c].rank(pct=True)
    df["y_rank"] = g["fwd_ret"].rank(pct=True)
    df["cs_cols"] = None
    df.attrs["cs_cols"] = ["cs_" + c for c in feature_cols]
    return df.drop(columns="cs_cols")


def build_dataset(horizon: int = 7, universe_cfg: dict | None = None,
                  include_news: bool = True, min_news_days: int = 60):
    """パネル読み込み → 特徴量 → ユニバース → 正規化 を通しで実行する。"""
    panel = load_panel()
    feat = build_features(panel, horizon=horizon,
                          include_news=include_news, min_news_days=min_news_days)
    fcols = feat.attrs["feature_cols"]
    uni = apply_universe(feat, universe_cfg)
    ds = cross_sectional_normalize(uni, fcols)
    cs_cols = ["cs_" + c for c in fcols]
    ds.attrs["feature_cols"] = fcols
    ds.attrs["cs_cols"] = cs_cols
    ds.attrs["horizon"] = horizon
    return ds, cs_cols


if __name__ == "__main__":
    ds, cs_cols = build_dataset()
    print("データセット形状 :", ds.shape)
    print("特徴量数         :", len(cs_cols))
    if ds.empty:
        print("")
        print("！ ユニバース条件を満たす日がまだありません。")
        print("  python src/backfill.py で過去データを取得するか、")
        print("  日次収集が min_history_days 日分たまるのを待ってください。")
        raise SystemExit(0)
    print("期間             : %s 〜 %s" % (ds["date"].min().date(), ds["date"].max().date()))
    per_day = ds.groupby("date")["coin_id"].size()
    print("1日あたり銘柄数   : 中央値 %d (最小 %d / 最大 %d)"
          % (per_day.median(), per_day.min(), per_day.max()))
    lab = ds["y_rank"].notna().sum()
    print("学習可能ラベル数 : {:,} 行".format(lab))
