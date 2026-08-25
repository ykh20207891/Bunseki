"""蓄積データから自己完結型の HTML ダッシュボードを生成する。

PC とスマホの両方で見る前提なので、
  - モバイルは1カラム、720px 以上で2カラム、1100px 以上で3カラム
  - 表はスマホ幅ではカード状に積み替える（横スクロールさせない）
  - ライト/ダークの両テーマに対応（OS設定・明示指定のどちらも）
外部リソースは Google Fonts のみ。データは HTML に直接埋め込むので単体で完結する。

使い方:
    python src/dashboard.py                # reports/dashboard.html を生成
    python src/dashboard.py --open         # 生成してブラウザで開く
"""
from __future__ import annotations

import argparse
import html
import math
import re
import sys
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import REPORT_DIR, ensure_dirs, setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402
from icons import load_icon_map, load_source_icon_map  # noqa: E402

LOG = setup_logger("dashboard")

# ---------------------------------------------------------------- 表示ヘルパ
def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def fmt_usd(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return "$%.2f%s" % (v / div, unit)
    return "$%.2f" % v


def fmt_price(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    if v >= 1000:
        return "$" + format(v, ",.0f")
    if v >= 1:
        return "$" + format(v, ",.2f")
    if v >= 0.01:
        return "$%.4f" % v
    if v <= 0:
        return "$0"
    # ミームコイン等は 1e-8 台まであるので、有効数字4桁が残るよう桁数を動的に決める
    decimals = min(12, max(4, 3 - math.floor(math.log10(v))))
    return "$%.*f" % (decimals, v)


def fmt_pct(v, digits=1) -> str:
    if v is None:
        return "—"
    return "%+.*f%%" % (digits, float(v))


def delta_class(v) -> str:
    if v is None:
        return "flat"
    return "up" if float(v) > 0 else ("down" if float(v) < 0 else "flat")


def sparkline(values, width=120, height=32) -> str:
    """SVG のスパークライン。終点だけ強調する。"""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 3:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = [(i * width / (n - 1), height - 2 - (v - lo) / rng * (height - 4))
           for i, v in enumerate(vals)]
    d = " ".join("%s%.1f,%.1f" % ("M" if i == 0 else "L", x, y) for i, (x, y) in enumerate(pts))
    area = d + " L%.1f,%.1f L0,%.1f Z" % (width, height, height)
    ex, ey = pts[-1]
    trend = "up" if vals[-1] >= vals[0] else "down"
    return (
        '<svg class="spark spark-%s" viewBox="0 0 %d %d" width="%d" height="%d" '
        'preserveAspectRatio="none" aria-hidden="true">'
        '<path class="spark-area" d="%s"/><path class="spark-line" d="%s"/>'
        '<circle class="spark-dot" cx="%.1f" cy="%.1f" r="2.4"/></svg>'
        % (trend, width, height, width, height, area, d, ex, ey))


# ---------------------------------------------------------------- 答え合わせ
def _spearman(xs, ys) -> float | None:
    """順位相関。pandas を持ち込まずに済むよう自前で計算する。"""
    n = len(xs)
    if n < 5:
        return None

    def ranks(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        r = [0.0] * n
        i = 0
        while i < n:                       # 同値は平均順位にする
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return (num / (dx * dy)) if dx and dy else None


def gather_evaluation(conn, top_k: int = 10) -> dict:
    """出した予測が実際どうなったかを集計する。

    予測時点の価格と horizon 日後の価格を突き合わせる。horizon 日が経過して
    いない予測は「判定待ち」として残す。ここはバックテストと違い実運用の記録
    なので、生存者バイアスも後知恵も入らない。
    """
    rows = conn.execute(
        "SELECT p.predicted_on, p.horizon_days, p.model_tag, p.coin_id, p.symbol,"
        " p.rank, p.price_at_pred AS p0,"
        " COALESCE(s.price, h.price) AS p1,"
        " date(p.predicted_on, '+' || p.horizon_days || ' day') AS judge_date"
        " FROM predictions p"
        " LEFT JOIN snapshots s ON s.coin_id = p.coin_id"
        "   AND s.snapshot_date = date(p.predicted_on, '+' || p.horizon_days || ' day')"
        " LEFT JOIN price_history h ON h.coin_id = p.coin_id"
        "   AND h.date = date(p.predicted_on, '+' || p.horizon_days || ' day')"
        " ORDER BY p.predicted_on DESC, p.rank").fetchall()

    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["predicted_on"], r["horizon_days"], r["model_tag"]), []).append(r)

    judged, pending = [], []
    for (pon, hz, tag), items in groups.items():
        judge_date = items[0]["judge_date"]
        done = [x for x in items
                if x["p0"] and x["p1"] and x["p0"] > 0]
        if len(done) < 20:
            pending.append({"predicted_on": pon, "horizon": hz, "model_tag": tag,
                            "judge_date": judge_date, "n": len(items)})
            continue

        rets = {x["coin_id"]: (x["p1"] / x["p0"] - 1.0) for x in done}
        ordered = sorted(done, key=lambda x: x["rank"])
        top = ordered[:top_k]
        bottom = ordered[-top_k:]
        top_ret = sum(rets[x["coin_id"]] for x in top) / len(top)
        bottom_ret = sum(rets[x["coin_id"]] for x in bottom) / len(bottom)
        univ_ret = sum(rets.values()) / len(rets)
        ic = _spearman([-x["rank"] for x in done], [rets[x["coin_id"]] for x in done])

        judged.append({
            "predicted_on": pon, "judge_date": judge_date, "horizon": hz,
            "model_tag": tag, "n": len(done), "ic": ic,
            "top_ret": top_ret, "bottom_ret": bottom_ret, "univ_ret": univ_ret,
            "excess": top_ret - univ_ret,
            "detail": [{"rank": x["rank"], "symbol": x["symbol"], "coin_id": x["coin_id"],
                        "ret": rets[x["coin_id"]], "p0": x["p0"], "p1": x["p1"]}
                       for x in top],
        })

    judged.sort(key=lambda d: d["predicted_on"], reverse=True)
    pending.sort(key=lambda d: d["predicted_on"], reverse=True)
    return {"judged": judged, "pending": pending, "top_k": top_k}


# ---------------------------------------------------------------- データ取得
def gather(conn) -> dict:
    d: dict = {}
    latest = conn.execute("SELECT MAX(snapshot_date) FROM snapshots").fetchone()[0]
    d["latest"] = latest

    d["coverage"] = conn.execute(
        "SELECT COUNT(DISTINCT snapshot_date) days, MIN(snapshot_date) d0,"
        " MAX(snapshot_date) d1, COUNT(*) rows FROM snapshots").fetchone()
    d["hist"] = conn.execute(
        "SELECT COUNT(DISTINCT coin_id) coins, COUNT(*) rows, MIN(date) d0, MAX(date) d1"
        " FROM price_history").fetchone()
    d["news_cov"] = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT pub_date) days, MIN(pub_date) d0,"
        " MAX(pub_date) d1 FROM news_articles").fetchone()
    d["glob"] = conn.execute(
        "SELECT * FROM global_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
    d["n_coins_today"] = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE snapshot_date = ?", (latest,)).fetchone()[0] if latest else 0

    # 直近の収集ログ
    d["logs"] = conn.execute(
        "SELECT snapshot_date, status, rows_written, message, finished_at"
        " FROM collect_log ORDER BY id DESC LIMIT 8").fetchall()

    # 値動きランキング（安定した銘柄に限る: 時価総額と出来高で足切り）
    base = ("SELECT symbol, name, coin_id, price, market_cap, total_volume,"
            " pct_24h, pct_7d, pct_30d, mcap_rank FROM snapshots"
            " WHERE snapshot_date = ? AND market_cap >= 100000000"
            " AND total_volume >= 5000000 AND pct_7d IS NOT NULL")
    d["gainers"] = conn.execute(base + " ORDER BY pct_7d DESC LIMIT 10", (latest,)).fetchall() if latest else []
    d["losers"] = conn.execute(base + " ORDER BY pct_7d ASC LIMIT 10", (latest,)).fetchall() if latest else []
    d["top_mcap"] = conn.execute(
        "SELECT symbol, name, coin_id, price, market_cap, total_volume, pct_24h, pct_7d,"
        " mcap_rank FROM snapshots WHERE snapshot_date = ? AND market_cap IS NOT NULL"
        # CoinGecko の rank と時価総額の順序がまれにずれるので、表示順は rank に合わせる
        " ORDER BY (mcap_rank IS NULL), mcap_rank, market_cap DESC LIMIT 12",
        (latest,)).fetchall() if latest else []

    # ニュース
    d["news_top"] = conn.execute(
        "SELECT nm.coin_id, COUNT(*) n, AVG(na.sentiment) s,"
        " (SELECT symbol FROM snapshots WHERE coin_id = nm.coin_id"
        "  ORDER BY snapshot_date DESC LIMIT 1) sym"
        " FROM news_mentions nm JOIN news_articles na ON na.id = nm.article_id"
        " WHERE nm.pub_date >= date('now','-7 day')"
        " GROUP BY nm.coin_id HAVING n >= 1 ORDER BY n DESC LIMIT 12").fetchall()
    d["news_recent"] = conn.execute(
        "SELECT title, title_ja, lang, source, link, sentiment, published_at"
        " FROM news_articles ORDER BY published_at DESC LIMIT 16").fetchall()

    # 予測
    pred_meta = conn.execute(
        "SELECT predicted_on, horizon_days, model_tag FROM predictions"
        " ORDER BY predicted_on DESC, created_at DESC LIMIT 1").fetchone()
    d["pred_meta"] = pred_meta
    d["preds"] = []
    if pred_meta:
        d["preds"] = conn.execute(
            "SELECT p.rank, p.symbol, p.coin_id, p.score, p.price_at_pred,"
            " (SELECT name FROM snapshots WHERE coin_id = p.coin_id"
            "  ORDER BY snapshot_date DESC LIMIT 1) name,"
            " (SELECT pct_7d FROM snapshots WHERE coin_id = p.coin_id"
            "  ORDER BY snapshot_date DESC LIMIT 1) pct_7d"
            " FROM predictions p WHERE p.predicted_on = ? AND p.horizon_days = ?"
            " AND p.model_tag = ? ORDER BY p.rank LIMIT 15",
            (pred_meta["predicted_on"], pred_meta["horizon_days"],
             pred_meta["model_tag"])).fetchall()

    d["eval"] = gather_evaluation(conn)

    # BTC と総時価総額のスパークライン用
    d["btc_series"] = [r[0] for r in conn.execute(
        "SELECT price FROM price_history WHERE coin_id='bitcoin'"
        " ORDER BY date DESC LIMIT 60").fetchall()][::-1]
    d["mcap_series"] = [r[0] for r in conn.execute(
        "SELECT total_market_cap FROM global_snapshots"
        " ORDER BY snapshot_date DESC LIMIT 60").fetchall()][::-1]
    return d


# ---------------------------------------------------------------- ファビコン
# 外部ファイルを置けないので SVG を data URI で直接埋め込む。
# 黒地に、スパークラインと同じ上昇ラインを置いた記号にしてページの意匠と揃える。
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#000000"/>'
    '<path d="M11 45 L25 31 L36 38 L52 17" fill="none" stroke="#4FB3CF"'
    ' stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>'
    '<circle cx="52" cy="17" r="6" fill="#3FBF92"/>'
    '</svg>'
)
FAVICON_URI = "data:image/svg+xml," + urllib.parse.quote(_FAVICON_SVG, safe="")


