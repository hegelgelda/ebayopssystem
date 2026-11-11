"""worker コンテナ：定期処理（APScheduler）と画像処理ジョブ。"""
from __future__ import annotations

import logging
import signal
import time

from apscheduler.schedulers.background import BackgroundScheduler

from app.adapters import get_order_adapter
from app.config import get_config
from app.db import SessionLocal, session_scope
from app.logging_setup import setup_logging
from app.services import backup, fx, health, notify, orders, photos
from app.timeutil import JST

log = logging.getLogger("worker")


def _safe(name: str):
    def deco(fn):
        def run():
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("job %s failed", name)
        run.__name__ = run.__qualname__ = f"job_{name}"
        return run
    return deco


@_safe("deadlines")
def job_deadlines() -> None:
    with session_scope() as db:
        orders.run_deadline_checks(db)


@_safe("escalations")
def job_escalations() -> None:
    with session_scope() as db:
        notify.run_escalations(db)


@_safe("fx")
def job_fx() -> None:
    with session_scope() as db:
        fx.fetch_usdjpy(db)


@_safe("poll")
def job_poll() -> None:
    with session_scope() as db:
        get_order_adapter(db).poll(db)


@_safe("catalog")
def job_catalog() -> None:
    from app.services import importers
    from app.settings_store import get_setting
    try:  # tcg-catalog が落ちていても Limitless の補完は続ける
        with session_scope() as db:
            url = get_setting(db, "card_catalog_url")
            if url:
                res = importers.import_tcg_catalog(db, url)
                log.info("card catalog synced: %s", res)
    except Exception:  # noqa: BLE001
        log.exception("tcg-catalog sync failed")
    if get_setting_safe("card_catalog_limitless"):
        with session_scope() as db:
            res = importers.import_limitless_jp(db)
            log.info("limitless jp synced: %s", res)
            if res["new_sets"]:
                from app.services.notify import enqueue_post
                enqueue_post(db, "system", "🆕 カード一覧に新しい弾を追加しました：" + "、".join(res["new_sets"]),
                             mention_roles=[])


def get_setting_safe(key: str):
    from app.settings_store import get_setting
    with session_scope() as db:
        return get_setting(db, key)


@_safe("backup")
def job_backup() -> None:
    try:
        res = backup.create_backup()
        with session_scope() as db:
            health.record(db, "backup", ok=True)
            notify.enqueue_post(db, "system", f"💾 バックアップ完了：{res['db'].rsplit('/', 1)[-1]}"
                                + ("（オブジェクトストレージに保存）" if res["uploaded"] else "（VPS内のみ：オブジェクトストレージ未設定）"))
    except Exception as e:  # noqa: BLE001
        log.exception("backup failed")
        with session_scope() as db:
            notify.system_alert(db, f"⚠️ バックアップに失敗しました：{e}")


@_safe("disk")
def job_disk() -> None:
    ratio = backup.disk_usage_ratio()
    if ratio >= 0.8:
        from app.timeutil import today_jst
        with session_scope() as db:
            notify.system_alert(db, f"⚠️ ディスク使用率が {ratio:.0%} です（80%超）", event_type="disk_usage",
                                dedupe_key=today_jst().isoformat())


def main() -> None:
    cfg = get_config()
    cfg.ensure_dirs()
    setup_logging("worker")
    sched = BackgroundScheduler(timezone=JST)
    sched.add_job(job_deadlines, "interval", minutes=1, id="deadlines", max_instances=1, coalesce=True)
    sched.add_job(job_escalations, "interval", minutes=1, id="escalations", max_instances=1, coalesce=True)
    sched.add_job(job_poll, "interval", minutes=5, id="poll", max_instances=1, coalesce=True)
    sched.add_job(job_fx, "cron", hour=7, minute=10, id="fx")
    sched.add_job(job_backup, "cron", hour=3, minute=30, id="backup")
    sched.add_job(job_catalog, "cron", hour=4, minute=10, id="catalog")
    sched.add_job(job_disk, "interval", hours=1, id="disk")
    sched.start()
    job_fx()
    log.info("worker started")

    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(v=True))
    while not stop["v"]:
        try:
            with SessionLocal() as db:
                n = photos.run_pending_jobs(db)
        except Exception:  # noqa: BLE001
            log.exception("photo jobs failed")
            n = 0
        if not n:
            time.sleep(1.0)
    sched.shutdown(wait=False)


if __name__ == "__main__":
    main()
