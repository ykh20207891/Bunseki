"""暗号資産ニュースの日次収集（RSS）と、銘柄への紐付け・感情スコア付与。

設計:
  - 依存を増やさないため RSS は xml.etree（標準ライブラリ）で解析する。
  - 感情スコアは辞書ベース。暗号資産ニュース特有の語彙に合わせた単語リストを使う。
    ブラックボックスの学習済みモデルより、なぜそのスコアになったか説明できる方を優先した。
  - 銘柄マッチは誤検出が最大の敵。以下で抑える:
      * 銘柄名での一致を主軸にする（単語境界つき、大文字小文字無視）
      * シンボル一致は「3文字以上・大文字・英単語ストップリストに無い」場合のみ
      * 一般語と紛らわしい銘柄名（Story, Move, Near 等）は名前だけでは採用せず、
        シンボルも同じ記事に出ている場合のみ採用する

使い方:
    python src/news.py                 # 全フィードを取得して蓄積
    python src/news.py --status        # 蓄積状況
    python src/news.py --check-feeds   # 各フィードの到達性を確認
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect import USER_AGENT, http_get_text  # noqa: E402
from common import setup_logger  # noqa: E402
from db import connect, init_db  # noqa: E402

LOG = setup_logger("news")

# 媒体名 -> (RSS URL, 言語, サイトURL)
# 除外した媒体:
#   TheBlock         https://www.theblock.co/rss.xml   bot を 403 で弾く
#   Cointelegraph JP https://jp.cointelegraph.com/rss  410 Gone（配信終了）
#   BitcoinNewsJP    https://btcnews.jp/feed/          記事0件
FEEDS = {
    # --- 日本語media（翻訳不要。見出しがそのまま自然な日本語） ---
    "CoinPost": ("https://coinpost.jp/?feed=rss2", "ja", "https://coinpost.jp"),
    "あたらしい経済": ("https://www.neweconomy.jp/feed", "ja", "https://www.neweconomy.jp"),
    "CoinDesk JAPAN": ("https://www.coindeskjapan.com/feed/", "ja",
                       "https://www.coindeskjapan.com"),
    # --- 英語media（見出しは translate.py で日本語訳を付ける） ---
    "CoinDesk": ("https://www.coindesk.com/arc/outboundfeeds/rss/", "en",
                 "https://www.coindesk.com"),
    "Cointelegraph": ("https://cointelegraph.com/rss", "en", "https://cointelegraph.com"),
    "Decrypt": ("https://decrypt.co/feed", "en", "https://decrypt.co"),
    "CryptoSlate": ("https://cryptoslate.com/feed/", "en", "https://cryptoslate.com"),
    "Bitcoinist": ("https://bitcoinist.com/feed/", "en", "https://bitcoinist.com"),
    "NewsBTC": ("https://www.newsbtc.com/feed/", "en", "https://www.newsbtc.com"),
    "AMBCrypto": ("https://ambcrypto.com/feed/", "en", "https://ambcrypto.com"),
    "BitcoinMagazine": ("https://bitcoinmagazine.com/feed", "en",
                        "https://bitcoinmagazine.com"),
    "CryptoPotato": ("https://cryptopotato.com/feed/", "en", "https://cryptopotato.com"),
    "BeInCrypto": ("https://beincrypto.com/feed/", "en", "https://beincrypto.com"),
}

# ---------------------------------------------------------------- 感情辞書
POSITIVE = {
    "surge": 2, "soar": 2, "rally": 2, "skyrocket": 2, "spike": 2, "jump": 1.5,
    "climb": 1.5, "gain": 1.5, "rise": 1, "rises": 1, "up": 0.5, "bullish": 2,
    "breakout": 2, "record": 1.5, "high": 1, "ath": 2, "all-time": 1.5,
    "adoption": 1.5, "partnership": 1.5, "upgrade": 1.5, "approval": 2,
    "approved": 2, "approve": 1.5, "inflow": 1.5, "inflows": 1.5,
    "accumulate": 1.5, "accumulation": 1.5, "buy": 1, "buying": 1, "launch": 1,
    "integration": 1, "milestone": 1.5, "outperform": 2, "rebound": 2,
    "recover": 1.5, "recovery": 1.5, "optimism": 1.5, "optimistic": 1.5,
    "boost": 1.5, "momentum": 1, "institutional": 1, "green": 1, "profit": 1.5,
    "surges": 2, "soars": 2, "rallies": 2, "jumps": 1.5, "gains": 1.5,
    "breakthrough": 2, "expansion": 1, "growth": 1.5, "demand": 1,
    "whale": 0.5, "etf": 0.5, "treasury": 1, "staking": 0.5, "burn": 1,
}
NEGATIVE = {
    "plunge": 2, "crash": 2.5, "tumble": 2, "slump": 2, "plummet": 2.5,
    "drop": 1.5, "fall": 1.5, "falls": 1.5, "decline": 1.5, "slide": 1.5,
    "dump": 2, "bearish": 2, "selloff": 2, "sell-off": 2, "liquidation": 2,
    "liquidations": 2, "hack": 3, "hacked": 3, "exploit": 3, "exploited": 3,
    "breach": 2.5, "lawsuit": 2, "sue": 2, "sued": 2, "ban": 2, "banned": 2,
    "crackdown": 2, "delist": 2.5, "delisted": 2.5, "outflow": 1.5,
    "outflows": 1.5, "fear": 1.5, "warning": 1.5, "warns": 1.5, "risk": 1,
    "scam": 3, "fraud": 3, "rug": 3, "rugpull": 3, "halt": 1.5, "halted": 1.5,
    "bankruptcy": 3, "bankrupt": 3, "investigation": 2, "charges": 2,
    "fine": 1.5, "penalty": 1.5, "downturn": 2, "correction": 1.5,
    "loss": 1.5, "losses": 1.5, "collapse": 2.5, "crisis": 2, "panic": 2.5,
    "plunges": 2, "crashes": 2.5, "tumbles": 2, "drops": 1.5, "sinks": 2,
    "red": 1, "weak": 1.5, "weakness": 1.5, "concern": 1.5, "concerns": 1.5,
    "delay": 1, "rejected": 2, "reject": 1.5, "vulnerability": 2,
}

# 日本語記事用の感情辞書。英語と違い単語境界が無いので部分一致で数える。
POSITIVE_JA = {
    "急騰": 2, "高騰": 2, "急伸": 2, "上昇": 1.5, "反発": 2, "回復": 1.5, "上向": 1.5,
    "最高値": 2, "高値更新": 2, "過去最高": 2, "突破": 1.5, "好調": 1.5, "堅調": 1.5,
    "強気": 2, "買い": 1, "資金流入": 1.5, "流入": 1, "採用": 1.5, "提携": 1.5,
    "承認": 2, "認可": 2, "上場": 1.5, "拡大": 1, "増加": 1, "成長": 1.5, "期待": 1,
    "追い風": 1.5, "好感": 1.5, "改善": 1.5, "実装": 0.5, "アップグレード": 1,
    "過去最大": 1, "推進": 0.5, "参入": 1,
}
NEGATIVE_JA = {
    "急落": 2, "暴落": 2.5, "下落": 1.5, "反落": 1.5, "低迷": 2, "軟調": 1.5,
    "安値": 1, "弱気": 2, "売り": 1, "投げ売り": 2, "資金流出": 1.5, "流出": 1,
    "ハッキング": 3, "ハッキング被害": 3, "不正": 2, "詐欺": 3, "流失": 2,
    "訴訟": 2, "提訴": 2, "規制強化": 2, "禁止": 2, "取り締ま": 2, "摘発": 2,
    "上場廃止": 2.5, "破綻": 3, "倒産": 3, "停止": 1.5, "凍結": 2, "懸念": 1.5,
    "リスク": 1, "警告": 1.5, "損失": 1.5, "赤字": 1.5, "撤退": 1.5, "調整": 1,
    "延期": 1, "却下": 2, "脆弱性": 2, "障害": 1.5, "混乱": 1.5, "下方修正": 2,
}

_WORD_RE = re.compile(r"[a-z][a-z\-']*")
_JA_RE = re.compile(r"[ぁ-んァ-ヴ一-龥]")


def is_japanese(text: str) -> bool:
    return bool(_JA_RE.search(text or ""))


# 平滑化定数。(pos-neg)/(pos+neg) だと該当語が1つあるだけで ±1 に振り切れてしまうため、
# 分母に定数を足して「該当語が多いほど確信度が高い」形にする。
_SENTIMENT_SMOOTHING = 2.5


def sentiment_score(text: str) -> float:
    """辞書ベースの感情スコア。-1（弱気）〜 +1（強気）。

    否定表現（"not bullish" 等）は扱っていない。単純な語彙カウントである点は限界として
    認識しておくこと。見出し中心の短文が対象なので実用上は概ね機能する。
    """
    if not text:
        return 0.0
    if is_japanese(text):
        # 日本語は分かち書きしないので部分一致で数える
        pos = sum(w * text.count(k) for k, w in POSITIVE_JA.items() if k in text)
        neg = sum(w * text.count(k) for k, w in NEGATIVE_JA.items() if k in text)
    else:
        words = _WORD_RE.findall(text.lower())
        pos = sum(POSITIVE.get(w, 0.0) for w in words)
        neg = sum(NEGATIVE.get(w, 0.0) for w in words)
    if pos + neg == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / (pos + neg + _SENTIMENT_SMOOTHING)))


# ---------------------------------------------------------------- 銘柄マッチ
# シンボルと綴りが同じ一般英単語。シンボル一致から除外する。
_SYMBOL_STOPWORDS = {
    "ALL", "AND", "ANY", "ARE", "ASK", "ATM", "BAN", "BAR", "BET", "BIG", "BIT",
    "BOX", "BUY", "CAP", "CAR", "CEO", "CFO", "CTO", "CUT", "DAO", "DAY", "DEX",
    "DID", "END", "ETF", "FED", "FEE", "FEW", "FOR", "FUD", "GAS", "GET", "GOT",
    "HAS", "HIT", "HOT", "HOW", "ICO", "IMF", "INC", "IPO", "IRS", "JOB", "KEY",
    "LAW", "LOW", "MAN", "MAP", "MAX", "MAY", "NET", "NEW", "NEWS", "NFT", "NOT",
    "NOW", "OFF", "OLD", "ONE", "OUR", "OUT", "OWN", "PAY", "PER", "PUT", "RAW",
    "RUN", "SEC", "SEE", "SET", "SIX", "TAX", "TEN", "THE", "TOP", "TRY", "TWO",
    "USE", "VIA", "WAR", "WAS", "WAY", "WHO", "WHY", "WIN", "YES", "YET", "YOU",
    "CBDC", "DEFI", "FOMO", "HODL", "LIVE", "MORE", "MOST", "MUCH", "NEXT",
    "OPEN", "OVER", "SOON", "THAN", "THAT", "THIS", "TIME", "VERY", "WEEK",
    "WELL", "WHAT", "WHEN", "WILL", "WITH", "YEAR", "REAL", "MOVE", "NEAR",
    "SAND", "LINK", "SUN", "APE", "ARB", "AI", "PI", "CC", "ME", "ID", "US",
    "USD", "EUR", "JPY", "GDP", "AML", "KYC", "P2P", "API", "APR", "APY",
}

# よくある英単語。銘柄名がこれ1語だけの場合は名前一致を信用しない
# （実際に "Just" / "Would" / "Cash" / "Cross" などの銘柄が誤検出された）。
_COMMON_WORDS = set("""
about above across act add after again against age ago agree air all allow also
always among amount and another answer any appear apply area arm around arrive art
ask away back bad bag ball bank base beat beauty become bed before begin behind
believe below best better between beyond big bill bit black block blood blue board
boat body book born both box boy break bring broad brother build business but buy
call can car card care carry case cash catch cause cell center central century
certain chair chance change charge check child choice choose church city civil claim
class clear close coast cold collect color come common company compare complete
computer concern condition consider contain continue control cook cool copy corner
cost could count country couple course court cover create cross crowd cup current cut
dark data date day dead deal dear death decide deep degree demand describe design
desire detail develop die difference direct discover discuss divide doctor dog dollar
door double down draw dream dress drink drive drop dry during duty each early earth
ease east easy eat edge effect effort egg eight either else end enemy energy english
enjoy enough enter entire equal escape even evening event ever every exact example
except exist expect experience explain eye face fact fail fair fall family far farm
fast father fear feed feel few field fight figure fill film final find fine finger
finish fire first fish fit five fix flat floor flow fly follow food foot for force
forest forget form former forward four free fresh friend from front full fun future
gain game garden gas gather general get gift girl give glass go gold good govern
grand grass gray great green ground group grow guard guess guide hair half hand
hang happen happy hard have head hear heart heat heavy help here hide high hill
history hit hold hole home hope horse hospital hot hour house how huge human hundred
hunt hurry idea image imagine impact important improve include increase indeed
industry inside instead interest into introduce invite iron island issue item join
joy judge jump just keep key kill kind king knock know labor lack lady land language
large last late laugh law lay lead learn least leave left leg legal length less let
letter level lie life lift light like limit line list listen little live load local
lock long look lose loss lot love low luck machine main major make man many map march
mark market marry mass master match matter may mean measure meat meet member memory
mention method middle might mile milk mind mine minute miss mix model modern moment
money month moon moral more morning most mother mount move much music must name
nation native nature near necessary neck need never new news next nice night nine
none noon nor north nose note nothing notice now number object occur ocean offer
office often oil old once one only open opinion order origin other ought out over
own page pain paint pair paper part party pass past path pay peace people perfect
perhaps period person phone pick picture piece place plan plant play please point
poor popular position possible post pound power practice prepare present press
pretty prevent price print private prize problem produce program promise proper
protect prove provide public pull pure purpose push put quality quarter question
quick quiet quite race radio rain raise range rate rather reach read ready real
reason receive record red reduce refer reflect refuse regard region relate remain
remember remove repeat reply report represent require rest result return reveal
rich ride right ring rise risk river road rock roll room root round rule run safe
sail salt same sand save say scale scene school science score sea search season
seat second secret section see seek seem sell send sense separate serious serve set
settle seven several shake shall shape share sharp she shell shine ship shoot shop
short should show side sign silence silver similar simple since sing single sir
sister sit site six size skill skin sky sleep slow small smile smoke snow social
soft soil soldier solid some son song soon sort sound source south space speak
special speed spend spirit spot spread spring square stage stand star start state
station stay step stick still stock stone stop store storm story straight strange
street stress strike strong structure student study stuff style subject succeed such
sudden suffer suggest summer sun supply support suppose sure surface surprise
swim system table take talk tall taste teach team tear tell ten term test than
thank that the their them then there these they thick thin thing think third this
those though thought three through throw thus tie time tiny title today together
tone tonight too top total touch toward town trade train travel treat tree trip
trouble true trust truth try turn twenty twice two type under understand union unit
until up upon use usual value various very view village visit voice vote wait walk
wall want war warm wash watch water wave way weak wear weather week weight welcome
well west what wheel when where whether which while white who whole why wide wife
wild will win wind window wine wing winter wire wise wish with within without woman
wonder wood word work world worry worth would write wrong yard year yellow yes
yesterday yet you young your
""".split())

# 上記の機械的判定で拾いきれない、暗号資産特有の紛らわしい銘柄名
_AMBIGUOUS_NAMES = {
    "arbitrum one", "aptos", "sui", "sei", "mask", "gala", "vana", "swell",
    "grass", "banana", "turbo", "baby doge", "immutable", "the graph", "beam",
    "ark", "pi network", "power ledger", "official trump", "sun token",
    "worldcoin", "world liberty financial", "spark", "echo", "usual", "ondo",
    "aerodrome finance", "jupiter", "raydium", "pendle", "ethena", "lido dao",
}


# 日本語記事は銘柄をカタカナで書くため、英語名では一致しない。
# シンボルへの別名表を持ち、シンボル経由で coin_id に解決する。
_JA_NAME_TO_SYMBOL = {
    "ビットコイン": "BTC", "イーサリアム": "ETH", "イーサ": "ETH", "リップル": "XRP",
    "テザー": "USDT", "ソラナ": "SOL", "ドージコイン": "DOGE", "カルダノ": "ADA",
    "エイダ": "ADA", "トロン": "TRX", "ポルカドット": "DOT", "チェーンリンク": "LINK",
    "ライトコイン": "LTC", "ビットコインキャッシュ": "BCH", "ステラ": "XLM",
    "モネロ": "XMR", "アバランチ": "AVAX", "ポリゴン": "POL", "シバイヌ": "SHIB",
    "ユニスワップ": "UNI", "アプトス": "APT", "アービトラム": "ARB",
    "オプティミズム": "OP", "コスモス": "ATOM", "テゾス": "XTZ", "ファイルコイン": "FIL",
    "ヘデラ": "HBAR", "アスター": "ASTR", "ネム": "XEM", "シンボル": "XYM",
    "イオス": "EOS", "ペペ": "PEPE", "ハイパーリキッド": "HYPE", "ジーキャッシュ": "ZEC",
    "ワールドコイン": "WLD", "リド": "LDO", "メイカー": "MKR", "エイブ": "AAVE",
    "アーベ": "AAVE", "カーブ": "CRV", "サンドボックス": "SAND", "ディセントラランド": "MANA",
    "エイプコイン": "APE", "イミュータブル": "IMX", "スイ": "SUI", "セイ": "SEI",
    "ニア": "NEAR", "アルゴランド": "ALGO", "ヴィチェーン": "VET", "クアンタム": "QTUM",
    "ダッシュ": "DASH", "ジャスミー": "JASMY", "エンジン": "ENJ", "チリーズ": "CHZ",
    "ボバ": "BOBA", "オアシス": "OAS", "パレット": "PLT", "フィナンシェ": "FNCT",
    "ビットフライヤー": None, "ソニー": None,   # 企業名。銘柄ではないので無視する
}


def _load_coins(conn) -> list[dict]:
    latest = conn.execute("SELECT MAX(snapshot_date) FROM snapshots").fetchone()[0]
    rows = conn.execute(
        "SELECT coin_id, symbol, name FROM snapshots WHERE snapshot_date = ?"
        " ORDER BY market_cap DESC", (latest,)).fetchall()
    coins = []
    for r in rows:
        name = (r["name"] or "").strip()
        sym = (r["symbol"] or "").strip().upper()
        if not name or not sym:
            continue
        low = name.lower()
        single_token = " " not in low
        ambiguous = (
            low in _AMBIGUOUS_NAMES
            or len(low) <= 3
            or (single_token and low in _COMMON_WORDS)
            or (single_token and low.rstrip("s") in _COMMON_WORDS)
        )
        coins.append({
            "coin_id": r["coin_id"],
            "symbol": sym,
            "name": name,
            "name_re": re.compile(r"\b" + re.escape(low) + r"\b"),
            "sym_re": re.compile(r"\b" + re.escape(sym) + r"\b"),
            "ambiguous": ambiguous,
            "sym_usable": len(sym) >= 3 and sym not in _SYMBOL_STOPWORDS,
        })
    return coins


def rematch_all() -> int:
    """マッチ規則や感情辞書を更新したとき、蓄積済み記事を再処理する。"""
    init_db()
    with connect() as conn:
        coins = _load_coins(conn)
        arts = conn.execute("SELECT id, title, summary, pub_date FROM news_articles").fetchall()
        LOG.info("再処理対象: %d 記事 / 銘柄辞書 %d 件", len(arts), len(coins))
        conn.execute("DELETE FROM news_mentions")
        n = 0
        for a in arts:
            text = (a["title"] or "") + " " + (a["summary"] or "")
            conn.execute(
                "UPDATE news_articles SET sentiment = ?, lang = ? WHERE id = ?",
                (sentiment_score(text), "ja" if is_japanese(text) else "en", a["id"]))
            for coin_id, how in match_coins(text, coins):
                conn.execute(
                    "INSERT OR IGNORE INTO news_mentions"
                    " (article_id, coin_id, pub_date, matched_by) VALUES (?,?,?,?)",
                    (a["id"], coin_id, a["pub_date"], how))
                n += 1
        conn.commit()
    LOG.info("再マッチ完了: 言及 %d 件", n)
    return 0


# カタカナ名を長い順に並べたもの（包含関係の判定に使う）
_JA_SORTED = sorted((k for k, v in _JA_NAME_TO_SYMBOL.items() if v), key=len, reverse=True)


def ja_symbols_in(text: str) -> set[str]:
    """日本語テキストに出てくるカタカナ銘柄名からシンボル集合を作る。

    「ビットコインキャッシュ」には「ビットコイン」が含まれてしまうので、
    長い名前の出現回数を差し引いて、独立した言及が残る場合だけ採用する。
    """
    kept: list[str] = []
    for ja in _JA_SORTED:                      # 長い名前から順に見る
        n = text.count(ja)
        if n == 0:
            continue
        consumed = sum(text.count(longer) for longer in kept
                       if ja != longer and ja in longer)
        if n - consumed > 0:
            kept.append(ja)
    return {_JA_NAME_TO_SYMBOL[ja] for ja in kept}


def match_coins(text: str, coins: list[dict]) -> list[tuple[str, str]]:
    """記事本文から言及されている銘柄を抽出する。戻り値は (coin_id, 一致方法)。"""
    low = text.lower()
    ja_syms = ja_symbols_in(text) if is_japanese(text) else set()
    hits = []
    for c in coins:
        if c["symbol"] in ja_syms:
            hits.append((c["coin_id"], "ja_name"))
            continue
        by_name = bool(c["name_re"].search(low))
        by_sym = c["sym_usable"] and bool(c["sym_re"].search(text))
        if c["ambiguous"]:
            if by_name and by_sym:
                hits.append((c["coin_id"], "name+symbol"))
        elif by_name:
            hits.append((c["coin_id"], "name"))
        elif by_sym:
            hits.append((c["coin_id"], "symbol"))
    return hits


# ---------------------------------------------------------------- RSS 解析
def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "").replace("&nbsp;", " ").strip()


def _parse_date(s: str):
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed(xml_text: str) -> list[dict]:
    """RSS 2.0 / Atom の両方に対応して記事リストを返す。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise RuntimeError("XML 解析に失敗: %s" % e)

    ns = {"atom": "http://www.w3.org/2005/Atom",
          "content": "http://purl.org/rss/1.0/modules/content/",
          "dc": "http://purl.org/dc/elements/1.1/"}
    items = []

    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue

        def txt(name):
            el = it.find(name, ns)
            return (el.text or "") if el is not None and el.text else ""

        title = txt("title") or txt("atom:title")
        link = txt("link") or txt("atom:link")
        if not link:
            el = it.find("atom:link", ns)
            if el is not None:
                link = el.get("href", "")
        desc = (txt("description") or txt("atom:summary") or txt("content:encoded")
                or txt("atom:content"))
        pub = (txt("pubDate") or txt("atom:published") or txt("atom:updated")
               or txt("dc:date"))
        if not (title and link):
            continue
        items.append({
            "title": _strip_html(title),
            "link": link.strip(),
            "summary": _strip_html(desc)[:1200],
            "published": _parse_date(pub),
        })
    return items


