"""英語ニュース見出しの日本語訳。

日次バッチは無人で走るので自動翻訳が必要。MyMemory の無料 API を使う
（API キー不要・匿名で 1日 5,000 文字まで）。枠が小さいので、
ダッシュボードに実際に表示する見出しだけを訳し、結果は DB に保存して二度と訳さない。

機械翻訳はそのままだと暗号資産の文脈で硬い/誤った訳になりやすいので
（"rally" が「ラリー」、"whale" が「クジラ」など）、業界語は後処理で置き換える。

使い方:
    python src/translate.py            # 未翻訳の見出しを訳す（既定30件まで）
    python src/translate.py --limit 60
    python src/translate.py --status
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect import USER_AGENT, http_get_text  # noqa: E402
from common import setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402

LOG = setup_logger("translate")

API = "https://api.mymemory.translated.net/get"
DAILY_CHAR_BUDGET = 4500      # 匿名枠 5,000 文字に対して余裕を持たせる

# 機械翻訳が暗号資産の文脈でおかしくなる語の後処理。左を右へ置き換える。
GLOSSARY = [
    ("ラリー", "上昇"),
    ("集会", "上昇"),
    ("クジラ", "大口保有者"),
    ("強気の", "強気の"),
    ("ブルラン", "強気相場"),
    ("マイニング業者", "マイナー"),
    ("鉱夫", "マイナー"),
    ("採掘者", "マイナー"),
    ("財務会社", "財務戦略企業"),
    ("国庫会社", "財務戦略企業"),
    ("暗号通貨", "暗号資産"),
    ("仮想通貨株", "暗号資産関連株"),
    ("ステーブルコイン", "ステーブルコイン"),
    ("ハッキングされた", "ハッキング被害"),
    ("搾取", "脆弱性を突いた攻撃"),
    ("エクスプロイト", "脆弱性攻撃"),
    ("ラグプル", "持ち逃げ"),
    ("ホードル", "長期保有"),
    ("ジャンプ", "急伸"),
    ("スワーム", "殺到"),
    ("ファイリング", "申請"),
    ("スポットライト", "注目"),
    ("賭けています", "見込み"),
    ("説明しています", "公表"),
    ("指摘しています", "指摘"),
    ("記録しました", "記録"),
    ("ドルを失う", "ドルを下回る"),
    ("を失う", "を下回る"),
    ("スパイク", "急騰"),
    ("プランジ", "急落"),
    ("ダンプ", "投げ売り"),
    ("流入", "資金流入"),
    ("流出", "資金流出"),
    ("あなたは", ""),
    ("します。", "。"),
]


def polish(text: str) -> str:
    """機械翻訳の出力を読める日本語に整える。"""
    if not text:
        return text
    for a, b in GLOSSARY:
        text = text.replace(a, b)

    # 通貨表記の崩れを直す。"$ 80 K" のように空白が入って出てくることが多い。
    # 単位の直後は日本語が続くことが多く、\b は「K」と「に」の間を境界と見なさないため
    # （日本語も単語構成文字扱い）、英字が続かないことを明示的に確認する。
    text = re.sub(r"\$\s+", "$", text)
    text = re.sub(r"(\d)\s+([KkMmBb])(?![A-Za-z])", r"\1\2", text)
    # 「12億$」のように和数詞を挟んで $ が後置される形も直す
    text = re.sub(r"(\d+(?:[.,]\d+)?[万億兆]?)\s*\$", r"\1ドル", text)
    # 桁の換算（$80K → 8万ドル）は誤変換の危険があるのでやらず、単位だけ日本語にする
    text = re.sub(
        r"\$(\d+(?:[.,]\d+)?)\s*([KkMmBb])?(?![A-Za-z])",
        lambda m: "%s%sドル" % (
            m.group(1),
            {"k": "千", "m": "百万", "b": "十億"}.get((m.group(2) or "").lower(), "")),
        text)

    # 数字と日本語の間の不要な空白を詰める
    text = re.sub(r"(?<=[0-9])\s+(?=[ぁ-んァ-ヴ一-龥])", "", text)
    text = re.sub(r"(?<=[ぁ-んァ-ヴ一-龥])\s+(?=[0-9])", "", text)

    # 末尾の句点だけ落とす。語尾（〜します等）の一括削除は
    # 「戻します」→「戻」のように動詞を壊すのでやらない。
    text = text.strip().rstrip("。").strip()
    while "  " in text:
        text = text.replace("  ", " ")
    return text.strip("、 ").strip()


def translate_one(text: str, timeout: int = 30) -> str | None:
    url = API + "?" + urllib.parse.urlencode({"q": text[:480], "langpair": "en|ja"})
    payload = json.loads(http_get_text(url, {"User-Agent": USER_AGENT}, timeout, 2))
    if str(payload.get("responseStatus")) != "200":
        raise RuntimeError("API エラー: %s %s"
                           % (payload.get("responseStatus"), payload.get("responseDetails")))
    if payload.get("quotaFinished"):
        raise RuntimeError("QUOTA")
    out = (payload.get("responseData") or {}).get("translatedText") or ""
    # 訳せなかったとき原文をそのまま返してくることがある
    if not out or out.strip().lower() == text.strip().lower():
        return None
    return polish(out)


def pending(conn, limit: int):
    """翻訳が必要な記事を、新しい順に返す（表示されるのは新しい記事なので）。"""
    return conn.execute(
        "SELECT id, title FROM news_articles"
        " WHERE (lang IS NULL OR lang = 'en') AND (title_ja IS NULL OR title_ja = '')"
        " ORDER BY published_at DESC LIMIT ?", (limit,)).fetchall()


def run(limit: int, sleep_sec: float) -> int:
    init_db()
    with connect() as conn:
        rows = pending(conn, limit)
    if not rows:
        LOG.info("翻訳が必要な見出しはありません。")
        return 0

    LOG.info("=== 翻訳開始: %d 件 ===", len(rows))
    used = ok = fail = 0
    for i, r in enumerate(rows, 1):
        title = (r["title"] or "").strip()
        if not title:
            continue
        if used + len(title) > DAILY_CHAR_BUDGET:
            LOG.info("1日の文字数枠に達したため中断します（%d 文字使用）。残りは明日訳します。", used)
            break
        try:
            ja = translate_one(title)
            used += len(title)
            if ja:
                with connect() as conn:
                    conn.execute(
                        "UPDATE news_articles SET title_ja = ?, lang = 'en' WHERE id = ?",
                        (ja, r["id"]))
                    conn.commit()
                ok += 1
                if i <= 3 or i % 10 == 0:
                    LOG.info("[%2d/%d] %s", i, len(rows), ja[:60])
            else:
                fail += 1
        except Exception as e:
            if "QUOTA" in str(e):
                LOG.warning("翻訳APIの無料枠を使い切りました。明日また実行してください。")
                break
            fail += 1
            LOG.warning("[%2d/%d] 失敗: %s", i, len(rows), str(e)[:100])
        time.sleep(sleep_sec)

    LOG.info("=== 完了: 成功 %d / 失敗 %d / %d 文字使用 ===", ok, fail, used)
    return 0


def repolish_all() -> int:
    """用語辞書や整形規則を直したとき、保存済みの訳文にもう一度かけ直す。

    翻訳APIは呼ばないので枠を消費しない。
    """
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title_ja FROM news_articles"
            " WHERE title_ja IS NOT NULL AND title_ja <> '' AND lang <> 'ja'").fetchall()
        changed = 0
        for r in rows:
            new = polish(r["title_ja"])
            if new and new != r["title_ja"]:
                conn.execute("UPDATE news_articles SET title_ja = ? WHERE id = ?",
                             (new, r["id"]))
                changed += 1
        conn.commit()
    LOG.info("再整形: %d / %d 件を更新", changed, len(rows))
    return 0


def show_status() -> None:
    init_db()
    with connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n,"
            " SUM(CASE WHEN lang='ja' THEN 1 ELSE 0 END) ja,"
            " SUM(CASE WHEN title_ja IS NOT NULL AND title_ja<>'' THEN 1 ELSE 0 END) has_ja"
            " FROM news_articles").fetchone()
        print("記事数           : %s" % (r["n"] or 0))
        print("日本語media の記事: %s" % (r["ja"] or 0))
        print("日本語見出しあり : %s" % (r["has_ja"] or 0))
        print("未翻訳           : %s" % ((r["n"] or 0) - (r["has_ja"] or 0)))


def main() -> int:
    p = argparse.ArgumentParser(description="英語見出しの日本語訳")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--sleep", type=float, default=1.2)
    p.add_argument("--repolish", action="store_true",
                   help="保存済みの訳文に整形規則をかけ直す（API は呼ばない）")
    p.add_argument("--status", action="store_true")
    args = p.parse_args()
    if args.status:
        show_status()
        return 0
    if args.repolish:
        return repolish_all()
    return run(args.limit, args.sleep)


if __name__ == "__main__":
    raise SystemExit(main())
