"""撮影と画像整形（F0-2）。

アップロードは元画像を保存してジョブを積むだけ（応答を待たせない）。整形は worker が行う：
EXIF向き補正 → 背景除去（失敗時は元画像）→ 長辺1600px → JPEG と WebP。
"""
from __future__ import annotations

import io
import logging
import secrets
import time
import uuid
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.config import get_config
from app.models import Item, ItemStatus as IS, Job, Photo, User
from app.services import states
from app.services.errors import NotFound, ServiceError
from app.timeutil import utcnow

log = logging.getLogger(__name__)

LONG_EDGE = 1600
MIN_EDGE = 500
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
MAX_BYTES = 30 * 1024 * 1024
KIND_SEQ = {"front": 1, "back": 2}


def public_url(photo: Photo) -> str:
    return get_config().base_url.rstrip("/") + "/img/" + photo.public_rel_path


def save_upload(db: Session, user: User, item: Item, kind: str, data: bytes, filename: str,
                client_upload_id: str | None = None, replace_photo_id: int | None = None) -> Photo:
    if kind not in ("front", "back", "extra"):
        raise ServiceError("写真の種類が不正です")
    if client_upload_id:
        dup = db.scalar(select(Photo).where(Photo.client_upload_id == client_upload_id))
        if dup:
            return dup  # 再送（回線断からの復帰）は同じ写真として扱う
    if not data:
        raise ServiceError("ファイルが空です")
    if len(data) > MAX_BYTES:
        raise ServiceError("ファイルが大きすぎます（30MBまで）")
    ext = Path(filename or "").suffix.lower() or ".jpg"
    if ext not in ALLOWED_EXT:
        raise ServiceError("対応していない画像形式です")
    try:
        Image.open(io.BytesIO(data)).verify()
    except Exception:
        if ext not in (".heic", ".heif"):
            raise ServiceError("画像として読み込めませんでした")
    if item.status in (IS.SOLD, IS.WITHDRAWN):
        raise ServiceError("売却済み・取り下げの在庫には写真を追加できません")

    cfg = get_config()
    odir = cfg.originals_dir / item.cert_number
    odir.mkdir(parents=True, exist_ok=True)
    opath = odir / f"{uuid.uuid4().hex}{ext}"
    opath.write_bytes(data)

    old: Photo | None = None
    if replace_photo_id:
        old = db.get(Photo, replace_photo_id)
        if not old or old.item_id != item.id or old.replaced_by_photo_id is not None:
            raise NotFound("差し替え対象の写真が見つかりません")
        kind = old.kind
    elif kind in KIND_SEQ:
        old = item.photo(kind)

    if old is not None:
        seq = old.seq
    elif kind in KIND_SEQ:
        seq = KIND_SEQ[kind]
    else:
        seqs = [p.seq for p in item.photos]
        seq = max([2] + seqs) + 1

    photo = Photo(item_id=item.id, kind=kind, seq=seq, original_path=str(opath.relative_to(cfg.images_dir)),
                  public_token=secrets.token_urlsafe(16).replace("-", "x").replace("_", "y"),
                  uploaded_by=user.id, client_upload_id=client_upload_id or None)
    db.add(photo)
    db.flush()
    if old is not None:
        old.replaced_by_photo_id = photo.id  # 旧画像は残す（F7）
        audit.log(db, audit.Actor.user(user), "item", item.id, "photo.replace",
                  before={"photo_id": old.id, "path": old.processed_path, "original": old.original_path},
                  after={"photo_id": photo.id, "kind": kind, "original": photo.original_path})
    else:
        audit.log(db, audit.Actor.user(user), "item", item.id, "photo.upload",
                  after={"photo_id": photo.id, "kind": kind, "original": photo.original_path})
    db.add(Job(kind="process_photo", payload={"photo_id": photo.id}))
    db.refresh(item)
    maybe_mark_photographed(db, item, audit.Actor.user(user))
    return photo


def maybe_mark_photographed(db: Session, item: Item, actor: audit.Actor) -> None:
    if item.status == IS.INTAKE and item.photo("front") and item.photo("back"):
        states.transition_item(db, item, IS.PHOTOGRAPHED, actor, action="item.photographed")
        from app.services import pricing
        pricing.ensure_open_quote(db, item)


_rembg_session = None
_rembg_retry_at = 0.0  # モデル取得に失敗したら一定時間は背景除去を飛ばす（整形を止めない）


def _remove_bg(img: Image.Image) -> Image.Image | None:
    if not get_config().enable_rembg:
        return None
    global _rembg_session, _rembg_retry_at
    try:
        from rembg import new_session, remove  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        if _rembg_session is None:
            if time.monotonic() < _rembg_retry_at:
                return None
            try:
                _rembg_session = new_session(get_config().rembg_model)
            except Exception:
                _rembg_retry_at = time.monotonic() + 600
                raise
        cut = remove(img, session=_rembg_session)
        bg = Image.new("RGB", cut.size, (255, 255, 255))
        bg.paste(cut, mask=cut.split()[-1])
        return bg
    except Exception as e:  # noqa: BLE001 - 失敗したら元画像を使う
        log.warning("rembg failed: %s", e)
        return None


def _open(path: Path) -> Image.Image:
    if path.suffix.lower() in (".heic", ".heif"):
        try:
            from pillow_heif import register_heif_opener  # type: ignore
            register_heif_opener()
        except Exception:  # noqa: BLE001
            pass
    return Image.open(path)


def process_photo(db: Session, photo_id: int) -> Photo:
    photo = db.get(Photo, photo_id)
    if not photo:
        raise NotFound("photo not found")
    cfg = get_config()
    src = cfg.images_dir / photo.original_path
    try:
        img = _open(src)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        cut = _remove_bg(img)
        photo.bg_removed = cut is not None
        if cut is not None:
            img = cut
        w, h = img.size
        scale = LONG_EDGE / max(w, h)
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        if min(img.size) < MIN_EDGE:
            log.warning("photo %s short edge %s < %s", photo.id, min(img.size), MIN_EDGE)
        rel = Path(photo.public_rel_path)
        out_jpg = cfg.public_images_dir / rel
        out_jpg.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_jpg, "JPEG", quality=90, optimize=True, progressive=True)
        out_webp = out_jpg.with_suffix(".webp")
        img.save(out_webp, "WEBP", quality=85)
        photo.processed_path = str(rel)
        photo.webp_path = str(rel.with_suffix(".webp"))
        photo.width, photo.height = img.size
        photo.process_status = "done"
        photo.process_error = None
    except Exception as e:  # noqa: BLE001
        photo.process_status = "failed"
        photo.process_error = str(e)[:500]
        log.exception("photo processing failed")
    audit.log(db, audit.Actor.system(), "item", photo.item_id, "photo.processed",
              after={"photo_id": photo.id, "status": photo.process_status, "bg_removed": photo.bg_removed,
                     "size": [photo.width, photo.height]})
    return photo


def run_pending_jobs(db: Session, limit: int = 10) -> int:
    jobs = db.scalars(select(Job).where(Job.status == "pending", Job.run_after <= utcnow())
                      .order_by(Job.id).limit(limit)).all()
    n = 0
    for job in jobs:
        job.attempts += 1
        try:
            if job.kind == "process_photo":
                process_photo(db, job.payload["photo_id"])
            job.status = "done"
            job.finished_at = utcnow()
        except Exception as e:  # noqa: BLE001
            job.error = str(e)[:500]
            job.status = "failed" if job.attempts >= 3 else "pending"
        db.commit()
        n += 1
    return n
