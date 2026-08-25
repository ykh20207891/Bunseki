"""GDELT から過去のニュース量・トーンをバックフィルする。

RSS は「今この瞬間の最新記事」しか返さないので、過去のニュースは取得できない。
一方 GDELT Project は全世界のニュースを 2015 年から索引しており、
キーワードごとの「日次の報道量」と「平均トーン（感情）」を無料 API で遡って取れる。
これが過去ニュースを得られる現実的にほぼ唯一の無料手段。

制約:
  - レート制限が厳しい（公称 5秒に1回、実際はもっと保守的に叩く必要がある）。
    既定 15 秒間隔。バーストすると数十分〜数時間ブロックされるので焦らないこと。
  - キーワード検索なので銘柄名の曖昧さが誤検出になる。
    "crypto OR cryptocurrency OR blockchain" を AND 条件に足して絞り込んでいる。
  - 記事単位ではなく日次集計のみ。個別記事の見出しは取れない。

使い方:
    python src/news_backfill.py --limit 100          # 上位100銘柄
    python src/news_backfill.py --sleep 20           # もっと安全側に
    python src/news_backfill.py --retry-errors
    python src/news_backfill.py --status
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect import USER_AGENT, http_get_text  # noqa: E402
from common import setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402
from news import _COMMON_WORDS, _SYMBOL_STOPWORDS  # noqa: E402

LOG = setup_logger("news_backfill")

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
CRYPTO_CONTEXT = "(crypto OR cryptocurrency OR blockchain)"
# GDELT はレート制限時に「本文の先頭に HTTP ヘッダ文字列が入った HTML」を返してくる。
# JSON 以外が返ってきたら基本的にレート制限とみなして長く休む。
RATE_LIMIT_MARKERS = ("limit requests", "GDELT API Server", "Content-type:", "<html")


def build_query(name: str, symbol: str) -> str:
    """銘柄名から GDELT 用の検索式を組み立てる。

    名前が一般英単語と衝突する銘柄は、暗号資産文脈の語との AND で強く絞る。
    それでも取り切れない誤検出は残るので、絶対量ではなく変化量を特徴量に使うこと。
    """
    low = name.lower()
    generic = (" " not in low and low in _COMMON_WORDS) or len(low) <= 3
    if generic and symbol not in _SYMBOL_STOPWORDS:
        term = '("%s" OR "%s")' % (name, symbol)
    else:
        term = '"%s"' % name
    return "%s %s sourcelang:english" % (term, CRYPTO_CONTEXT)


def parse_timeline(payload: dict) -> dict[str, dict[str, float]]:
    """GDELT の timeline レスポンスを {系列名: {日付: 値}} に整形する。"""
    out: dict[str, dict[str, float]] = {}
    for s in payload.get("timeline", []) or []:
        name = s.get("series") or "?"
        vals: dict[str, float] = {}
        for pt in s.get("data", []) or []:
            raw = str(pt.get("date", ""))
            try:
                d = datetime.strptime(raw[:8], "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                continue
            try:
                vals[d] = float(pt.get("value"))
            except (TypeError, ValueError):
                continue
        out[name] = vals
    return out


def fetch_coin(name: str, symbol: str, days: int, timeout: int = 120) -> list[tuple]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    params = {
        "query": build_query(name, symbol),
        "mode": "timelinevoltone",
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        "format": "json",
    }
    url = GDELT_URL + "?" + urllib.parse.urlencode(params)
    try:
        text = http_get_text(url, {"User-Agent": USER_AGENT}, timeout, max_retry=1)
    except Exception as e:
        if "429" in str(e):
            raise RuntimeError("RATE_LIMIT")
        raise

    head = text[:400]
    if not text.lstrip().startswith("{") or any(m in head for m in RATE_LIMIT_MARKERS):
        raise RuntimeError("RATE_LIMIT")

    series = parse_timeline(json.loads(text))
    vol = next((v for k, v in series.items() if "volume" in k.lower()), {})
    tone = next((v for k, v in series.items() if "tone" in k.lower()), {})
    dates = sorted(set(vol) | set(tone))
    return [(d, vol.get(d), tone.get(d)) for d in dates]


def target_coins(conn, limit: int | None, retry_errors: bool) -> list[tuple[str, str, str]]:
    latest = conn.execute("SELECT MAX(snapshot_date) FROM snapshots").fetchone()[0]
    if not latest:
        raise RuntimeError("snapshots が空です。先に collect.py を実行してください。")
    cond = ("p.status <> 'ok'" if retry_errors else "(p.status IS NULL OR p.status <> 'ok')")
    rows = conn.execute(
        "SELECT s.coin_id, s.symbol, s.name FROM snapshots s"
        " LEFT JOIN news_backfill_progress p ON p.coin_id = s.coin_id"
        " WHERE s.snapshot_date = ? AND s.name IS NOT NULL AND " + cond +
        " ORDER BY s.market_cap DESC", (latest,)).fetchall()
    coins = [(r["coin_id"], (r["symbol"] or "").upper(), r["name"]) for r in rows]
    return coins[:limit] if limit else coins


def show_status() -> None:
    with connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT coin_id) c, MIN(date) d0, MAX(date) d1"
            " FROM news_daily WHERE source='gdelt'").fetchone()
        print("GDELT 行数 : {:,}".format(r["n"] or 0))
        print("銘柄数     : %s" % (r["c"] or 0))
        print("期間       : %s 〜 %s" % (r["d0"], r["d1"]))
        prog = conn.execute("SELECT status, COUNT(*) n FROM news_backfill_progress"
                            " GROUP BY status").fetchall()
        if prog:
            print("")
            print("進捗:")
            for x in prog:
                print("  %-6s %d 銘柄" % (x["status"], x["n"]))


def run(days: int, limit: int | None, sleep_sec: float, retry_errors: bool) -> int:
    init_db()
    with connect() as conn:
        coins = target_coins(conn, limit, retry_errors)
    if not coins:
        LOG.info("対象銘柄はありません（すべて取得済み）。")
        return 0

    LOG.info("=== GDELT バックフィル開始: %d 銘柄 × %d 日 (間隔 %.0f 秒 / 推定 %.0f 分) ===",
             len(coins), days, sleep_sec, len(coins) * sleep_sec / 60)

    ok = fail = rows_total = 0
    cur_sleep = sleep_sec
    consecutive_limits = 0

    for i, (coin_id, symbol, name) in enumerate(coins, 1):
        try:
            rows = fetch_coin(name, symbol, days)
            with connect() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO news_daily"
                    " (coin_id, date, source, article_count, tone) VALUES (?,?,'gdelt',?,?)",
                    [(coin_id, d, v, t) for (d, v, t) in rows])
                conn.execute(
                    "INSERT OR REPLACE INTO news_backfill_progress"
                    " (coin_id, done_at, rows, status, message) VALUES (?,?,?,?,?)",
                    (coin_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     len(rows), "ok", None))
                conn.commit()
            ok += 1
            rows_total += len(rows)
            consecutive_limits = 0
            LOG.info("[%3d/%d] %-10s %-24s %4d 日分", i, len(coins), symbol, coin_id, len(rows))
        except Exception as e:
            msg = str(e)
            if "RATE_LIMIT" in msg:
                consecutive_limits += 1
                cur_sleep = min(120.0, cur_sleep * 1.5)
                wait = 60 * consecutive_limits
                LOG.warning("[%3d/%d] %-10s レート制限 -> %d 秒休止し、間隔を %.0f 秒に拡大",
                            i, len(coins), symbol, wait, cur_sleep)
                time.sleep(wait)
                if consecutive_limits >= 5:
                    LOG.error("レート制限が続くため中断します。時間をおいて再実行してください"
                              "（進捗は保存済みなので続きから再開できます）。")
                    break
                continue      # この銘柄は未処理のまま次回に回す
            fail += 1
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO news_backfill_progress"
                    " (coin_id, done_at, rows, status, message) VALUES (?,?,?,?,?)",
                    (coin_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     0, "error", msg[:500]))
                conn.commit()
            LOG.warning("[%3d/%d] %-10s 失敗: %s", i, len(coins), symbol, msg[:140])

        if i < len(coins):
            time.sleep(cur_sleep)

    LOG.info("=== 完了: 成功 %d / 失敗 %d / 計 %d 行 ===", ok, fail, rows_total)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="GDELT による過去ニュースのバックフィル")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--limit", type=int, default=100,
                   help="対象銘柄数（既定100。小型銘柄はニュース自体が無いので上位に絞る）")
    p.add_argument("--sleep", type=float, default=15.0)
    p.add_argument("--retry-errors", action="store_true")
    p.add_argument("--status", action="store_true")
    args = p.parse_args()
    if args.status:
        init_db()
        show_status()
        return 0
    return run(args.days, args.limit, args.sleep, args.retry_errors)


if __name__ == "__main__":
    raise SystemExit(main())
