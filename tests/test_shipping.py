from __future__ import annotations

from decimal import Decimal


def _sold(db, admin, sku, ebay_id, box="A", slot="1", paid=True):
    from app.adapters.manual import ManualOrderAdapter
    from app.models import Item, Listing
    it = Item(sku=sku, model_id="SV1-001", status="LISTED", storage_box=box, storage_slot=slot,
              card_name_jp=f"カード{sku}", purchase_price_jpy=1000)
    db.add(it)
    db.flush()
    lst = Listing(item_id=it.id, title=f"Card {sku}", format="auction", status="active", ebay_item_id=ebay_id)
    db.add(lst)
    db.flush()
    return ManualOrderAdapter().record_sale(db, admin, lst, buyer="buyer1", amount=Decimal("100"),
                                            currency="USD", ship_country="US", paid=paid)


def test_shipping_pages_and_pick_verify(admin_client, db, users):
    from app.models import ChecklistItem
    admin = users["admin"]
    o1 = _sold(db, admin, "S-00101", "700000000001", box="B", slot="10")
    o2 = _sold(db, admin, "S-00102", "700000000002", box="A", slot="2")
    o3 = _sold(db, admin, "S-00103", "700000000003", paid=False)
    db.commit()
    ids = (o1.id, o2.id, o3.id)

    r = admin_client.get("/shipping")
    assert r.status_code == 200 and "S-00101" in r.text and "支払待ち" in r.text
    r = admin_client.get("/shipping/picklist?scope=all")
    assert r.status_code == 200
    assert r.text.index("S-00102") < r.text.index("S-00101")  # 保管場所の順（A → B）
    r = admin_client.get(f"/orders/{ids[0]}/packing-slip")
    assert r.status_code == 200 and "Thank you for your purchase" in r.text and "S-00101" in r.text

    # 別の在庫を読み取ると警告、正しい在庫ならピッキング済み
    admin_client.post(f"/orders/{ids[0]}/verify-pick", data={"code": "S-00102"})
    r = admin_client.get(f"/orders/{ids[0]}")
    assert "別の在庫です" in r.text
    admin_client.post(f"/orders/{ids[0]}/verify-pick", data={"code": "s-00101"})
    db.expire_all()
    assert db.query(ChecklistItem).filter_by(order_id=ids[0], item_key="picked").count() == 1

    # 発送ページのスキャン → 注文を開いて照合済み
    r = admin_client.get("/shipping/scan?code=S-00102")
    assert r.status_code == 303 and r.headers["location"] == f"/orders/{ids[1]}"
    db.expire_all()
    assert db.query(ChecklistItem).filter_by(order_id=ids[1], item_key="picked").count() == 1


def test_actual_shipping_cost_used_for_profit(admin_client, db, users):
    from app.models import Order
    from app.services import orders as order_svc
    o = _sold(db, users["admin"], "S-00201", "700000000011")
    db.commit()
    est = order_svc.gross_profit(db, o)
    assert est and not est["shipping_actual"]
    r = admin_client.post(f"/orders/{o.id}/shipping-cost", data={"cost": "3,000"})
    assert r.status_code == 303
    db.expire_all()
    o = db.get(Order, o.id)
    gp = order_svc.gross_profit(db, o)
    assert o.shipping_cost_jpy == 3000 and gp["shipping_actual"] and gp["shipping_jpy"] == 3000
    assert gp["gross_profit_jpy"] == est["gross_profit_jpy"] + est["shipping_jpy"] - 3000
