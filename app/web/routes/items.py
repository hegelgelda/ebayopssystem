"""在庫一覧・入荷登録・在庫詳細・撮影（F6-2〜F6-4）。"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import audit
from app.config import get_config
from app.db import get_db
from app.models import (
    AuditLog, Category, Comment, Item, ItemStatus as IS, Listing, ListingFormat as LF, Order, Photo, User,
)
from app.services import cardinfo, items as item_svc, labels as label_svc, listing as listing_svc, photos as photo_svc, pricing
from app.services.errors import NotFound, ServiceError
from app.settings_store import get_setting
from app.web.deps import current_user, flash, render, require_lv2
from app.web.routes.common import back, form_int, to

router = APIRouter()


@router.get("/items")
def items_list(request: Request, status: str = "", set_id: str = "", box: str = "", q: str = "",
               date_from: str = "", date_to: str = "", category: str = "", fmt: str = "",
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Item)
    if category:
        stmt = stmt.where(Item.category == category)
    if fmt:
        stmt = stmt.where(Item.listing_format == fmt)
    if status:
        stmt = stmt.where(Item.status == status)
    if set_id:
        stmt = stmt.where(Item.set_id == set_id)
    if box:
        stmt = stmt.where(Item.storage_box == box)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Item.cert_number.like(like), Item.sku.ilike(like), Item.model_id.ilike(like),
                              Item.card_name_jp.like(like), Item.card_number.ilike(like),
                              Item.card_name_en.ilike(like), Item.set_name.ilike(like)))
    if date_from:
        stmt = stmt.where(Item.created_at >= date.fromisoformat(date_from))
    if date_to:
        stmt = stmt.where(Item.created_at < date.fromisoformat(date_to) + timedelta(days=1))
    rows = db.scalars(stmt.order_by(Item.id.desc()).limit(500)).all()
    sets = [s for (s,) in db.execute(select(Item.set_id).distinct().order_by(Item.set_id)) if s]
    boxes = [b for (b,) in db.execute(select(Item.storage_box).distinct().order_by(Item.storage_box)) if b]
    return render(request, "items_list.html", {
        "user": user, "items": rows, "sets": sets, "boxes": boxes,
        "f": {"status": status, "set_id": set_id, "box": box, "q": q, "date_from": date_from, "date_to": date_to,
              "category": category, "fmt": fmt}})


# ---- 入荷登録（F0-1 / F6-3） ----

@router.get("/intake")
def intake_page(request: Request, cert: str = "", done: int | None = None, cat: str = "",
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    last = db.get(Item, done) if done else None
    return render(request, "intake.html", _intake_ctx(db, user, cert=cert, last=last,
                                                       cat=cat or (last.category if last else "")))


def _intake_ctx(db: Session, user: User, **kw) -> dict:
    return {"user": user, "today": date.today().isoformat(), "Category": Category, "LF": LF,
            "conditions": get_setting(db, "raw_conditions") or [],
            "default_formats": get_setting(db, "default_format_by_category") or {},
            "labels_pending": len(label_svc.pending_items(db)), **kw}


@router.get("/api/lookup")
def api_lookup(cert: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = cardinfo.normalize_cert(cert)
    if not c:
        return JSONResponse({"error": "cert番号を読み取れませんでした"}, status_code=400)
    existing = item_svc.find_by_cert(db, c)
    if existing:
        return {"cert": c, "existing": {"id": existing.id, "status": IS.LABELS[existing.status],
                                        "card_name": existing.card_name_jp or existing.card_name_en,
                                        "model_id": existing.model_id, "url": f"/items/{existing.id}"}}
    res = cardinfo.lookup(db, c)
    return {"cert": c, "source": res["source"], "fields": res["fields"], "errors": res["errors"],
            "candidates": [_cand(x) for x in res["candidates"]]}


def _cand(c) -> dict:
    return {"model_id": c.model_id, "set_id": c.set_id, "card_number": c.card_number, "name_jp": c.name_jp,
            "name_en": c.name_en, "set_name": c.set_name, "set_name_jp": c.set_name_jp, "rarity": c.rarity,
            "image_url": c.image_url}


@router.get("/api/catalog/search")
def api_catalog_search(q: str = "", number: str = "", set: str = "", user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    """q：カード名などの部分一致／number：型番（「201」「201/165」）で検索。set でセットを絞り込む。"""
    if number:
        return {"candidates": [_cand(c) for c in cardinfo.search_by_number(db, number, set)],
                "number": cardinfo.normalize_card_number(number)}
    return {"candidates": [_cand(c) for c in cardinfo.candidates(db, q)]}


@router.get("/api/catalog/sets")
def api_catalog_sets(q: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"sets": cardinfo.search_sets(db, q)}


@router.post("/intake")
async def intake_submit(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    form = dict(await request.form())
    try:
        item = item_svc.register_item(db, user, form)
        db.commit()
    except ServiceError as e:
        db.rollback()
        flash(request, e.message, "error")
        ex = getattr(e, "existing_item_id", None)
        if ex:
            return to(f"/items/{ex}")
        return render(request, "intake.html", _intake_ctx(db, user, cert=form.get("cert_number", ""), form=form,
                                                           cat=form.get("category", ""),
                                                           flashes=request.session.pop("flash", [])), 400)
    flash(request, f"登録しました：{item.category_label} {item.key_label}（{item.format_label}）")
    return to(f"/intake?done={item.id}&cat={item.category}")


# ---- 在庫詳細 ----

@router.get("/items/{item_id}")
def item_detail(item_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = item_svc.get_item(db, item_id)
    listings = db.scalars(select(Listing).where(Listing.item_id == item.id).order_by(Listing.id.desc())).all()
    orders = db.scalars(select(Order).where(Order.item_id == item.id).order_by(Order.id.desc())).all()
    comments = db.scalars(select(Comment).where(Comment.target_type == "item", Comment.target_id == item.id)
                          .order_by(Comment.id)).all()
    logs = db.scalars(select(AuditLog).where(AuditLog.target_type == "item", AuditLog.target_id == str(item.id))
                      .order_by(AuditLog.id.desc()).limit(100)).all()
    old_photos = [p for p in item.photos if p.replaced_by_photo_id is not None]
    return render(request, "item_detail.html", {
        "user": user, "item": item, "listings": listings, "orders": orders, "comments": comments, "logs": logs,
        "show_price": item_svc.can_see_purchase_price(db, user, item), "old_photos": old_photos,
        "show_sensitive": item_svc.can_see_purchase_price(db, user, item),
        "quote": pricing.latest_approved(db, item), "draft": listing_svc.current_draft(db, item),
        "active_listing": next((x for x in listings if x.status == "active"), None),
        "IS": IS, "LF": LF, "users": {u.id: u for u in db.scalars(select(User))},
        "condition": listing_svc.raw_condition(db, item.condition),
        "conditions": get_setting(db, "raw_conditions") or []})


@router.post("/items/{item_id}/edit")
async def item_edit(item_id: int, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    item = item_svc.get_item(db, item_id)
    form = dict(await request.form())
    version = form_int(form.pop("version", None))
    if not item_svc.can_see_purchase_price(db, user, item):
        form.pop("purchase_price_jpy", None)
        form.pop("purchase_date", None)
    item_svc.update_item(db, user, item, form, version)
    db.commit()
    flash(request, "保存しました")
    return to(f"/items/{item.id}")


@router.post("/items/{item_id}/withdraw")
def item_withdraw(item_id: int, request: Request, reason: str = Form(""), version: str = Form(""),
                  user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    item = item_svc.get_item(db, item_id)
    item_svc.withdraw_item(db, user, item, reason, form_int(version))
    db.commit()
    flash(request, "取り下げました")
    return to(f"/items/{item.id}")


@router.post("/items/{item_id}/rollback")
def item_rollback(item_id: int, request: Request, to_status: str = Form(...), reason: str = Form(""),
                  version: str = Form(""), user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    item = item_svc.get_item(db, item_id)
    item_svc.rollback_item(db, user, item, to_status, reason, form_int(version))
    db.commit()
    flash(request, f"状態を {IS.LABELS[to_status]} に戻しました")
    return to(f"/items/{item.id}")


@router.post("/items/{item_id}/format")
def item_format(item_id: int, request: Request, listing_format: str = Form(...), version: str = Form(""),
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = item_svc.get_item(db, item_id)
    item_svc.set_listing_format(db, user, item, listing_format, form_int(version))
    db.commit()
    flash(request, f"出品形式を「{LF.LABELS[listing_format]}」に変更しました")
    return back(request, f"/items/{item.id}")


@router.post("/comments")
def add_comment(request: Request, target_type: str = Form(...), target_id: int = Form(...),
                body: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    if target_type not in ("item", "order") or not body.strip():
        raise ServiceError("コメントを入力してください")
    c = Comment(target_type=target_type, target_id=target_id, user_id=user.id, body=body.strip())
    db.add(c)
    db.flush()
    audit.log(db, audit.Actor.user(user), target_type, target_id, "comment.add", after={"body": c.body})
    if target_type == "order":
        from app.services.notify import enqueue_post
        enqueue_post(db, "orders", f"💬 {user.display_name}：{c.body}", order_id=target_id)
    db.commit()
    return back(request, f"/{target_type}s/{target_id}")


# ---- SKUラベル ----

@router.get("/labels")
def labels_page(request: Request, ids: str = "", psa: int = 0, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    """ids（カンマ区切り、または ids=1&ids=2）を指定するとその在庫、無ければラベル未印刷の在庫。"""
    raw = request.query_params.getlist("ids") or ([ids] if ids else [])
    id_list = [int(x) for part in raw for x in part.split(",") if x.strip().isdigit()]
    pending = label_svc.pending_items(db, include_psa=bool(psa))
    if id_list:
        found = {it.id: it for it in db.scalars(select(Item).where(Item.id.in_(id_list)))}
        items = [found[i] for i in id_list if i in found]
    else:
        items = pending
    return render(request, "labels.html", {"user": user, "items": items, "pending": pending, "psa": psa,
                                           "selected": bool(id_list), "qr": label_svc.qr_svg})


@router.post("/labels/printed")
async def labels_printed(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()
    id_list = [int(x) for x in form.getlist("item_id") if str(x).isdigit()]
    items = list(db.scalars(select(Item).where(Item.id.in_(id_list)))) if id_list else []
    n = label_svc.mark_printed(db, user, items)
    db.commit()
    return {"marked": n}


@router.get("/scan")
def scan_open(request: Request, code: str = "", dest: str = Query("item", alias="to"),
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    """ラベル（SKU の QR）や PSA のバーコードを読み取って在庫を開く。to=photos で撮影画面。"""
    item = item_svc.find_by_key(db, code)
    if not item:
        flash(request, f"{code} の在庫が見つかりません（SKU または cert番号）", "error")
        return back(request, "/items")
    return to(f"/photos/{item.id}" if dest == "photos" else f"/items/{item.id}")


# ---- 撮影（F0-2 / F6-4） ----

@router.get("/photos")
def photo_queue(request: Request, category: str = "", user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    def q(stmt):
        return stmt.where(Item.category == category) if category else stmt
    intake = db.scalars(q(select(Item).where(Item.status == IS.INTAKE)).order_by(Item.id)).all()
    retake = db.scalars(q(select(Item).where(Item.status.in_([IS.PHOTOGRAPHED, IS.PRICED, IS.READY])))
                        .order_by(Item.id.desc()).limit(50)).all()
    counts = dict(db.execute(select(Item.category, func.count()).where(Item.status == IS.INTAKE)
                             .group_by(Item.category)).all())
    return render(request, "photo_queue.html", {"user": user, "intake": intake, "retake": retake,
                                                "category": category, "Category": Category, "counts": counts})


@router.get("/photos/scan")
def photo_scan(request: Request, cert: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = item_svc.find_by_key(db, cert)
    if not item:
        flash(request, f"{cert} の在庫が見つかりません（cert番号またはSKU）。先に入荷登録してください", "error")
        return to("/photos")
    return to(f"/photos/{item.id}")


@router.get("/photos/{item_id}")
def photo_shoot(item_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = item_svc.get_item(db, item_id)
    # 次のレコードは同じ区分の未撮影から（区分ごとに撮影台の段取りが違うため）
    same = select(Item).where(Item.status == IS.INTAKE, Item.id != item.id, Item.category == item.category)
    nxt = db.scalar(same.where(Item.id > item.id).order_by(Item.id).limit(1)) or db.scalar(
        same.order_by(Item.id).limit(1))
    labels = {"front": "表（必須）", "back": "裏（必須）", **Category.PHOTO_LABELS.get(item.category, {})}
    slots = [(k, labels[k], k in item.required_photos()) for k in ("front", "back")]
    return render(request, "photo_shoot.html", {"user": user, "item": item, "next_item": nxt, "slots": slots})


def _photo_json(p: Photo) -> dict:
    return {"id": p.id, "kind": p.kind, "seq": p.seq, "status": p.process_status, "bg_removed": p.bg_removed,
            "url": ("/img/" + p.processed_path) if p.processed_path else None,
            "public_url": photo_svc.public_url(p) if p.processed_path else None, "error": p.process_error,
            "width": p.width, "height": p.height}


@router.get("/api/items/{item_id}/photos")
def api_photos(item_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = item_svc.get_item(db, item_id)
    return {"item_status": item.status, "item_status_label": IS.LABELS[item.status],
            "photos": [_photo_json(p) for p in item.active_photos()]}


@router.post("/api/items/{item_id}/photos")
async def api_upload_photo(item_id: int, kind: str = Form(...), file: UploadFile = File(...),
                           client_upload_id: str = Form(""), replace_photo_id: str = Form(""),
                           user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = item_svc.get_item(db, item_id)
    data = await file.read()
    p = photo_svc.save_upload(db, user, item, kind, data, file.filename or "upload.jpg",
                              client_upload_id=client_upload_id or None,
                              replace_photo_id=form_int(replace_photo_id))
    db.commit()
    db.refresh(item)
    return {"photo": _photo_json(p), "item_status": item.status, "item_status_label": IS.LABELS[item.status]}


@router.get("/media/original/{photo_id}")
def media_original(photo_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.get(Photo, photo_id)
    if not p:
        raise NotFound("写真が見つかりません")
    return FileResponse(get_config().images_dir / p.original_path)
