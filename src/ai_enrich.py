"""Workers AI で既存のニュース処理を補強する。

方針:
  * 既存の辞書/正規表現ベースの結果は消さない。AI は「上乗せ」として扱う。
  * AI が使えない・失敗した場合は何もしない（従来動作のまま）。
  * 対象は既存マッチが付いた記事だけ。全銘柄に投げると無駄が多い。

やること:
  1. 銘柄マッチの検証   … 'Just'/'Cash' のような一般語との誤検出をAIが棄却する
  2. センチメント再判定 … 辞書では扱えない否定表現("not bullish")を文脈で読む

結果は news_articles / news_mentions を壊さないよう別カラムに入れる。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai  # noqa: E402
from common import setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402

LOG = setup_logger("ai_enrich")

# 1回のバッチで処理する上限（無料枠と実行時間を守るため）
DEFAULT_LIMIT = 60

MATCH_SYSTEM = (
    "あなたは暗号資産ニュースの分類器です。"
    "記事の見出しと要約を読み、指定された銘柄が『その記事の話題として実際に言及されているか』"
    "を判定します。単に一般的な英単語が一致しただけの誤検出を棄却するのが目的です。"
    '出力は {"relevant": ["SYMBOL", ...]} の形式のみ。'
)

SENTIMENT_SYSTEM = (
    "あなたは暗号資産ニュースのセンチメント判定器です。"
    "見出しと要約から、その銘柄の価格にとって強気か弱気かを -1.0(強い弱気) 〜 +1.0(強い強気) で評価します。"
    "'not bullish' のような否定表現を正しく解釈してください。中立は 0。"
    '出力は {"score": 数値} の形式のみ。'
)


SUMMARY_SYSTEM = (
    "あなたは暗号資産市場の解説者です。"
    "渡されたニュース見出し全体を俯瞰し、その日の市場の要点を日本語でまとめます。\n"
    "厳守事項:\n"
    "1. 出力はちょうど3行。4行以上は禁止。\n"
    "2. 各行は50字以内。\n"
    "3. 個別ニュースの羅列ではなく、全体の傾向としてまとめる。\n"
    "4. 値上がり/値下がりの予想や投資判断は書かない（事実の要約のみ）。\n"
    "5. 箇条書き記号や見出しは付けない。"
)

MAX_SUMMARY_LINES = 3


def _ensure_summary_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_daily_summary (
            summary_date TEXT PRIMARY KEY,
            summary      TEXT NOT NULL,
            article_count INTEGER,
            created_at   TEXT NOT NULL
        )
        """
    )
    conn.commit()


def make_daily_summary(conn, force: bool = False) -> str | None:
    """その日のニュース見出しから市場サマリーを作る。"""
    _ensure_summary_table(conn)

    row = conn.execute(
        "SELECT MAX(pub_date) FROM news_articles WHERE pub_date IS NOT NULL"
    ).fetchone()
    target = row[0] if row else None
    if not target:
        return None

    if not force:
        done = conn.execute(
            "SELECT summary FROM ai_daily_summary WHERE summary_date = ?", (target,)
        ).fetchone()
        if done:
            return done[0]

    titles = [
        r[0]
        for r in conn.execute(
            "SELECT COALESCE(title_ja, title) FROM news_articles "
            "WHERE pub_date = ? AND COALESCE(title_ja, title) IS NOT NULL "
            "ORDER BY published_at DESC LIMIT 25",
            (target,),
        )
    ]
    if not titles:
        return None

    text = ai.chat(
        SUMMARY_SYSTEM,
        f"{target} の暗号資産ニュース見出し:\n" + "\n".join(f"- {t}" for t in titles),
        max_tokens=300,
    )
    if not text:
        return None

    # 指示に反して長く返すことがあるので、こちらで行数を切り詰める
    lines = [ln.strip(" ・-*0123456789.") for ln in text.splitlines() if ln.strip()]
    text = "\n".join(lines[:MAX_SUMMARY_LINES])
    if not text:
        return None

    conn.execute(
        "INSERT INTO ai_daily_summary (summary_date, summary, article_count, created_at) "
        "VALUES (?, ?, ?, datetime('now')) "
        "ON CONFLICT(summary_date) DO UPDATE SET "
        "summary=excluded.summary, article_count=excluded.article_count, "
        "created_at=excluded.created_at",
        (target, text.strip(), len(titles)),
    )
    conn.commit()
    LOG.info("%s の市場サマリーを生成しました", target)
    return text.strip()


