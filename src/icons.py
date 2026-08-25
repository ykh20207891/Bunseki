"""各通貨のアイコン（ロゴ画像）を取得してローカルに保存する。

画像 URL は /coins/markets のレスポンスに含まれているので、API を叩き直す必要はない。
すでに raw/ に保存してある生 JSON から拾える（--from-raw）。

画像本体は icons/ にファイルとして置く。ダッシュボードは Artifact として公開すると
外部ホストへの通信が CSP で遮断されるため、生成時に data URI として埋め込む必要がある。
そのためローカルに実体を持っておくことが必須になる。

使い方:
    python src/icons.py --from-raw     # 生JSONから image_url を DB に取り込む
    python src/icons.py                # 未取得のアイコンをダウンロード
    python src/icons.py --size thumb   # 25px 版（既定は small = 50px）
    python src/icons.py --refresh      # 取得済みも含めて取り直す
    python src/icons.py --status
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect import USER_AGENT, http_get_bytes  # noqa: E402
from common import RAW_DIR, ROOT, ensure_dirs, setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402

LOG = setup_logger("icons")

ICON_DIR = ROOT / "icons"
SIZES = ("thumb", "small", "large")   # 25px / 50px / 250px

# マジックバイトから種別を判定する。CoinGecko は PNG が多いが JPEG/WEBP/SVG も混ざる。
_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
]


def sniff(data: bytes) -> tuple[str, str]:
    for magic, mime, ext in _MAGIC:
        if data.startswith(magic):
            return mime, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    head = data[:200].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<svg"):
        return "image/svg+xml", ".svg"
    return "application/octet-stream", ".bin"


def resize_url(url: str, size: str) -> str:
    """CoinGecko の画像 URL は /large/ /small/ /thumb/ を差し替えるだけでサイズが変わる。"""
    return re.sub(r"/(thumb|small|large)/", "/%s/" % size, url, count=1)


def safe_name(coin_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", coin_id)[:120]


# ---------------------------------------------------------------- URL 取り込み
def import_urls_from_raw() -> int:
    """raw/*.json.gz から image_url を snapshots に取り込む（API 不要）。"""
    init_db()
    files = sorted(RAW_DIR.glob("coingecko_*.json.gz"))
    if not files:
        LOG.warning("raw/ に生JSONがありません。")
        return 1

    total = 0
    with connect() as conn:
        for f in files:
            date = f.stem.replace("coingecko_", "").replace(".json", "")
            try:
                payload = json.load(gzip.open(f, "rt", encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                LOG.warning("%s の読み込みに失敗: %s", f.name, e)
                continue
            rows = [(r.get("image"), date, r.get("id"))
                    for r in payload.get("markets", [])
                    if r.get("id") and r.get("image")]
            conn.executemany(
                "UPDATE snapshots SET image_url = ? WHERE snapshot_date = ? AND coin_id = ?",
                rows)
            total += len(rows)
            LOG.info("%s: %d 件の image_url を取り込み", f.name, len(rows))
        conn.commit()
    LOG.info("=== 合計 %d 件 ===", total)
    return 0


# ---------------------------------------------------------------- ダウンロード
def targets(conn, size: str, refresh: bool, limit: int | None):
    """未取得（または再取得対象）の (coin_id, symbol, url) を返す。"""
    sql = (
        "SELECT s.coin_id, s.symbol, s.image_url FROM snapshots s"
        " LEFT JOIN coin_icons i ON i.coin_id = s.coin_id"
        " WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM snapshots)"
        "   AND s.image_url IS NOT NULL AND s.image_url <> ''"
    )
    if not refresh:
        # サイズを変えた場合も取り直す
        sql += " AND (i.status IS NULL OR i.status <> 'ok' OR i.size <> '%s')" % size
    sql += " ORDER BY s.market_cap DESC"
    rows = conn.execute(sql).fetchall()
    out = [(r["coin_id"], r["symbol"] or r["coin_id"], r["image_url"]) for r in rows]
    return out[:limit] if limit else out


def download(size: str, refresh: bool, limit: int | None, sleep_sec: float) -> int:
    init_db()
    ensure_dirs()
    ICON_DIR.mkdir(parents=True, exist_ok=True)

    with connect() as conn:
        items = targets(conn, size, refresh, limit)
    if not items:
        LOG.info("取得対象はありません（すべて取得済み）。")
        return 0

    LOG.info("=== アイコン取得開始: %d 件 (size=%s / 間隔 %.2f 秒) ===",
             len(items), size, sleep_sec)
    headers = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*"}

    ok = fail = total_bytes = 0
    for i, (coin_id, symbol, url) in enumerate(items, 1):
        src = resize_url(url, size)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            data = http_get_bytes(src, headers, timeout=30, max_retry=3)
            if not data:
                raise RuntimeError("空のレスポンス")
            mime, ext = sniff(data)
            fname = safe_name(coin_id) + ext
            (ICON_DIR / fname).write_bytes(data)
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO coin_icons"
                    " (coin_id, symbol, source_url, filename, mime, bytes, size,"
                    "  fetched_at, status, message) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (coin_id, symbol, src, fname, mime, len(data), size, now, "ok", None))
                conn.commit()
            ok += 1
            total_bytes += len(data)
            if i % 25 == 0 or i == len(items):
                LOG.info("[%3d/%d] %-10s %s (%.1f KB)  累計 %.1f MB",
                         i, len(items), symbol, fname, len(data) / 1024,
                         total_bytes / 1024 / 1024)
        except Exception as e:
            fail += 1
            msg = "%s: %s" % (type(e).__name__, e)
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO coin_icons"
                    " (coin_id, symbol, source_url, filename, mime, bytes, size,"
                    "  fetched_at, status, message) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (coin_id, symbol, src, None, None, 0, size, now, "error", msg[:400]))
                conn.commit()
            LOG.warning("[%3d/%d] %-10s 失敗: %s", i, len(items), symbol, msg[:120])

        if i < len(items):
            time.sleep(sleep_sec)

    LOG.info("=== 完了: 成功 %d / 失敗 %d / 合計 %.1f MB ===", ok, fail, total_bytes / 1024 / 1024)
    return 0


# ---------------------------------------------------------------- 媒体アイコン
SOURCE_ICON_DIR = ICON_DIR / "_sources"


def fetch_source_icons(refresh: bool = False, sleep_sec: float = 0.4) -> int:
    """ニュース媒体のサイトアイコン（ファビコン）を取得する。

    各サイトの /favicon.ico は無かったりサイズがバラバラだったりするので、
    まず Google のファビコン配信を使い、駄目なら直接 /favicon.ico を試す。
    """
    from news import FEEDS

    init_db()
    SOURCE_ICON_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*"}

    with connect() as conn:
        done = {r["source"] for r in conn.execute(
            "SELECT source FROM source_icons WHERE status='ok'")} if not refresh else set()

    ok = fail = 0
    for source, spec in FEEDS.items():
        if source in done:
            continue
        site = spec[2] if isinstance(spec, tuple) and len(spec) > 2 else None
        if not site:
            continue
        domain = re.sub(r"^https?://", "", site).split("/")[0]
        candidates = [
            "https://www.google.com/s2/favicons?sz=64&domain=%s" % domain,
            "%s/favicon.ico" % site.rstrip("/"),
        ]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        saved = False
        for src in candidates:
            try:
                data = http_get_bytes(src, headers, timeout=25, max_retry=2)
                if len(data) < 60:            # 空/1x1 のダミーは弾く
                    raise RuntimeError("画像が小さすぎます (%d bytes)" % len(data))
                mime, ext = sniff(data)
                if mime == "application/octet-stream":
                    mime, ext = "image/x-icon", ".ico"
                # 媒体名が日本語だと safe_name で全部 "_" になり衝突するので、
                # 元の名前のハッシュを付けて一意にする。
                stem = safe_name(source).strip("_") or "src"
                digest = hashlib.md5(source.encode("utf-8")).hexdigest()[:6]
                fname = "%s_%s%s" % (stem, digest, ext)
                (SOURCE_ICON_DIR / fname).write_bytes(data)
                with connect() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO source_icons"
                        " (source, site_url, filename, mime, bytes, fetched_at, status, message)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (source, site, fname, mime, len(data), now, "ok", None))
                    conn.commit()
                LOG.info("%-16s %s (%.1f KB)", source, fname, len(data) / 1024)
                ok += 1
                saved = True
                break
            except Exception as e:
                last = str(e)[:150]
        if not saved:
            fail += 1
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO source_icons"
                    " (source, site_url, filename, mime, bytes, fetched_at, status, message)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (source, site, None, None, 0, now, "error", last))
                conn.commit()
            LOG.warning("%-16s 取得失敗: %s", source, last)
        time.sleep(sleep_sec)

    LOG.info("=== 媒体アイコン: 成功 %d / 失敗 %d ===", ok, fail)
    return 0


def load_source_icon_map() -> dict[str, str]:
    """媒体名 -> data URI。"""
    import base64
    out: dict[str, str] = {}
    with connect() as conn:
        rows = conn.execute(
            "SELECT source, filename, mime FROM source_icons WHERE status='ok'").fetchall()
    for r in rows:
        path = SOURCE_ICON_DIR / (r["filename"] or "")
        if not r["filename"] or not path.exists():
            continue
        try:
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            continue
        out[r["source"]] = "data:%s;base64,%s" % (r["mime"] or "image/png", b64)
    return out


# ---------------------------------------------------------------- 参照用 API
def load_icon_map(coin_ids, max_bytes: int = 24_000) -> dict[str, str]:
    """coin_id -> data URI の辞書を返す。ダッシュボード埋め込み用。

    max_bytes を超える画像は返さない（1銘柄で数百KBある SVG などを弾く）。
    """
    ids = [c for c in dict.fromkeys(coin_ids) if c]
    if not ids:
        return {}
    out: dict[str, str] = {}
    import base64
    with connect() as conn:
        qs = ",".join("?" * len(ids))
        rows = conn.execute(
            "SELECT coin_id, filename, mime, bytes FROM coin_icons"
            " WHERE status='ok' AND coin_id IN (%s)" % qs, ids).fetchall()
    for r in rows:
        if not r["filename"] or (r["bytes"] or 0) > max_bytes:
            continue
        path = ICON_DIR / r["filename"]
        if not path.exists():
            continue
        try:
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            continue
        out[r["coin_id"]] = "data:%s;base64,%s" % (r["mime"] or "image/png", b64)
    return out


def show_status() -> None:
    init_db()
    with connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, SUM(status='ok') ok, SUM(bytes) b,"
            " MIN(size) s0, MAX(size) s1 FROM coin_icons").fetchone()
        have_url = conn.execute(
            "SELECT COUNT(*) FROM snapshots"
            " WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM snapshots)"
            "   AND image_url IS NOT NULL").fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM snapshots"
            " WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM snapshots)").fetchone()[0]
        print("image_url を持つ銘柄 : %d / %d" % (have_url, total))
        print("アイコン取得済み     : %s / %s 件" % (r["ok"] or 0, r["n"] or 0))
        print("合計サイズ           : %.1f MB" % ((r["b"] or 0) / 1024 / 1024))
        size_label = r["s0"] if r["s0"] == r["s1"] else "%s〜%s" % (r["s0"], r["s1"])
        print("サイズ               : %s" % (size_label or "—"))
        errs = conn.execute(
            "SELECT symbol, message FROM coin_icons WHERE status<>'ok' LIMIT 10").fetchall()
        if errs:
            print("")
            print("失敗した銘柄:")
            for e in errs:
                print("  %-10s %s" % (e["symbol"], (e["message"] or "")[:80]))
    files = list(ICON_DIR.glob("*")) if ICON_DIR.exists() else []
    print("")
    print("icons/ のファイル数  : %d" % len(files))


def main() -> int:
    p = argparse.ArgumentParser(description="通貨アイコンの取得")
    p.add_argument("--from-raw", action="store_true",
                   help="raw/ の生JSONから image_url を DB に取り込む（API不要）")
    p.add_argument("--size", choices=SIZES, default="small", help="既定 small (50px)")
    p.add_argument("--refresh", action="store_true", help="取得済みも取り直す")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--sleep", type=float, default=0.3)
    p.add_argument("--sources", action="store_true",
                   help="ニュース媒体のサイトアイコンを取得する")
    p.add_argument("--status", action="store_true")
    args = p.parse_args()

    if args.status:
        show_status()
        return 0
    if args.from_raw:
        return import_urls_from_raw()
    if args.sources:
        return fetch_source_icons(args.refresh)
    # 引数なしのときは通貨アイコンと媒体アイコンの両方を揃える
    rc = download(args.size, args.refresh, args.limit, args.sleep)
    fetch_source_icons(args.refresh)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