# ---------------------------------------------------------------- HTML 部品
STYLE = """
/* 単一テーマ（ブラック）に固定している。閲覧側のライト/ダーク設定に関係なく
   同じ見た目になるよう、色はすべてここで明示的に定義する。 */
:root{
  --ground:#000000; --surface:#0B0F12; --surface-2:#151B21;
  --ink:#E8EEF2; --ink-2:#B4C1CB; --muted:#7C8B97;
  --line:#1E272E; --line-soft:#161D23;
  --accent:#4FB3CF; --accent-soft:#0F2C36; --accent-ink:#8FD4E8;
  --up:#3FBF92; --up-soft:#0E2A22; --down:#E4736B; --down-soft:#2C1917;
  --shadow:0 0 0 1px rgba(255,255,255,.02), 0 8px 24px -16px rgba(0,0,0,.9);
  color-scheme:dark;
}

*{box-sizing:border-box}
html{overflow-x:hidden}
body{
  margin:0; background:var(--ground); color:var(--ink); overflow-x:hidden;
  font-family:"Source Sans 3","Hiragino Kaku Gothic ProN","Yu Gothic UI",system-ui,sans-serif;
  font-size:15px; line-height:1.6; -webkit-text-size-adjust:100%;
}
/* 数値は等幅フォントをやめ、本文と同じプロポーショナル書体 + tabular-nums にした。
   等幅だと小数点やカンマまで数字と同じ幅の枠に入るため「$2. 68T」のように
   間延びして読みにくい。tabular-nums なら数字の幅だけ揃うので桁は縦に揃いつつ、
   句読点は自然な幅になる。黒背景では細く見えるので太さは 500。 */
.num{font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1;
  letter-spacing:0; font-weight:500}
h1,h2,h3{font-family:"Archivo","Hiragino Kaku Gothic ProN","Yu Gothic UI",system-ui,sans-serif;
  text-wrap:balance; margin:0}

.wrap{max-width:1240px; margin:0 auto; padding:0 16px 64px}

/* ---- ヘッダ ---- */
.masthead{
  border-bottom:1px solid var(--line); background:var(--surface);
  position:sticky; top:0; z-index:20;
}
.masthead-in{max-width:1240px; margin:0 auto; padding:14px 16px;
  display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 16px}
.masthead h1{font-size:19px; font-weight:700; letter-spacing:-.015em}
.masthead .sub{color:var(--muted); font-size:12.5px}
.pills{display:flex; flex-wrap:wrap; gap:6px; margin-left:auto}
.pill{
  font-size:11.5px; padding:3px 9px; border-radius:999px; white-space:nowrap;
  border:1px solid var(--line); color:var(--ink-2); background:var(--surface-2);
}
.pill.ok{background:var(--up-soft); border-color:transparent; color:var(--up)}
.pill.warn{background:var(--down-soft); border-color:transparent; color:var(--down)}

/* ---- レイアウト ---- */
.grid{display:grid; gap:16px; margin-top:20px; grid-template-columns:1fr}
@media (min-width:720px){ .grid{grid-template-columns:repeat(2,1fr)} }
@media (min-width:1100px){ .grid{grid-template-columns:repeat(3,1fr)} }
.span-2{grid-column:span 1}
@media (min-width:720px){ .span-2{grid-column:span 2} }
.span-all{grid-column:1/-1}

.card{
  background:var(--surface); border:1px solid var(--line); border-radius:10px;
  padding:16px; box-shadow:var(--shadow); min-width:0;
  container-type:inline-size;   /* カード幅に応じて中身を詰められるようにする */
}
/* 3カラム表示だとカードが 330px 程度になり、表がわずかにはみ出す。
   画面幅ではなくカード幅で判定して余白と銘柄名の幅を詰める。 */
@container (max-width:380px){
  .card td{padding:9px 4px}
  .card th{padding:0 4px 7px}
  .card .nm{max-width:95px}
  .card td.c-sub span + span{margin-left:10px}
}
.card > h2{
  font-size:12px; font-weight:700; letter-spacing:.09em; text-transform:uppercase;
  color:var(--muted); margin-bottom:12px;
  display:flex; align-items:center; gap:8px;
}
.card > h2::after{content:""; flex:1; height:1px; background:var(--line-soft)}
.card .note{font-size:12.5px; color:var(--muted); margin-top:10px}

/* ---- 指標タイル ---- */
.tiles{display:grid; grid-template-columns:repeat(2,1fr); gap:12px}
@media (min-width:520px){ .tiles{grid-template-columns:repeat(4,1fr)} }
.tile{background:var(--surface-2); border-radius:8px; padding:12px 13px; min-width:0}
.tile .k{font-size:11px; color:var(--muted); letter-spacing:.04em}
.tile .v{font-size:20px; font-weight:600; margin-top:3px; word-break:break-all}
.tile .d{font-size:12.5px; margin-top:2px}

.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--muted)}

/* ---- スパークライン ---- */
.spark{display:block; width:100%; height:34px; margin-top:8px; overflow:visible}
.spark-line{fill:none; stroke-width:1.6; vector-effect:non-scaling-stroke}
.spark-area{stroke:none; opacity:.13}
.spark-up .spark-line,.spark-up .spark-dot{stroke:var(--up); fill:var(--up)}
.spark-up .spark-area{fill:var(--up)}
.spark-down .spark-line,.spark-down .spark-dot{stroke:var(--down); fill:var(--down)}
.spark-down .spark-area{fill:var(--down)}
.spark-dot{stroke-width:0}

/* ---- 表（PC）---- */
table{width:100%; border-collapse:collapse; font-size:14px}
th{
  text-align:left; font-size:11px; letter-spacing:.06em; text-transform:uppercase;
  color:var(--muted); font-weight:600; padding:0 8px 7px; border-bottom:1px solid var(--line);
  white-space:nowrap;
}
td{padding:10px 8px; border-bottom:1px solid var(--line-soft); vertical-align:middle}
td.c-main{font-size:14.5px; font-weight:600}
tr:last-child td{border-bottom:none}
th.r,td.r{text-align:right}
/* 価格と時価総額を1セルに並べる。td を flex にすると table-cell でなくなり
   行の高さ計算から外れて左右の罫線がずれるので、間隔は span 間のマージンで取る。
   またセルは折り返しを許し、各値の内部だけ nowrap にする。こうすると幅が足りないとき
   時価総額が次の行に落ちるだけで済み、見切れも横スクロールも起きない。 */
td.c-sub{text-align:right}
td.c-sub span{color:var(--muted); font-size:12.5px;
  display:inline-block; white-space:nowrap}
td.c-sub span + span{margin-left:14px}

/* 銘柄セル: アイコン + シンボル/名称 */
.coin{display:flex; align-items:center; gap:9px; min-width:0}
.icon{width:22px; height:22px; border-radius:50%; flex:0 0 22px;
  object-fit:cover; background:var(--surface-2)}
.icon-ph{width:22px; height:22px; border-radius:50%; flex:0 0 22px;
  background:var(--surface-2); color:var(--muted); font-size:10px; font-weight:600;
  display:flex; align-items:center; justify-content:center}
.sym{font-weight:600; letter-spacing:-.01em; display:block}
.nm{color:var(--muted); font-size:12px; display:block; margin-top:-2px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:150px}
.rk{color:var(--muted); font-size:12px; width:26px}

/* スコアバー */
.bar{position:relative; height:5px; border-radius:3px; background:var(--surface-2); min-width:54px}
.bar > i{position:absolute; inset:0 auto 0 0; border-radius:3px; background:var(--accent)}

/* ---- 表（スマホではカードに積み替え）---- */
@media (max-width:560px){
  table.stack thead{display:none}
  table.stack tr{
    display:grid; grid-template-columns:auto 1fr auto; gap:2px 10px;
    padding:10px 0; border-bottom:1px solid var(--line-soft); align-items:center;
  }
  table.stack td{display:block; border:none; padding:0}
  table.stack td.c-rank{grid-row:1/3; color:var(--muted); font-size:12px}
  table.stack td.c-name{grid-column:2; min-width:0}
  table.stack td.c-main{grid-column:3; text-align:right; font-weight:600}
  /* スマホでは tr が grid・td が block なので、ここでは flex にしても崩れない */
  table.stack td.c-sub{
    grid-column:2/4; display:flex; gap:12px; flex-wrap:wrap; text-align:left;
    font-size:12px; color:var(--muted); margin-top:2px;
  }
  table.stack td.c-sub span + span{margin-left:0}
  table.stack td.c-sub span::before{content:attr(data-label)" "; color:var(--muted); opacity:.8}
  .nm{max-width:none}
}

/* ---- ニュース ---- */
.news{list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:11px}
.news li{display:flex; gap:10px; align-items:flex-start}
.news a{color:var(--ink); text-decoration:none; font-size:13.5px; line-height:1.45}
.news a:hover{color:var(--accent); text-decoration:underline}
.news .src{font-size:11px; color:var(--muted); margin-top:3px;
  display:flex; align-items:center; gap:5px; flex-wrap:wrap}
.src-icon{width:14px; height:14px; border-radius:3px; flex:0 0 14px;
  object-fit:contain; background:var(--surface-2)}
.mt{font-size:10px; padding:1px 5px; border-radius:3px;
  background:var(--surface-2); color:var(--muted); letter-spacing:.02em}
.dot{width:7px; height:7px; border-radius:50%; flex:0 0 7px; margin-top:6px; background:var(--muted)}
.dot.up{background:var(--up)} .dot.down{background:var(--down)}

/* ---- 状態表示 ---- */
.status-row{display:flex; justify-content:space-between; gap:10px; font-size:13px;
  padding:7px 0; border-bottom:1px solid var(--line-soft)}
.status-row:last-child{border-bottom:none}
.tag{font-size:11px; padding:2px 7px; border-radius:4px; background:var(--surface-2); color:var(--muted)}
.tag.ok{background:var(--up-soft); color:var(--up)}
.tag.warn{background:var(--down-soft); color:var(--down)}

.alert{
  margin-top:20px; padding:13px 16px; border-radius:10px; font-size:14px;
  background:var(--down-soft); color:var(--down);
  border:1px solid color-mix(in srgb, var(--down) 35%, transparent);
}
.alert strong{display:block; margin-bottom:2px}
.alert code{font-family:ui-monospace,monospace; font-size:13px;
  background:rgba(0,0,0,.35); padding:1px 5px; border-radius:3px}
.empty{
  color:var(--muted); font-size:13.5px; background:var(--surface-2);
  border:1px dashed var(--line); border-radius:8px; padding:16px; text-align:center;
}

footer{margin-top:28px; padding-top:18px; border-top:1px solid var(--line);
  color:var(--muted); font-size:12.5px}
footer strong{color:var(--ink-2)}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent); outline-offset:2px; border-radius:3px}
@media (prefers-reduced-motion:reduce){*{animation:none!important; transition:none!important}}
"""