def _ensure_columns(conn) -> None:
    """AI 用のカラムを後付けする（既存データは触らない）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(news_articles)")}
    if "ai_sentiment" not in cols:
        conn.execute("ALTER TABLE news_articles ADD COLUMN ai_sentiment REAL")
    if "ai_checked_at" not in cols:
        conn.execute("ALTER TABLE news_articles ADD COLUMN ai_checked_at TEXT")

    mcols = {r[1] for r in conn.execute("PRAGMA table_info(news_mentions)")}
    if "ai_verified" not in mcols:
        # 1=AIが妥当と判定 / 0=誤検出と判定 / NULL=未判定
        conn.execute("ALTER TABLE news_mentions ADD COLUMN ai_verified INTEGER")
    conn.commit()


def _pending_articles(conn, limit: int) -> list:
    """まだAI判定していない、かつ銘柄マッチがある記事。新しい順。"""
    return conn.execute(
        """
        SELECT a.id, a.title, a.summary
        FROM news_articles a
        WHERE a.ai_checked_at IS NULL
          AND EXISTS (SELECT 1 FROM news_mentions m WHERE m.article_id = a.id)
        ORDER BY a.published_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _mentions_of(conn, article_id: int) -> list:
    return conn.execute(
        """
        SELECT m.coin_id, COALESCE(s.symbol, m.coin_id) AS symbol
        FROM news_mentions m
        LEFT JOIN (
            SELECT coin_id, symbol, MAX(snapshot_date) FROM snapshots GROUP BY coin_id
        ) s ON s.coin_id = m.coin_id
        WHERE m.article_id = ?
        """,
        (article_id,),
    ).fetchall()


def _article_text(title: str | None, summary: str | None, limit: int = 700) -> str:
    text = f"{title or ''}\n{summary or ''}".strip()
    return text[:limit]


def run(limit: int = DEFAULT_LIMIT) -> int:
    if not ai.is_enabled():
        LOG.info("CF_ACCOUNT_ID / CF_AI_TOKEN が未設定のため AI 補強をスキップします")
        return 0

    init_db()
    processed = 0

    with connect() as conn:
        _ensure_columns(conn)
        articles = _pending_articles(conn, limit)
        if not articles:
            # 記事の補強対象が無くても、サマリーは作れる場合がある
            LOG.info("AI 補強の対象記事はありません")
            make_daily_summary(conn)
            return 0

        LOG.info("AI 補強を開始: %d件", len(articles))

        for row in articles:
            article_id, title, summary = row[0], row[1], row[2]
            text = _article_text(title, summary)
            if not text:
                continue

            mentions = _mentions_of(conn, article_id)
            symbols = [m[1].upper() for m in mentions if m[1]]

            # 1) 銘柄マッチの検証
            if symbols:
                verdict = ai.chat_json(
                    MATCH_SYSTEM,
                    f"記事:\n{text}\n\n候補銘柄: {', '.join(symbols)}\n"
                    "この中で記事が実際に話題にしている銘柄だけを挙げてください。",
                    max_tokens=200,
                )
                relevant = None
                if isinstance(verdict, dict):
                    raw = verdict.get("relevant")
                    if isinstance(raw, list):
                        relevant = {str(x).upper().strip() for x in raw}

                if relevant is not None:
                    for coin_id, symbol in mentions:
                        ok = 1 if (symbol or "").upper() in relevant else 0
                        conn.execute(
                            "UPDATE news_mentions SET ai_verified=? "
                            "WHERE article_id=? AND coin_id=?",
                            (ok, article_id, coin_id),
                        )

            # 2) センチメント再判定
            sent = ai.chat_json(
                SENTIMENT_SYSTEM,
                f"記事:\n{text}\n\nこの記事のセンチメントを評価してください。",
                max_tokens=60,
            )
            score = None
            if isinstance(sent, dict):
                try:
                    score = float(sent.get("score"))
                    score = max(-1.0, min(1.0, score))
                except (TypeError, ValueError):
                    score = None

            conn.execute(
                "UPDATE news_articles "
                "SET ai_sentiment=?, ai_checked_at=datetime('now') WHERE id=?",
                (score, article_id),
            )
            conn.commit()
            processed += 1

        # 記事の処理が済んだら、その日の市場サマリーを作る
        make_daily_summary(conn)

    LOG.info("AI 補強が完了: %d件", processed)
    return processed


def main() -> int:
    p = argparse.ArgumentParser(description="Workers AI でニュース処理を補強する")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--all", action="store_true", help="未処理を全件（上限を大きくする）")
    args = p.parse_args()

    run(limit=100000 if args.all else args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
