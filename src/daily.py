"""日次パイプラインの入口。ローカル（collect.bat）でもクラウド（GitHub Actions）でも
これ 1 本を呼べば同じ処理が走るようにしてある。

各ステップは独立して失敗しうる（外部APIが落ちる、レート制限に当たる等）。
1 つ転んでも後続を止めず、最後にまとめて結果を報告する。
ただし価格収集だけは全ての土台なので、失敗したら異常終了する。

使い方:
    python src/daily.py
    python src/daily.py --dashboard-out site/index.html
    python src/daily.py --skip predict            # 特定ステップを飛ばす
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import setup_logger  # noqa: E402

LOG = setup_logger("daily")


def step_collect():
    import collect
    if collect.run_collect() != 0:
        raise RuntimeError("価格収集が失敗しました")


def step_news():
    import news
    news.collect_news()


def step_translate():
    import translate
    translate.run(limit=30, sleep_sec=1.2)


def step_icons():
    import icons
    icons.download(size="small", refresh=False, limit=None, sleep_sec=0.3)
    icons.fetch_source_icons(refresh=False)


def step_predict():
    import predict
    predict.run_predict(horizon=7, top_n=20, model_tag=None, skip_importance=True)


def make_export_json_step(dashboard_out: str | None):
    """資産管理アプリが取得するための予測 JSON を、ダッシュボードと同じ場所へ出す。"""
    def step_export_json():
        import export_json
        target = (
            Path(dashboard_out).parent / "prediction.json"
            if dashboard_out
            else None
        )
        export_json.run_export(str(target) if target else None)
    return step_export_json


def make_dashboard_step(out_path: str | None):
    def step_dashboard():
        import dashboard
        from db import connect, init_db
        init_db()
        with connect() as conn:
            data = dashboard.gather(conn)
        target = Path(out_path) if out_path else (dashboard.REPORT_DIR / "dashboard.html")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dashboard.build_html(data), encoding="utf-8")
        LOG.info("ダッシュボードを生成: %s (%.0f KB)", target, target.stat().st_size / 1024)
    return step_dashboard


# (名前, 関数, 失敗したら中断するか)
def build_steps(dashboard_out: str | None):
    return [
        ("collect", step_collect, True),
        ("news", step_news, False),
        ("translate", step_translate, False),
        ("icons", step_icons, False),
        ("predict", step_predict, False),
        ("dashboard", make_dashboard_step(dashboard_out), False),
        ("export_json", make_export_json_step(dashboard_out), False),
    ]


def main() -> int:
    p = argparse.ArgumentParser(description="日次パイプライン")
    p.add_argument("--dashboard-out", default=None,
                   help="ダッシュボードの出力先（既定 reports/dashboard.html）")
    p.add_argument("--skip", action="append", default=[],
                   help="飛ばすステップ名（複数指定可）")
    args = p.parse_args()

    steps = build_steps(args.dashboard_out)
    results: list[tuple[str, str, float, str]] = []
    fatal = False

    for name, fn, critical in steps:
        if name in args.skip:
            results.append((name, "skip", 0.0, ""))
            continue
        LOG.info("--- %s 開始 ---", name)
        t0 = time.monotonic()
        try:
            fn()
            results.append((name, "ok", time.monotonic() - t0, ""))
        except Exception as e:
            msg = "%s: %s" % (type(e).__name__, e)
            results.append((name, "NG", time.monotonic() - t0, msg[:200]))
            LOG.error("--- %s 失敗 --- %s", name, msg[:300])
            LOG.debug("%s", traceback.format_exc())
            if critical:
                fatal = True
                LOG.error("%s は他の全処理の前提なので中断します。", name)
                break

    LOG.info("=" * 56)
    LOG.info(" 日次パイプライン結果")
    LOG.info("=" * 56)
    for name, status, secs, msg in results:
        LOG.info(" %-10s %-4s %6.1f秒 %s", name, status, secs, msg)
    ng = [r for r in results if r[1] == "NG"]
    LOG.info(" 失敗 %d / %d ステップ", len(ng), len(results))

    return 1 if fatal else 0


if __name__ == "__main__":
    raise SystemExit(main())
