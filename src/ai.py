"""Cloudflare Workers AI の薄いクライアント。

日次バッチ（GitHub Actions / ローカル）から REST API 経由で呼ぶ。
無料枠（1日1万ニューロン）で足りる想定。記事は1日20件程度しかない。

必要な環境変数:
    CF_ACCOUNT_ID    Cloudflare のアカウントID
    CF_AI_TOKEN      Workers AI - Read / Edit 権限を持つ APIトークン

どちらか欠けていれば `is_enabled()` が False を返し、呼び出し側は
従来の辞書ベース処理にそのままフォールバックする（AIは任意の上乗せ）。
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import setup_logger  # noqa: E402

LOG = setup_logger("ai")

# collect.py と同じ TLS 事情（Avast の HTTPS 復号で OpenSSL が証明書を拒否する）。
# truststore で Windows ネイティブ検証にし、駄目なら curl.exe に切り替える。
_SSL_CTX = None
_USE_CURL = False


def _ssl_context():
    global _SSL_CTX
    if _SSL_CTX is None:
        try:
            import truststore  # type: ignore
            _SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            try:
                import certifi  # type: ignore
                _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def _curl_post(url: str, headers: dict, body: bytes, timeout: int) -> bytes:
    curl = shutil.which(r"C:\Windows\System32\curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("curl.exe が見つかりません")
    cmd = [curl, "-sS", "--fail", "--location", "--max-time", str(timeout), "-X", "POST"]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["--data-binary", "@-", url]
    proc = subprocess.run(cmd, input=body, capture_output=True, timeout=timeout + 30)
    if proc.returncode != 0:
        raise RuntimeError(
            "curl 失敗 (rc=%d): %s"
            % (proc.returncode, proc.stderr.decode("utf-8", "replace")[:200])
        )
    return proc.stdout

BASE = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"

# 用途別のモデル。安価で速いものを選ぶ。
MODEL_TEXT = "@cf/meta/llama-3.1-8b-instruct"
MODEL_TRANSLATE = "@cf/meta/m2m100-1.2b"

TIMEOUT = 40
MAX_RETRY = 2


def is_enabled() -> bool:
    return bool(os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_AI_TOKEN"))


def _post(model: str, payload: dict) -> dict | None:
    account = os.environ.get("CF_ACCOUNT_ID")
    token = os.environ.get("CF_AI_TOKEN")
    if not account or not token:
        return None

    url = BASE.format(account=account, model=model)
    body = json.dumps(payload).encode("utf-8")

    global _USE_CURL
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    for attempt in range(MAX_RETRY + 1):
        try:
            if _USE_CURL:
                raw = _curl_post(url, headers, body, TIMEOUT)
            else:
                req = urllib.request.Request(
                    url, data=body, headers=headers, method="POST"
                )
                with urllib.request.urlopen(
                    req, timeout=TIMEOUT, context=_ssl_context()
                ) as res:
                    raw = res.read()
            data = json.loads(raw.decode("utf-8"))
            if not data.get("success"):
                LOG.warning("Workers AI が失敗を返しました: %s", data.get("errors"))
                return None
            return data.get("result") or None
        except urllib.error.HTTPError as e:
            # 429(レート制限) / 5xx は少し待って再試行
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRY:
                time.sleep(2 * (attempt + 1))
                continue
            LOG.warning("Workers AI HTTP %s: %s", e.code, e.reason)
            return None
        except Exception as e:  # noqa: BLE001
            # TLS で弾かれる環境では curl.exe に切り替えて再試行する
            if not _USE_CURL and "CERTIFICATE_VERIFY_FAILED" in str(e):
                LOG.info("TLS 検証に失敗したため curl 経由に切り替えます")
                _USE_CURL = True
                continue
            if attempt < MAX_RETRY:
                time.sleep(2 * (attempt + 1))
                continue
            LOG.warning("Workers AI 呼び出しに失敗: %s", e)
            return None

    return None


def chat(system: str, user: str, max_tokens: int = 400) -> str | None:
    """指示に従ってテキストを返させる。失敗したら None。"""
    result = _post(
        MODEL_TEXT,
        {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            # 分析用途なので毎回ぶれない方が良い
            "temperature": 0.1,
        },
    )
    if not result:
        return None

    # モデルによって形式が違う:
    #   旧: {"response": "..."}
    #   新(OpenAI互換): {"choices":[{"message":{"content":"..."}}]}
    text = result.get("response")
    if not isinstance(text, str) or not text.strip():
        choices = result.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            text = message.get("content")

    return text.strip() if isinstance(text, str) and text.strip() else None


def chat_json(system: str, user: str, max_tokens: int = 400) -> dict | list | None:
    """JSON を返させる。壊れた出力は None にする（呼び出し側でフォールバック）。"""
    raw = chat(system + "\n必ず JSON のみを出力し、説明文は書かないこと。", user, max_tokens)
    if not raw:
        return None

    # ```json ... ``` で包まれることがあるので剥がす
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()

    # 前後に余計な文字が付く場合に備えて最初の { or [ から最後の } or ] まで
    start = min(
        (i for i in (text.find("{"), text.find("[")) if i >= 0),
        default=-1,
    )
    end = max(text.rfind("}"), text.rfind("]"))
    if start >= 0 and end > start:
        text = text[start : end + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        LOG.debug("AI の JSON 解析に失敗: %s", raw[:200])
        return None


def translate(text: str, source: str = "en", target: str = "ja") -> str | None:
    """機械翻訳。MyMemory の代替（レート制限が緩く安定している）。"""
    result = _post(
        MODEL_TRANSLATE,
        {"text": text, "source_lang": source, "target_lang": target},
    )
    if not result:
        return None
    out = result.get("translated_text")
    return out.strip() if isinstance(out, str) else None
