"""バックアップ（7章）：DBと画像を毎日バックアップし30日分保持。保存先は S3 互換のオブジェクトストレージ。

ローカルにも /data/backups に同じものを置く（オブジェクトストレージ未設定でも動く）。
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

from app.config import get_config
from app.timeutil import JST

log = logging.getLogger(__name__)


def _s3():
    cfg = get_config()
    if not (cfg.s3_bucket and cfg.s3_access_key and cfg.s3_secret_key):
        return None
    import boto3
    return boto3.client("s3", endpoint_url=cfg.s3_endpoint_url or None, aws_access_key_id=cfg.s3_access_key,
                        aws_secret_access_key=cfg.s3_secret_key, region_name=cfg.s3_region or None)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(JST)).strftime("%Y%m%d-%H%M%S")


def create_backup(now: datetime | None = None) -> dict:
    cfg = get_config()
    cfg.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp(now)
    db_out = cfg.backups_dir / f"db-{stamp}.sqlite3"
    # SQLite のオンラインバックアップ（WAL中でも整合性のあるコピー）
    src = sqlite3.connect(cfg.db_path)
    dst = sqlite3.connect(db_out)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    img_out = cfg.backups_dir / f"images-{stamp}.tar.gz"
    with tarfile.open(img_out, "w:gz") as tar:
        if cfg.images_dir.exists():
            tar.add(cfg.images_dir, arcname="images")
    uploaded = []
    s3 = _s3()
    if s3 is not None:
        for p in (db_out, img_out):
            key = f"{cfg.s3_prefix}backups/{p.name}"
            s3.upload_file(str(p), cfg.s3_bucket, key)
            uploaded.append(key)
    prune()
    log.info("backup created: %s %s uploaded=%s", db_out.name, img_out.name, uploaded)
    return {"db": str(db_out), "images": str(img_out), "uploaded": uploaded}


def _stamp_of(name: str) -> datetime | None:
    try:
        part = name.split("-", 1)[1].split(".")[0]
        return datetime.strptime(part, "%Y%m%d-%H%M%S").replace(tzinfo=JST)
    except (IndexError, ValueError):
        return None


def prune(now: datetime | None = None) -> None:
    cfg = get_config()
    cutoff = (now or datetime.now(JST)) - timedelta(days=cfg.backup_retention_days)
    for p in cfg.backups_dir.glob("*"):
        ts = _stamp_of(p.name)
        if ts and ts < cutoff:
            p.unlink(missing_ok=True)
    s3 = _s3()
    if s3 is not None:
        resp = s3.list_objects_v2(Bucket=cfg.s3_bucket, Prefix=f"{cfg.s3_prefix}backups/")
        for obj in resp.get("Contents", []):
            ts = _stamp_of(obj["Key"].rsplit("/", 1)[-1])
            if ts and ts < cutoff:
                s3.delete_object(Bucket=cfg.s3_bucket, Key=obj["Key"])


def list_backups() -> list[str]:
    cfg = get_config()
    names = {p.name for p in cfg.backups_dir.glob("db-*.sqlite3")}
    s3 = _s3()
    if s3 is not None:
        resp = s3.list_objects_v2(Bucket=cfg.s3_bucket, Prefix=f"{cfg.s3_prefix}backups/db-")
        names |= {o["Key"].rsplit("/", 1)[-1] for o in resp.get("Contents", [])}
    return sorted(n[3:-8] for n in names)


def restore(stamp: str) -> None:
    """stamp は YYYYmmdd-HHMMSS。app / worker / bot を止めてから実行する。"""
    cfg = get_config()
    db_name, img_name = f"db-{stamp}.sqlite3", f"images-{stamp}.tar.gz"
    s3 = _s3()
    for name in (db_name, img_name):
        local = cfg.backups_dir / name
        if not local.exists():
            if s3 is None:
                raise FileNotFoundError(f"{name} がありません")
            s3.download_file(cfg.s3_bucket, f"{cfg.s3_prefix}backups/{name}", str(local))
    # DB
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)
    shutil.copy2(cfg.backups_dir / db_name, cfg.db_path)
    # 画像
    if cfg.images_dir.exists():
        shutil.rmtree(cfg.images_dir)
    with tarfile.open(cfg.backups_dir / img_name, "r:gz") as tar:
        tar.extractall(cfg.data_dir, filter="data")
    log.info("restored backup %s", stamp)


def disk_usage_ratio() -> float:
    u = shutil.disk_usage(get_config().data_dir)
    return u.used / u.total if u.total else 0.0