def coin_cell(coin_id, symbol, name, icons: dict) -> str:
    """アイコン付きの銘柄セルの中身を返す。アイコンが無ければ頭文字で代替する。"""
    uri = icons.get(coin_id)
    if uri:
        # data URI なので遅延読み込みの利点はなく、描画が遅れる分むしろ不利
        img = '<img class="icon" src="%s" alt="" width="22" height="22">' % uri
    else:
        img = '<span class="icon-ph">%s</span>' % esc((symbol or "?")[:2].upper())
    return ('<div class="coin">%s<div style="min-width:0">'
            '<span class="sym">%s</span><span class="nm">%s</span>'
            '</div></div>' % (img, esc(symbol), esc(name or "")))


def tile(k, v, d="", dclass="flat", spark="") -> str:
    return ('<div class="tile"><div class="k">%s</div>'
            '<div class="v num">%s</div><div class="d num %s">%s</div>%s</div>'
            % (esc(k), esc(v), dclass, esc(d), spark))


def coin_rows(rows, main_key, main_fmt, icons: dict) -> str:
    out = []
    for r in rows:
        main = main_fmt(r[main_key])
        cls = delta_class(r[main_key]) if main_key.startswith("pct") else ""
        out.append(
            '<tr>'
            '<td class="c-rank rk num">%s</td>'
            '<td class="c-name">%s</td>'
            '<td class="c-main r num %s">%s</td>'
            '<td class="c-sub r num"><span data-label="価格">%s</span>'
            '<span data-label="時価総額">%s</span></td>'
            '</tr>'
            % (esc(r["mcap_rank"] if r["mcap_rank"] else "–"),
               coin_cell(r["coin_id"], r["symbol"], r["name"], icons),
               cls, esc(main),
               esc(fmt_price(r["price"])), esc(fmt_usd(r["market_cap"]))))
    return "".join(out)