# ---------------------------------------------------------------- 収集本体
def collect_news(feeds: dict | None = None) -> int:
    init_db()
    feeds = feeds or FEEDS
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"}

    with connect() as conn:
        coins = _load_coins(conn)
    LOG.info("銘柄辞書: %d 件", len(coins))

    total_new = total_mentions = 0
    for source, spec in feeds.items():
        url, lang = (spec[0], spec[1]) if isinstance(spec, tuple) else (spec, "en")
        try:
            xml_text = http_get_text(url, headers, timeout=45, max_retry=3)
            items = parse_feed(xml_text)
        except Exception as e:
            LOG.warning("%-16s 取得失敗: %s", source, str(e)[:120])
            continue

        new_here = mentions_here = 0
        with connect() as conn:
            for it in items:
                pub = it["published"] or datetime.now(timezone.utc)
                pub_date = pub.strftime("%Y-%m-%d")
                text = it["title"] + " " + it["summary"]
                senti = sentiment_score(text)
                # 日本語media は見出しがそのまま日本語なので翻訳不要
                title_ja = it["title"] if lang == "ja" else None

                cur = conn.execute(
                    "INSERT OR IGNORE INTO news_articles"
                    " (link, published_at, pub_date, source, title, summary, sentiment,"
                    "  lang, title_ja, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (it["link"], pub.isoformat(timespec="seconds"), pub_date, source,
                     it["title"], it["summary"], senti, lang, title_ja, fetched_at))
                if cur.rowcount == 0:
                    continue                     # 既出の記事
                new_here += 1
                article_id = cur.lastrowid

                for coin_id, how in match_coins(text, coins):
                    conn.execute(
                        "INSERT OR IGNORE INTO news_mentions"
                        " (article_id, coin_id, pub_date, matched_by) VALUES (?,?,?,?)",
                        (article_id, coin_id, pub_date, how))
                    mentions_here += 1
            conn.commit()

        total_new += new_here
        total_mentions += mentions_here
        LOG.info("%-16s 記事 %3d 件（新規 %3d / 銘柄言及 %3d）",
                 source, len(items), new_here, mentions_here)

    LOG.info("=== ニュース収集完了: 新規 %d 記事 / 言及 %d 件 ===", total_new, total_mentions)
    return 0


