"""CoinGecko から時価総額上位 N 銘柄の日次スナップショットを収集して SQLite に蓄積する。

方針:
  - 標準ライブラリのみに依存する。毎日必ず動くことが最優先なので、
    pandas 等が壊れていても収集だけは止まらないようにしている。
  - 同じ日に複数回走らせても安全（PRIMARY KEY で上書き）。
  - 429 / 5xx はリトライ。全ページ取れなくても取れた分は必ず保存する。

使い方:
    python src/collect.py            # 今日(UTC)のスナップショットを取得
    python src/collect.py --status   # 蓄積状況を表示するだけ
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import RAW_DIR, ensure_dirs, load_config, setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402

LOG = setup_logger("collect")

PUBLIC_BASE = "https://api.coingecko.com/api/v3"
PRO_BASE = "https://pro-api.coingecko.com/api/v3"
USER_AGENT = "crypto-analysis-local/1.0 (personal research)"
PCT_WINDOWS = "1h,24h,7d,14d,30d,200d,1y"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _base_and_headers(cfg: dict) -> tuple[str, dict]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    key = (cfg.get("coingecko_api_key") or "").strip()
    if not key:
        return PUBLIC_BASE, headers
    if cfg.get("coingecko_api_plan") == "pro":
        headers["x-cg-pro-api-key"] = key
        return PRO_BASE, headers
    headers["x-cg-demo-api-key"] = key
    return PUBLIC_BASE, headers


# --- TLS 対策 -------------------------------------------------------------
# この PC では Avast の Web シールドが HTTPS を復号しており、その差し替え証明書を
# OpenSSL 3.5 が「Basic Constraints が critical でない」として拒否する。
# truststore を使うと Windows ネイティブの検証（Chrome と同じ経路）になり通る。
# それも駄目なら Windows 同梱の curl.exe（Schannel）に切り替える。
_SSL_CTX = None
_SSL_BACKEND = None
_USE_CURL = False


def build_ssl_context():
    global _SSL_CTX, _SSL_BACKEND
    if _SSL_CTX is not None:
        return _SSL_CTX
    try:
        import truststore  # type: ignore
        _SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        _SSL_BACKEND = "truststore(OS native)"
    except Exception:
        try:
            import certifi  # type: ignore
            _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
            _SSL_BACKEND = "certifi"
        except Exception:
            _SSL_CTX = ssl.create_default_context()
            _SSL_BACKEND = "system default"
    LOG.info("TLS 検証バックエンド: %s", _SSL_BACKEND)
    return _SSL_CTX


def _find_curl():
    for cand in (r"C:\Windows\System32\curl.exe", "curl"):
        path = shutil.which(cand) or (cand if Path(cand).exists() else None)
        if path:
            return path
    return None


def _curl_get_bytes(url: str, headers: dict, timeout: int) -> bytes:
    """urllib が TLS で失敗する環境向けのフォールバック（Schannel を使う curl）。"""
    curl = _find_curl()
    if not curl:
        raise RuntimeError("curl.exe が見つからないためフォールバックできません")
    cmd = [curl, "-sS", "--fail", "--location", "--max-time", str(timeout)]
    for k, v in headers.items():
        cmd += ["-H", "%s: %s" % (k, v)]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 30)
    if proc.returncode != 0:
        raise RuntimeError("curl 失敗 (rc=%d): %s"
                           % (proc.returncode, proc.stderr.decode("utf-8", "replace")[:300]))
    return proc.stdout


def http_get_bytes(url: str, headers: dict, timeout: int, max_retry: int) -> bytes:
    """GET して生バイト列を返す。429/5xx は指数バックオフでリトライ。"""
    global _USE_CURL
    ctx = build_ssl_context()
    delay = 5.0
    last_err = None
    for attempt in range(1, max_retry + 1):
        if _USE_CURL:
            try:
                return _curl_get_bytes(url, headers, timeout)
            except Exception as e:
                last_err = e
                LOG.warning("curl 経由で失敗 (attempt %d/%d): %s -> %.0f秒待って再試行",
                            attempt, max_retry, e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except ssl.SSLError as e:
            last_err = e
            LOG.warning("TLS 検証に失敗しました (%s)。curl フォールバックに切り替えます。", e)
            _USE_CURL = True
            continue
        except urllib.error.HTTPError as e:
            last_err = e
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if e.code in (429, 500, 502, 503, 504):
                wait = float(retry_after) if (retry_after or "").isdigit() else delay
                LOG.warning("HTTP %s (attempt %d/%d) -> %.0f秒待って再試行",
                            e.code, attempt, max_retry, wait)
                time.sleep(wait)
                delay = min(delay * 2, 120)
                continue
            LOG.error("HTTP %s: %s", e.code, url)
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            # urllib は SSL エラーを URLError で包むので reason を見る
            reason = getattr(e, "reason", None)
            if isinstance(reason, ssl.SSLError) and not _USE_CURL:
                LOG.warning("TLS 検証に失敗しました (%s)。curl フォールバックに切り替えます。", reason)
                _USE_CURL = True
                continue
            LOG.warning("通信エラー (attempt %d/%d): %s -> %.0f秒待って再試行",
                        attempt, max_retry, e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 120)
    raise RuntimeError("リトライ上限に到達しました: %s (%s)" % (url, last_err))


def http_get_text(url: str, headers: dict, timeout: int, max_retry: int) -> str:
    """GET して本文テキストを返す。"""
    return http_get_bytes(url, headers, timeout, max_retry).decode("utf-8", "replace")


def http_get_json(url: str, headers: dict, timeout: int, max_retry: int):
    """GET して JSON を返す。"""
    text = http_get_text(url, headers, timeout, max_retry)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError("JSON として解析できませんでした (%s): %s" % (e, text[:200]))


def fetch_markets(cfg: dict) -> list[dict]:
    """上位 top_n 銘柄を per_page ずつページングして取得する。"""
    base, headers = _base_and_headers(cfg)
    per_page = int(cfg["per_page"])
    top_n = int(cfg["top_n"])
    pages = (top_n + per_page - 1) // per_page
    rows: list[dict] = []

    for page in range(1, pages + 1):
        params = {
            "vs_currency": cfg["vs_currency"],
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
            "sparkline": "false",
            "price_change_percentage": PCT_WINDOWS,
            "locale": "en",
        }
        url = base + "/coins/markets?" + urllib.parse.urlencode(params)
        LOG.info("取得中: page %d/%d (per_page=%d)", page, pages, per_page)
        data = http_get_json(url, headers, int(cfg["request_timeout"]), int(cfg["max_retry"]))
        if not isinstance(data, list):
            raise RuntimeError("想定外のレスポンス形式: %s" % type(data))
        rows.extend(data)
        LOG.info("  -> %d 件取得（累計 %d 件）", len(data), len(rows))
        if len(data) < per_page:
            LOG.info("  -> 最終ページに到達")
            break
        if page < pages:
            time.sleep(float(cfg["sleep_between_pages"]))

    return rows[:top_n]


def fetch_global(cfg: dict):
    base, headers = _base_and_headers(cfg)
    try:
        data = http_get_json(base + "/global", headers,
                             int(cfg["request_timeout"]), int(cfg["max_retry"]))
        return data.get("data") if isinstance(data, dict) else None
    except Exception as e:  # 市場全体データは無くても致命傷ではない
        LOG.warning("global データの取得に失敗（スキップします）: %s", e)
        return None


def _f(row: dict, key: str):
    """数値フィールドを安全に float 化する。NaN/Inf/変換不能は None。"""
    v = row.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or abs(f) == float("inf"):
        return None
    return f


INSERT_SNAPSHOT = (
    "INSERT OR REPLACE INTO snapshots ("
    "snapshot_date, fetched_at, source, coin_id, symbol, name, mcap_rank,"
    "price, market_cap, fdv, total_volume, high_24h, low_24h,"
    "pct_1h, pct_24h, pct_7d, pct_14d, pct_30d, pct_200d, pct_1y,"
    "circulating_supply, total_supply, max_supply,"
    "ath, ath_change_pct, ath_date, atl, atl_change_pct, atl_date, image_url"
    ") VALUES (" + ",".join(["?"] * 30) + ")"
)


def save_markets(conn, snapshot_date: str, fetched_at: str, rows: list[dict]) -> int:
    # 同じ日に再実行したとき、その間に上位500から外れた銘柄の行が残ると
    # 「その日の上位500」というスナップショットの意味が崩れるので、一度消して入れ直す。
    # 取得が失敗した場合はここまで来ないので、消して空になる心配はない。
    conn.execute("DELETE FROM snapshots WHERE snapshot_date = ?", (snapshot_date,))

    records = []
    for r in rows:
        if not r.get("id"):
            continue
        records.append((
            snapshot_date, fetched_at, "coingecko",
            r.get("id"),
            (r.get("symbol") or "").upper() or None,
            r.get("name"),
            r.get("market_cap_rank"),
            _f(r, "current_price"),
            _f(r, "market_cap"),
            _f(r, "fully_diluted_valuation"),
            _f(r, "total_volume"),
            _f(r, "high_24h"),
            _f(r, "low_24h"),
            _f(r, "price_change_percentage_1h_in_currency"),
            _f(r, "price_change_percentage_24h_in_currency"),
            _f(r, "price_change_percentage_7d_in_currency"),
            _f(r, "price_change_percentage_14d_in_currency"),
            _f(r, "price_change_percentage_30d_in_currency"),
            _f(r, "price_change_percentage_200d_in_currency"),
            _f(r, "price_change_percentage_1y_in_currency"),
            _f(r, "circulating_supply"),
            _f(r, "total_supply"),
            _f(r, "max_supply"),
            _f(r, "ath"),
            _f(r, "ath_change_percentage"),
            r.get("ath_date"),
            _f(r, "atl"),
            _f(r, "atl_change_percentage"),
            r.get("atl_date"),
            r.get("image"),
        ))

    conn.executemany(INSERT_SNAPSHOT, records)
    return len(records)


def save_global(conn, snapshot_date: str, fetched_at: str, g) -> None:
    if not g:
        return
    mcap = (g.get("total_market_cap") or {}).get("usd")
    vol = (g.get("total_volume") or {}).get("usd")
    dom = g.get("market_cap_percentage") or {}
    conn.execute(
        "INSERT OR REPLACE INTO global_snapshots ("
        "snapshot_date, fetched_at, total_market_cap, total_volume,"
        "btc_dominance, eth_dominance, active_cryptos, mcap_change_24h_pct"
        ") VALUES (?,?,?,?,?,?,?,?)",
        (snapshot_date, fetched_at, mcap, vol, dom.get("btc"), dom.get("eth"),
         g.get("active_cryptocurrencies"),
         g.get("market_cap_change_percentage_24h_usd")),
    )


def save_raw(snapshot_date: str, markets: list[dict], g) -> None:
    ensure_dirs()
    path = RAW_DIR / ("coingecko_%s.json.gz" % snapshot_date)
    payload = {"snapshot_date": snapshot_date, "markets": markets, "global": g}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    LOG.info("生JSONを保存: %s (%.1f KB)", path.name, path.stat().st_size / 1024)


def show_status() -> None:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT snapshot_date) AS days,"
            " MIN(snapshot_date) AS first_day,"
            " MAX(snapshot_date) AS last_day,"
            " COUNT(*) AS rows FROM snapshots"
        ).fetchone()
        print("蓄積日数     : %s 日" % row["days"])
        print("期間         : %s 〜 %s" % (row["first_day"], row["last_day"]))
        print("総レコード数 : {:,} 行".format(row["rows"]))
        if row["days"]:
            recent = conn.execute(
                "SELECT snapshot_date, COUNT(*) AS n FROM snapshots"
                " GROUP BY snapshot_date ORDER BY snapshot_date DESC LIMIT 10"
            ).fetchall()
            print("")
            print("直近の取得状況:")
            for r in recent:
                print("  %s  %4d 銘柄" % (r["snapshot_date"], r["n"]))
            fails = conn.execute(
                "SELECT snapshot_date, status, message FROM collect_log"
                " WHERE status NOT IN ('ok') ORDER BY id DESC LIMIT 5"
            ).fetchall()
            if fails:
                print("")
                print("直近の失敗/部分取得:")
                for r in fails:
                    print("  %s  [%s] %s" % (r["snapshot_date"], r["status"], r["message"]))


def run_collect() -> int:
    cfg = load_config()
    init_db()
    now = _utc_now()
    snapshot_date = now.strftime("%Y-%m-%d")
    fetched_at = now.isoformat(timespec="seconds")

    LOG.info("=== 収集開始 snapshot_date=%s (UTC) top_n=%s ===", snapshot_date, cfg["top_n"])

    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO collect_log (snapshot_date, started_at, status) VALUES (?,?,?)",
            (snapshot_date, fetched_at, "running"))
        log_id = cur.lastrowid
        conn.commit()

    status, written, message = "error", 0, ""
    try:
        markets = fetch_markets(cfg)
        g = fetch_global(cfg)
        with connect() as conn:
            written = save_markets(conn, snapshot_date, fetched_at, markets)
            save_global(conn, snapshot_date, fetched_at, g)
            conn.commit()
        if cfg.get("keep_raw_json"):
            save_raw(snapshot_date, markets, g)

        expected = int(cfg["top_n"])
        status = "ok" if written >= expected * 0.9 else "partial"
        message = "%d/%d 銘柄を保存" % (written, expected)
        LOG.info("=== 収集完了 status=%s %s ===", status, message)
    except Exception as e:
        message = "%s: %s" % (type(e).__name__, e)
        LOG.exception("収集に失敗しました")
    finally:
        with connect() as conn:
            conn.execute(
                "UPDATE collect_log SET finished_at=?, status=?, rows_written=?, message=?"
                " WHERE id=?",
                (_utc_now().isoformat(timespec="seconds"), status, written, message, log_id))
            conn.commit()

    return 0 if status in ("ok", "partial") else 1


def main() -> int:
    p = argparse.ArgumentParser(description="CoinGecko 日次スナップショット収集")
    p.add_argument("--status", action="store_true", help="蓄積状況だけ表示する")
    args = p.parse_args()
    if args.status:
        init_db()
        show_status()
        return 0
    return run_collect()


if __name__ == "__main__":
    raise SystemExit(main())
