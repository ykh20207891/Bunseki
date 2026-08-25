"""CoinGecko の /coins/{id}/market_chart から過去の日次系列を一括取得する。

日次収集だけだと学習に足るデータが貯まるまで数ヶ月かかるので、まず過去365日分を
まとめて取って初期の学習・検証をできるようにする。

重要な注意（生存者バイアス）:
    バックフィル対象は「実行時点の上位500銘柄」である。過去の時点で上位にいたが
    その後消えた/落ちた銘柄は含まれない。したがってバックフィル期間の検証結果は
    実運用より楽観的に出る。日次収集で貯まる分にはこのバイアスは無い。

使い方:
    python src/backfill.py                 # 上位500銘柄 × 365日
    python src/backfill.py --limit 20      # まず20銘柄で試す
    python src/backfill.py --days 180
    python src/backfill.py --retry-errors  # 前回失敗した銘柄だけ再試行
    python src/backfill.py --status
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect import _base_and_headers, http_get_json  # noqa: E402
from common import load_config, setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402

LOG = setup_logger("backfill")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def target_coins(conn, limit: int | None, retry_errors: bool,
                 force: bool = False) -> list[tuple[str, str]]:
    """バックフィル対象の (coin_id, symbol) を返す。取得済みは既定でスキップする。"""
    latest = conn.execute("SELECT MAX(snapshot_date) FROM snapshots").fetchone()[0]
    if not latest:
        raise RuntimeError("snapshots が空です。先に python src/collect.py を実行してください。")

    if force:
        # PC を止めていて日次収集が抜けた期間を埋め直すとき用。取得済みでも再取得する。
        sql = ("SELECT coin_id, symbol FROM snapshots WHERE snapshot_date = ?"
               " ORDER BY market_cap DESC")
    elif retry_errors:
        sql = ("SELECT s.coin_id, s.symbol FROM snapshots s"
               " JOIN backfill_progress p ON p.coin_id = s.coin_id"
               " WHERE s.snapshot_date = ? AND p.status <> 'ok'"
               " ORDER BY s.market_cap DESC")
    else:
        sql = ("SELECT s.coin_id, s.symbol FROM snapshots s"
               " LEFT JOIN backfill_progress p ON p.coin_id = s.coin_id"
               " WHERE s.snapshot_date = ? AND (p.status IS NULL OR p.status <> 'ok')"
               " ORDER BY s.market_cap DESC")

    rows = conn.execute(sql, (latest,)).fetchall()
    coins = [(r["coin_id"], r["symbol"] or r["coin_id"]) for r in rows]
    return coins[:limit] if limit else coins


def parse_market_chart(payload: dict) -> list[tuple[str, float | None, float | None, float | None]]:
    """[[ts_ms, value], ...] 形式の3系列を日付キーでマージする。

    同じ日付に複数点がある場合は最初の点（= 00:00 UTC のスナップショット）を採用し、
    末尾にある「現在時刻の途中経過」の点で上書きしないようにする。
    """
    def to_map(series):
        out = {}
        for item in series or []:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            ts, val = item[0], item[1]
            try:
                d = datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                continue
            if d in out:          # 先勝ち
                continue
            try:
                f = float(val)
            except (TypeError, ValueError):
                continue
            out[d] = f if f == f and abs(f) != float("inf") else None
        return out

    prices = to_map(payload.get("prices"))
    caps = to_map(payload.get("market_caps"))
    vols = to_map(payload.get("total_volumes"))

    dates = sorted(set(prices) | set(caps) | set(vols))
    return [(d, prices.get(d), caps.get(d), vols.get(d)) for d in dates]


def fetch_one(cfg: dict, coin_id: str, days: int) -> list[tuple]:
    base, headers = _base_and_headers(cfg)
    url = ("%s/coins/%s/market_chart?vs_currency=%s&days=%d"
           % (base, coin_id, cfg["vs_currency"], days))
    payload = http_get_json(url, headers, int(cfg["request_timeout"]), int(cfg["max_retry"]))
    if not isinstance(payload, dict):
        raise RuntimeError("想定外のレスポンス: %s" % type(payload))
    return parse_market_chart(payload)


def save_history(conn, coin_id: str, rows: list[tuple]) -> int:
    records = [(coin_id, d, p, m, v) for (d, p, m, v) in rows]
    conn.executemany(
        "INSERT OR REPLACE INTO price_history (coin_id, date, price, market_cap, total_volume)"
        " VALUES (?,?,?,?,?)",
        records)
    return len(records)


def show_status() -> None:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT coin_id) AS coins, COUNT(*) AS rows,"
            " MIN(date) AS d0, MAX(date) AS d1 FROM price_history").fetchone()
        print("バックフィル済み銘柄 : %s" % row["coins"])
        print("総行数               : {:,}".format(row["rows"] or 0))
        print("期間                 : %s 〜 %s" % (row["d0"], row["d1"]))
        prog = conn.execute(
            "SELECT status, COUNT(*) AS n FROM backfill_progress GROUP BY status").fetchall()
        if prog:
            print("")
            print("進捗:")
            for r in prog:
                print("  %-6s %d 銘柄" % (r["status"], r["n"]))
        errs = conn.execute(
            "SELECT coin_id, message FROM backfill_progress WHERE status <> 'ok' LIMIT 10").fetchall()
        if errs:
            print("")
            print("失敗した銘柄（--retry-errors で再試行できます）:")
            for r in errs:
                print("  %-24s %s" % (r["coin_id"], (r["message"] or "")[:80]))


def run(days: int, limit: int | None, sleep_sec: float, retry_errors: bool,
        force: bool = False) -> int:
    cfg = load_config()
    init_db()

    with connect() as conn:
        coins = target_coins(conn, limit, retry_errors, force)

    if not coins:
        LOG.info("対象銘柄はありません（すべて取得済み）。")
        return 0

    LOG.info("=== バックフィル開始: %d 銘柄 × %d 日 (間隔 %.1f 秒 / 推定 %.0f 分) ===",
             len(coins), days, sleep_sec, len(coins) * sleep_sec / 60)

    ok = fail = total_rows = 0
    # 429 を食らったら間隔を広げ、順調なら少しずつ詰める（無料枠は制限が厳しい）
    cur_sleep = sleep_sec
    calm = 0
    for i, (coin_id, symbol) in enumerate(coins, 1):
        t0 = time.monotonic()
        try:
            rows = fetch_one(cfg, coin_id, days)
            with connect() as conn:
                n = save_history(conn, coin_id, rows)
                conn.execute(
                    "INSERT OR REPLACE INTO backfill_progress"
                    " (coin_id, done_at, days, rows, status, message) VALUES (?,?,?,?,?,?)",
                    (coin_id, _utc_now_iso(), days, n, "ok", None))
                conn.commit()
            ok += 1
            total_rows += n
            LOG.info("[%3d/%d] %-12s %-24s %4d 日分", i, len(coins), symbol, coin_id, n)
        except Exception as e:
            fail += 1
            msg = "%s: %s" % (type(e).__name__, e)
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO backfill_progress"
                    " (coin_id, done_at, days, rows, status, message) VALUES (?,?,?,?,?,?)",
                    (coin_id, _utc_now_iso(), days, 0, "error", msg[:500]))
                conn.commit()
            LOG.warning("[%3d/%d] %-12s %-24s 失敗: %s", i, len(coins), symbol, coin_id, msg[:160])

        # 所要時間が長い = 内部でリトライ待ちが発生している → 間隔を広げる
        elapsed = time.monotonic() - t0
        if elapsed > 15:
            calm = 0
            if cur_sleep < 30:
                cur_sleep = min(30.0, cur_sleep + 3.0)
                LOG.info("       レート制限を検知 -> 間隔を %.1f 秒に拡大", cur_sleep)
        else:
            calm += 1
            if calm >= 25 and cur_sleep > sleep_sec:
                cur_sleep = max(sleep_sec, cur_sleep - 1.0)
                calm = 0
                LOG.info("       順調 -> 間隔を %.1f 秒に短縮", cur_sleep)

        if i < len(coins):
            time.sleep(cur_sleep)

    LOG.info("=== バックフィル完了: 成功 %d / 失敗 %d / 計 %d 行 ===", ok, fail, total_rows)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="CoinGecko 過去日次データのバックフィル")
    p.add_argument("--days", type=int, default=365, help="遡る日数（無料枠は最大365）")
    p.add_argument("--limit", type=int, default=None, help="対象銘柄数の上限（お試し用）")
    p.add_argument("--sleep", type=float, default=6.0, help="リクエスト間隔（秒）")
    p.add_argument("--retry-errors", action="store_true", help="失敗した銘柄だけ再試行")
    p.add_argument("--force", action="store_true",
                   help="取得済みでも再取得する（PCを止めて日次収集が抜けた期間の穴埋め用）")
    p.add_argument("--status", action="store_true", help="進捗を表示するだけ")
    args = p.parse_args()

    if args.status:
        init_db()
        show_status()
        return 0
    return run(args.days, args.limit, args.sleep, args.retry_errors, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