def check_feeds() -> int:
    headers = {"User-Agent": USER_AGENT}
    for source, spec in FEEDS.items():
        url = spec[0] if isinstance(spec, tuple) else spec
        try:
            xml_text = http_get_text(url, headers, timeout=30, max_retry=2)
            items = parse_feed(xml_text)
            dates = [i["published"] for i in items if i["published"]]
            newest = max(dates).strftime("%Y-%m-%d %H:%M") if dates else "?"
            print("  OK   %-16s %3d 件  最新: %s" % (source, len(items), newest))
        except Exception as e:
            print("  NG   %-16s %s" % (source, str(e)[:90]))
    return 0


def show_status() -> None:
    with connect() as conn:
        a = conn.execute(
            "SELECT COUNT(*) n, MIN(pub_date) d0, MAX(pub_date) d1,"
            " COUNT(DISTINCT pub_date) days FROM news_articles").fetchone()
        print("記事数     : {:,}".format(a["n"] or 0))
        print("期間       : %s 〜 %s (%s 日)" % (a["d0"], a["d1"], a["days"]))
        m = conn.execute("SELECT COUNT(*) n, COUNT(DISTINCT coin_id) c FROM news_mentions").fetchone()
        print("銘柄言及   : {:,} 件 / {} 銘柄".format(m["n"] or 0, m["c"] or 0))
        print("")
        print("言及の多い銘柄 (直近7日):")
        rows = conn.execute(
            "SELECT nm.coin_id, COUNT(*) n, AVG(na.sentiment) s"
            " FROM news_mentions nm JOIN news_articles na ON na.id = nm.article_id"
            " WHERE nm.pub_date >= date('now','-7 day')"
            " GROUP BY nm.coin_id ORDER BY n DESC LIMIT 15").fetchall()
        for r in rows:
            print("  %-24s %3d 件  平均感情 %+.2f" % (r["coin_id"], r["n"], r["s"] or 0))
        nd = conn.execute("SELECT COUNT(*) n, COUNT(DISTINCT coin_id) c,"
                          " MIN(date) d0, MAX(date) d1 FROM news_daily").fetchone()
        if nd["n"]:
            print("")
            print("外部日次集計(GDELT等): {:,} 行 / {} 銘柄 / {} 〜 {}".format(
                nd["n"], nd["c"], nd["d0"], nd["d1"]))


def main() -> int:
    p = argparse.ArgumentParser(description="暗号資産ニュースの収集")
    p.add_argument("--status", action="store_true")
    p.add_argument("--check-feeds", action="store_true")
    p.add_argument("--rematch", action="store_true",
                   help="蓄積済み記事の銘柄紐付けを作り直す")
    args = p.parse_args()
    if args.check_feeds:
        return check_feeds()
    if args.rematch:
        return rematch_all()
    if args.status:
        init_db()
        show_status()
        return 0
    return collect_news()


if __name__ == "__main__":
    raise SystemExit(main())