def table_block(rows, main_label, main_key, main_fmt, icons: dict) -> str:
    if not rows:
        return '<div class="empty">データがまだありません。</div>'
    return (
        '<div style="overflow-x:auto"><table class="stack">'
        '<thead><tr><th>#</th><th>銘柄</th><th class="r">%s</th>'
        '<th class="r">価格 / 時価総額</th></tr></thead><tbody>%s</tbody></table></div>'
        % (esc(main_label), coin_rows(rows, main_key, main_fmt, icons)))


def build_html(d: dict) -> str:
    now = datetime.now(timezone.utc)
    cov, hist, nc, g = d["coverage"], d["hist"], d["news_cov"], d["glob"]
    latest = d["latest"]
    # ヘッダーに差し込む文字列はここで確定させる。以降のブロック生成で
    # 同名の変数を使ってしまっても影響が及ばないようにするため。
    snapshot_label = esc(latest or "—")

    # 表示する銘柄のアイコンだけを data URI で埋め込む（Artifact は外部画像を読めない）
    shown = ([r["coin_id"] for r in d["gainers"]] + [r["coin_id"] for r in d["losers"]]
             + [r["coin_id"] for r in d["top_mcap"]] + [r["coin_id"] for r in d["preds"]]
             + [r["coin_id"] for r in d["news_top"]])
    icons = load_icon_map(shown)

    # データが古いことに気づけないのが一番まずい（実際に日次実行が空振りしていた）。
    # 何日遅れているかを明示し、遅れていれば冒頭に警告を出す。
    today = now.strftime("%Y-%m-%d")
    fresh = (latest == today)
    stale_days = 0
    if latest:
        try:
            stale_days = (datetime.strptime(today, "%Y-%m-%d")
                          - datetime.strptime(latest, "%Y-%m-%d")).days
        except ValueError:
            stale_days = 0
    pills = [
        '<span class="pill %s">価格 %s日分 / %s銘柄</span>'
        % ("ok" if fresh else "warn", cov["days"] or 0, d["n_coins_today"]),
        '<span class="pill">履歴 %s銘柄 · %s行</span>'
        % (hist["coins"] or 0, format(hist["rows"] or 0, ",")),
        '<span class="pill %s">ニュース %s日分 · %s件</span>'
        % ("ok" if (nc["n"] or 0) else "warn", nc["days"] or 0, format(nc["n"] or 0, ",")),
    ]

    # --- 市場サマリー ---
    if g:
        mcap_spark = sparkline(d["mcap_series"]) if len(d["mcap_series"]) >= 3 else ""
        btc_spark = sparkline(d["btc_series"]) if len(d["btc_series"]) >= 3 else ""
        btc_chg = None
        if len(d["btc_series"]) >= 8 and d["btc_series"][-8]:
            btc_chg = (d["btc_series"][-1] / d["btc_series"][-8] - 1) * 100
        tiles = "".join([
            tile("総時価総額", fmt_usd(g["total_market_cap"]),
                 fmt_pct(g["mcap_change_24h_pct"], 2) + " (24h)",
                 delta_class(g["mcap_change_24h_pct"]), mcap_spark),
            tile("BTC ドミナンス", "%.1f%%" % (g["btc_dominance"] or 0),
                 "ETH %.1f%%" % (g["eth_dominance"] or 0)),
            tile("BTC 価格", fmt_price(d["btc_series"][-1] if d["btc_series"] else None),
                 (fmt_pct(btc_chg) + " (7日)") if btc_chg is not None else "",
                 delta_class(btc_chg), btc_spark),
            tile("24h 出来高", fmt_usd(g["total_volume"]),
                 "%s 銘柄が稼働" % format(g["active_cryptos"] or 0, ",")),
        ])
        market = '<div class="tiles">%s</div>' % tiles
    else:
        market = '<div class="empty">市場データがまだありません。</div>'

    # --- 予測 ---
    pm = d["pred_meta"]
    if pm and d["preds"]:
        rows = "".join(
            '<tr><td class="c-rank rk num">%d</td>'
            '<td class="c-name">%s</td>'
            '<td class="c-main r num">%.3f</td>'
            '<td class="c-sub r"><span data-label="7日" class="num %s">%s</span>'
            '<span data-label="価格" class="num">%s</span></td></tr>'
            % (p["rank"], coin_cell(p["coin_id"], p["symbol"], p["name"], icons),
               p["score"], delta_class(p["pct_7d"]), esc(fmt_pct(p["pct_7d"])),
               esc(fmt_price(p["price_at_pred"])))
            for p in d["preds"])
        pred_block = (
            '<div style="overflow-x:auto"><table class="stack"><thead><tr>'
            '<th>#</th><th>銘柄</th><th class="r">スコア</th>'
            '<th class="r">直近7日 / 価格</th></tr></thead><tbody>%s</tbody></table></div>'
            '<p class="note">基準日 %s ・ %d日先の相対順位予測 ・ モデル %s<br>'
            'スコアは「このユニバース内での相対的な期待順位」です。'
            '上昇を示すものではなく、売買推奨でもありません。</p>'
            % (rows, esc(pm["predicted_on"]), pm["horizon_days"], esc(pm["model_tag"])))
    else:
        pred_block = (
            '<div class="empty">予測はまだ生成されていません。<br>'
            '過去データの取得が終わったら <code>python src/predict.py</code> を実行してください。</div>')

    # --- データ鮮度の警告 ---
    if stale_days >= 1:
        alert = (
            '<div class="alert"><strong>データが %d 日前で止まっています</strong>'
            '（最新 %s / 本日 %s）。日次収集が動いていない可能性があります。'
            '<code>collect.bat</code> を実行するか、タスクスケジューラの'
            '「CryptoDataCollect」の状態を確認してください。</div>'
            % (stale_days, esc(latest or "—"), esc(today)))
    else:
        alert = ""

    # --- 予測の答え合わせ ---
    ev = d["eval"]
    ev_parts = []
    if ev["judged"]:
        n = len(ev["judged"])
        avg_ic = sum(j["ic"] for j in ev["judged"] if j["ic"] is not None)
        ic_n = sum(1 for j in ev["judged"] if j["ic"] is not None)
        avg_ic = (avg_ic / ic_n) if ic_n else None
        avg_ex = sum(j["excess"] for j in ev["judged"]) / n
        wins = sum(1 for j in ev["judged"] if j["excess"] > 0)
        ev_parts.append(
            '<div class="tiles" style="grid-template-columns:repeat(3,1fr)">%s%s%s</div>'
            % (tile("判定済み", "%d 回" % n, "%d日先予測" % ev["judged"][0]["horizon"]),
               tile("平均IC", ("%+.3f" % avg_ic) if avg_ic is not None else "—",
                    "順位の当たり具合", delta_class(avg_ic)),
               tile("平均超過", fmt_pct(avg_ex * 100, 2),
                    "ユニバース比 ・ 勝ち %d/%d" % (wins, n), delta_class(avg_ex))))

        ev_parts.append(
            '<div style="overflow-x:auto"><table class="stack"><thead><tr>'
            '<th>基準日 → 判定日</th><th class="r">IC</th>'
            '<th class="r">上位%d</th><th class="r">ユニバース / 超過</th>'
            '</tr></thead><tbody>%s</tbody></table></div>'
            % (ev["top_k"], "".join(
                '<tr><td class="c-name"><span class="sym num">%s</span>'
                '<span class="nm num">→ %s</span></td>'
                '<td class="c-main r num %s">%s</td>'
                '<td class="c-main r num %s">%s</td>'
                '<td class="c-sub r num"><span data-label="ユニバース">%s</span>'
                '<span data-label="超過" class="%s">%s</span></td></tr>'
                % (esc(j["predicted_on"]), esc(j["judge_date"]),
                   delta_class(j["ic"]),
                   ("%+.3f" % j["ic"]) if j["ic"] is not None else "—",
                   delta_class(j["top_ret"]), fmt_pct(j["top_ret"] * 100, 2),
                   fmt_pct(j["univ_ret"] * 100, 2),
                   delta_class(j["excess"]), fmt_pct(j["excess"] * 100, 2))
                for j in ev["judged"][:8])))

        # 変数名は latest を避ける。ヘッダーで使うスナップショット日付と衝突するため。
        newest_judged = ev["judged"][0]
        ev_parts.append(
            '<h2 style="margin-top:18px">%s 予測の上位%d銘柄が実際どうなったか</h2>'
            '<div style="overflow-x:auto"><table class="stack"><tbody>%s</tbody></table></div>'
            % (esc(newest_judged["predicted_on"]), ev["top_k"], "".join(
                '<tr><td class="c-rank rk num">%d</td><td class="c-name">%s</td>'
                '<td class="c-main r num %s">%s</td>'
                '<td class="c-sub r num"><span data-label="予測時">%s</span>'
                '<span data-label="判定時">%s</span></td></tr>'
                % (x["rank"], coin_cell(x["coin_id"], x["symbol"], None, icons),
                   delta_class(x["ret"]), fmt_pct(x["ret"] * 100, 1),
                   esc(fmt_price(x["p0"])), esc(fmt_price(x["p1"])))
                for x in newest_judged["detail"])))

    if ev["pending"]:
        ev_parts.append(
            '<p class="note">判定待ち: %s</p>'
            % esc("、".join("%s の予測（%s に判定）" % (p["predicted_on"], p["judge_date"])
                            for p in ev["pending"][:4])))
    if not ev["judged"]:
        ev_parts.insert(0,
            '<div class="empty">まだ判定できる予測がありません。<br>'
            '予測を出してから %d 日経過すると、ここに実際の結果が出ます。</div>'
            % (ev["pending"][0]["horizon"] if ev["pending"] else 7))
    eval_block = "".join(ev_parts)

    # --- ニュース注目度 ---
    if d["news_top"]:
        maxn = max(r["n"] for r in d["news_top"]) or 1
        news_top = '<div style="overflow-x:auto"><table><tbody>' + "".join(
            '<tr><td>%s</td>'
            '<td style="width:40%%"><div class="bar"><i style="width:%.0f%%"></i></div></td>'
            '<td class="r num">%d件</td>'
            '<td class="r num %s">%s</td></tr>'
            % (coin_cell(r["coin_id"], r["sym"] or r["coin_id"], None, icons),
               r["n"] / maxn * 100, r["n"],
               delta_class(r["s"]), esc("%+.2f" % (r["s"] or 0)))
            for r in d["news_top"]) + '</tbody></table></div>'
        news_top += '<p class="note">直近7日の記事言及数と平均センチメント（-1〜+1）。</p>'
    else:
        news_top = '<div class="empty">ニュースの銘柄言及がまだありません。</div>'

    # --- 最新ニュース ---
    if d["news_recent"]:
        src_icons = load_source_icon_map()
        items = []
        for r in d["news_recent"]:
            # 日本語media は原文、英語media は機械翻訳。訳が無い間は原文を出す。
            headline = r["title_ja"] or r["title"]
            translated = bool(r["title_ja"]) and r["lang"] != "ja"
            uri = src_icons.get(r["source"])
            badge = ('<img class="src-icon" src="%s" alt="" width="14" height="14">' % uri
                     if uri else '<span class="src-icon"></span>')
            items.append(
                '<li><span class="dot %s"></span><div style="min-width:0">'
                '<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>'
                '<span class="src">%s%s ・ %s%s</span></div></li>'
                % (delta_class(r["sentiment"]), esc(r["link"]), esc(headline),
                   badge, esc(r["source"]),
                   esc((r["published_at"] or "")[:16].replace("T", " ")),
                   '<span class="mt">自動翻訳</span>' if translated else ""))
        news_list = '<ul class="news">' + "".join(items) + '</ul>'
    else:
        news_list = '<div class="empty">記事がまだありません。</div>'

    # --- 収集状況 ---
    if d["logs"]:
        logs = "".join(
            '<div class="status-row"><span class="num">%s</span>'
            '<span><span class="tag %s">%s</span> '
            '<span class="num" style="color:var(--muted)">%s</span></span></div>'
            % (esc(r["snapshot_date"]),
               "ok" if r["status"] == "ok" else ("warn" if r["status"] == "error" else ""),
               esc(r["status"]), esc(r["message"] or ""))
            for r in d["logs"])
    else:
        logs = '<div class="empty">収集ログがありません。</div>'

    coverage_note = (
        '<div class="status-row"><span>日次スナップショット</span>'
        '<span class="num">%s 〜 %s</span></div>'
        '<div class="status-row"><span>過去データ（バックフィル）</span>'
        '<span class="num">%s 〜 %s</span></div>'
        '<div class="status-row"><span>ニュース</span><span class="num">%s 〜 %s</span></div>'
        % (esc(cov["d0"] or "—"), esc(cov["d1"] or "—"),
           esc(hist["d0"] or "—"), esc(hist["d1"] or "—"),
           esc(nc["d0"] or "—"), esc(nc["d1"] or "—")))

    body = """
<header class="masthead"><div class="masthead-in">
  <h1>仮想通貨 分析ラボ</h1>
  <span class="sub num">{LATEST} 時点 (UTC)</span>
  <div class="pills">{PILLS}</div>
</div></header>

<div class="wrap">
  {ALERT}
  <div class="grid">
    <section class="card span-all"><h2>市場サマリー</h2>{MARKET}</section>

    <section class="card span-2"><h2>ランキング予測（{HORIZON}日先）</h2>{PRED}</section>

    <section class="card"><h2>ニュース注目度</h2>{NEWSTOP}</section>

    <section class="card span-all"><h2>予測の答え合わせ</h2>{EVAL}</section>

    <section class="card"><h2>7日 上昇率 上位</h2>{GAINERS}</section>
    <section class="card"><h2>7日 下落率 上位</h2>{LOSERS}</section>
    <section class="card"><h2>時価総額 上位</h2>{TOPMCAP}</section>

    <section class="card span-2"><h2>最新ニュース</h2>{NEWSLIST}</section>

    <section class="card"><h2>データ蓄積状況</h2>
      {COVERAGE}
      <h2 style="margin-top:18px">直近の収集</h2>
      {LOGS}
    </section>
  </div>

  <footer>
    <strong>これは分析ツールであり、投資助言ではありません。</strong>
    表示されるスコアは統計モデルによる相対順位の推定値で、価格の上昇を示すものではありません。
    バックフィル期間のデータは「現在の上位500銘柄」に限られるため生存者バイアスがあります。
    手数料・スリッページは一切考慮していません。<br>
    生成 {NOW} (UTC) ・ データ元 CoinGecko / 各種ニュースRSS
  </footer>
</div>
"""
    body = (body
            .replace("{LATEST}", snapshot_label)
            .replace("{PILLS}", "".join(pills))
            .replace("{ALERT}", alert)
            .replace("{MARKET}", market)
            .replace("{HORIZON}", str(pm["horizon_days"]) if pm else "7")
            .replace("{PRED}", pred_block)
            .replace("{NEWSTOP}", news_top)
            .replace("{EVAL}", eval_block)
            .replace("{GAINERS}", table_block(d["gainers"], "7日", "pct_7d", fmt_pct, icons))
            .replace("{LOSERS}", table_block(d["losers"], "7日", "pct_7d", fmt_pct, icons))
            .replace("{TOPMCAP}", table_block(d["top_mcap"], "24h", "pct_24h", fmt_pct, icons))
            .replace("{NEWSLIST}", news_list)
            .replace("{COVERAGE}", coverage_note)
            .replace("{LOGS}", logs)
            .replace("{NOW}", now.strftime("%Y-%m-%d %H:%M")))

    # 置換漏れ（テンプレートの穴が埋まっていない）と、Python の生データが
    # そのまま HTML に流れ込んでいないかを検出する。前者は表示崩れ、
    # 後者は変数の取り違えのサインなので、黙って出力させない。
    leftovers = re.findall(r"\{[A-Z_]{3,}\}", body)
    if leftovers:
        raise RuntimeError("テンプレートの置換漏れ: %s" % sorted(set(leftovers)))
    if re.search(r"\{&#x27;\w+&#x27;:", body):
        raise RuntimeError("Python の辞書がそのまま HTML に出力されています"
                           "（変数の取り違えの可能性）")

    return ('<title>仮想通貨 分析ラボ</title>\n'
            '<link rel="icon" href="%s">\n' % FAVICON_URI +
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
            'family=Archivo:wght@600;700&'
            'family=Source+Sans+3:wght@400;500;600&display=swap">\n'
            '<style>%s</style>\n%s' % (STYLE, body))


def main() -> int:
    p = argparse.ArgumentParser(description="ダッシュボード HTML の生成")
    p.add_argument("--open", action="store_true", help="生成後にブラウザで開く")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    init_db()
    ensure_dirs()
    with connect() as conn:
        d = gather(conn)
    out = Path(args.out) if args.out else (REPORT_DIR / "dashboard.html")
    out.write_text(build_html(d), encoding="utf-8")
    LOG.info("ダッシュボードを生成しました: %s (%.0f KB)", out, out.stat().st_size / 1024)
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
