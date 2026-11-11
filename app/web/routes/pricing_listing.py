"""価格承認（F6-5）と出品準備・投入（F6-6）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters import get_listing_adapter, get_order_adapter
from app.adapters.manual import mark_listed
from app.db import get_db
from app.models import ExportBatch, Item, ItemStatus as IS, Listing, ListingDraft, ListingFormat as LF, User
from app.services import items as item_svc, listing as listing_svc, orders as order_svc, pricing
from app.services.errors import NotFound, ServiceError
from app.web.deps import current_user, flash, render, require_lv2
from app.web.routes.common import form_dt_jst, form_int, to

router = APIRouter()


# ---- 価格承認 ----

def _lane(v: str | None) -> str:
    return v if v in LF.ALL else LF.FIXED


def _lane_counts(db: Session) -> dict[str, int]:
    return {f: len(pricing.pending_items(db, f)) for f in LF.ALL}


@router.get("/pricing")
def pricing_queue(request: Request, lane: str = "", user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    counts = _lane_counts(db)
    if lane not in LF.ALL:
        # 指定がなければ承認待ちのあるレーンを開く
        lane = next((f for f in LF.ALL if counts[f]), LF.FIXED)
    pend = pricing.pending_items(db, lane)
    if pend:
        return to(f"/pricing/{pend[0].id}")
    return render(request, "pricing.html", {"user": user, "item": None, "queue": [], "lane": lane,
                                            "lane_counts": counts})


@router.get("/pricing/{item_id}")
def pricing_item(item_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = item_svc.get_item(db, item_id)
    lane = item.listing_format
    queue = pricing.pending_items(db, lane)
    q = None
    if item.status in (IS.PHOTOGRAPHED, IS.PRICED):
        q = pricing.ensure_open_quote(db, item) if (item.status == IS.PHOTOGRAPHED or pricing.open_quote(db, item)) else None
        db.commit()
    users = {u.id: u for u in db.scalars(select(User))}
    return render(request, "pricing.html", {
        "user": user, "item": item, "quote": q, "queue": queue, "users": users, "lane": lane,
        "lane_counts": _lane_counts(db), "approved": pricing.latest_approved(db, item),
        "show_price": item_svc.can_see_purchase_price(db, user, item)})


@router.post("/pricing/{item_id}/propose")
def pricing_propose(item_id: int, request: Request, start: str = Form(""), bin: str = Form(""),
                    note: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = item_svc.get_item(db, item_id)
    pricing.propose(db, user, item, start, bin or None, note=note or None)
    db.commit()
    flash(request, "価格案を提案しました。Lv2の承認を待っています")
    return to(f"/pricing/{item.id}")


def _next_pending(db: Session, after_id: int, lane: str) -> str:
    for it in pricing.pending_items(db, lane):
        if it.id != after_id:
            return f"/pricing/{it.id}"
    return f"/pricing?lane={lane}"


@router.post("/pricing/{item_id}/approve")
def pricing_approve(item_id: int, request: Request, start: str = Form(""), bin: str = Form(""),
                    version: str = Form(""), user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    item = item_svc.get_item(db, item_id)
    q = pricing.approve(db, user, item, start, bin or None, version=form_int(version))
    db.commit()
    if item.listing_format == LF.FIXED:
        price = f"販売価格 ${q.approved_start}"
    else:
        price = f"開始 ${q.approved_start}" + (f" / 即決 ${q.approved_bin}" if q.approved_bin else "")
    flash(request, f"承認しました（{item.format_label}）：{item.key_label} {price}"
                   + ("（自己承認として記録）" if q.self_approved else ""))
    return to(_next_pending(db, item.id, item.listing_format))


# ---- 出品準備・投入 ----

@router.get("/listings")
def listings_page(request: Request, lane: str = "", user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    lane = _lane(lane)
    priced = db.scalars(select(Item).where(Item.status == IS.PRICED, Item.listing_format == lane)
                        .order_by(Item.id)).all()
    priced_rows = [(it, listing_svc.current_draft(db, it), pricing.open_quote(db, it)) for it in priced]
    ready = listing_svc.ready_drafts(db, lane)
    batches = db.scalars(select(ExportBatch).where(ExportBatch.format == lane)
                         .order_by(ExportBatch.created_at.desc()).limit(10)).all()
    active = db.scalars(select(Listing).where(Listing.status == "active", Listing.format == lane)
                        .order_by(Listing.end_time, Listing.id)).all()
    counts = {f: {"priced": db.scalar(select(func.count()).select_from(Item).where(
                      Item.status == IS.PRICED, Item.listing_format == f)) or 0,
                  "ready": len(listing_svc.ready_drafts(db, f)),
                  "active": db.scalar(select(func.count()).select_from(Listing).where(
                      Listing.status == "active", Listing.format == f)) or 0} for f in LF.ALL}
    return render(request, "listings.html", {"user": user, "priced_rows": priced_rows, "ready": ready,
                                             "batches": batches, "active": active, "lane": lane, "counts": counts})


@router.post("/listings/generate")
async def listings_generate(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [i for i in (form_int(x) for x in form.getlist("item_id")) if i is not None]
    if not ids:
        raise ServiceError("在庫を選んでください")
    made = []
    for iid in ids:
        d = listing_svc.generate_draft(db, user, item_svc.get_item(db, iid))
        made.append(d)
    db.commit()
    flash(request, f"出品下書きを{len(made)}件作成しました。プレビューで確認してください")
    return to(f"/drafts/{made[0].id}" if len(made) == 1 else f"/listings?lane={made[0].format}")


@router.get("/drafts/{draft_id}")
def draft_preview(draft_id: int, request: Request, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    d = db.get(ListingDraft, draft_id)
    if not d:
        raise NotFound("下書きが見つかりません")
    item = item_svc.get_item(db, d.item_id)
    photos = sorted(item.active_photos(), key=lambda p: p.seq)
    return render(request, "draft_preview.html", {"user": user, "d": d, "item": item, "photos": photos,
                                                  "images": listing_svc.image_urls(item)})


@router.post("/drafts/{draft_id}/edit")
def draft_edit(draft_id: int, request: Request, title: str = Form(""), duration_days: str = Form(""),
               quantity: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = db.get(ListingDraft, draft_id)
    if not d:
        raise NotFound("下書きが見つかりません")
    listing_svc.update_draft(db, user, d, {"title": title, "duration_days": duration_days or None,
                                           "quantity": quantity or None})
    db.commit()
    flash(request, "保存しました")
    return to(f"/drafts/{d.id}")


@router.post("/drafts/{draft_id}/confirm")
def draft_confirm(draft_id: int, request: Request, version: str = Form(""), user: User = Depends(require_lv2),
                  db: Session = Depends(get_db)):
    """出品の最終確認（Lv2）。確認済みで READY。"""
    d = db.get(ListingDraft, draft_id)
    if not d:
        raise NotFound("下書きが見つかりません")
    listing_svc.confirm_draft(db, user, d, form_int(version))
    db.commit()
    flash(request, "確認済みにしました（出品準備完了）")
    return to(f"/listings?lane={d.format}")


@router.post("/listings/export")
async def listings_export(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [i for i in (form_int(x) for x in form.getlist("draft_id")) if i is not None]
    drafts = [d for d in (db.get(ListingDraft, i) for i in ids) if d]
    if not drafts:
        raise ServiceError("CSVに入れる在庫を選んでください")
    res = get_listing_adapter(db).submit(db, user, drafts)
    db.commit()
    flash(request, f"CSVを作成しました（{LF.LABELS[drafts[0].format]} {len(drafts)}件）。"
                   "ダウンロードして Seller Hub にアップロードしてください")
    return to(f"/listings?lane={drafts[0].format}&batch={res.batch_id}")


@router.get("/exports/{batch_id}.csv")
def export_download(batch_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = db.get(ExportBatch, batch_id)
    if not b:
        raise NotFound("CSVが見つかりません")
    return FileResponse(b.file_path, media_type="text/csv", filename=f"ebay-upload-{batch_id}.csv")


@router.post("/listings/import")
async def listings_import(request: Request, file: UploadFile = File(...), user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    data = await file.read()
    res = get_listing_adapter(db).import_result(db, user, data)
    db.commit()
    msg = f"取り込みました：出品中 {len(res.listed)}件 / 失敗 {len(res.failed)}件 / スキップ {len(res.skipped)}件"
    flash(request, msg, "ok" if not res.failed else "error")
    for key, err in res.failed + res.skipped:
        flash(request, f"{key}：{err}", "error")
    return to(f"/listings?lane={_lane(request.query_params.get('lane'))}")


@router.post("/drafts/{draft_id}/mark-listed")
def draft_mark_listed(draft_id: int, request: Request, ebay_item_id: str = Form(""),
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    """結果CSVが使えないときの代替：商品IDを手で入れて LISTED にする。"""
    d = db.get(ListingDraft, draft_id)
    if not d:
        raise NotFound("下書きが見つかりません")
    item = item_svc.get_item(db, d.item_id)
    if item.status != IS.READY:
        raise ServiceError("出品準備完了の在庫ではありません")
    mark_listed(db, user, d, ebay_item_id)
    db.commit()
    flash(request, f"出品中にしました（eBay {ebay_item_id}）")
    return to(f"/listings?lane={d.format}")


# ---- 出品中の記録（manual：落札・不落札） ----

def _listing(db: Session, listing_id: int) -> Listing:
    lst = db.get(Listing, listing_id)
    if not lst:
        raise NotFound("出品が見つかりません")
    return lst


@router.post("/listings/{listing_id}/sale")
def listing_sale(listing_id: int, request: Request, buyer: str = Form(""), amount: str = Form(""),
                 currency: str = Form("USD"), ship_country: str = Form(""), sold_at: str = Form(""),
                 ebay_order_id: str = Form(""), paid: str = Form(""), quantity: str = Form("1"),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    lst = _listing(db, listing_id)
    try:
        amt = pricing._dec(amount)
    except ServiceError:
        amt = None
    o = get_order_adapter(db).record_sale(db, user, lst, buyer=buyer, amount=amt, currency=currency or "USD",
                                          ship_country=ship_country, sold_at=form_dt_jst(sold_at),
                                          ebay_order_id=ebay_order_id or None, paid=bool(paid),
                                          quantity=form_int(quantity) or 1)
    db.commit()
    flash(request, "落札を記録しました。#orders にスレッドを作成します")
    return to(f"/orders/{o.id}")


@router.post("/listings/{listing_id}/unsold")
def listing_unsold(listing_id: int, request: Request, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    lst = _listing(db, listing_id)
    order_svc.record_unsold(db, user, lst)
    db.commit()
    flash(request, ("不落札を記録しました" if lst.format == LF.AUCTION else "出品終了を記録しました")
          + "。在庫は価格確定に戻り、再出品の値下げ案が出ています")
    return to(f"/pricing/{lst.item_id}")


@router.post("/listings/{listing_id}/end-time")
def listing_end_time(listing_id: int, request: Request, end_time: str = Form(""),
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app import audit
    lst = _listing(db, listing_id)
    before = lst.end_time
    lst.end_time = form_dt_jst(end_time)
    audit.log(db, audit.Actor.user(user), "listing", lst.id, "listing.end_time",
              before={"end_time": before}, after={"end_time": lst.end_time})
    db.commit()
    flash(request, "終了時刻を保存しました")
    return to(f"/items/{lst.item_id}")
