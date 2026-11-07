from __future__ import annotations

from decimal import Decimal

HEADER = ("Sales Record Number,Order Number,Buyer Username,Buyer Name,Ship To Country,Item Number,Item Title,"
          "Custom Label,Quantity,Sold For,Shipping And Handling,Total Price,Sale Date,Paid On Date,Ship By Date")


def report(*rows: str) -> bytes:
    lines = ["", HEADER, *rows, "", "3 record(s) downloaded,from Nov-01-26 to Nov-07-26", "Seller ID : torelis"]
    return ("﻿" + "\n".join(lines)).encode("utf-8")


def _listed(db, sku, ebay_id, fmt="auction", qty=1):
    from app.models import Item, Listing
    it = Item(sku=sku, model_id="SV1-001", status="LISTED", quantity=qty, listing_format=fmt)
    db.add(it)
    db.flush()
    lst = Listing(item_id=it.id, title=sku, format=fmt, status="active", ebay_item_id=ebay_id, quantity=qty)
    db.add(lst)
    db.flush()
    return it, lst


def test_import_orders_report(db, users):
    from app.models import Order, OrderStatus as OS
    from app.services.order_import import import_orders_report
    admin = users["admin"]
    a, la = _listed(db, "S-00001", "111111111111")                   # オークション・支払済み
    b, lb = _listed(db, "S-00002", "222222222222", fmt="fixed", qty=3)  # 通常出品・まとめ買い・未払い
    c, lc = _listed(db, "S-00003", "333333333333", fmt="fixed")       # まとめ買いのもう1件
    db.commit()
    data = report(
        '1001,12-11111-11111,buyer_us,John,United States,111111111111,Card A,S-00001,1,$120.50,$0.00,'
        '$120.50,Nov-05-26,Nov-05-26,Nov-07-26',
        '1002,12-22222-22222,buyer_gb,Jane,United Kingdom,,,,,,,$95.00,Nov-06-26,,',   # まとめ買いの合計行
        '1002,12-22222-22222,buyer_gb,Jane,United Kingdom,222222222222,Card B,S-00002,2,$30.00,$10.00,'
        ',Nov-06-26,,',
        '1002,12-22222-22222,buyer_gb,Jane,United Kingdom,333333333333,Card C,S-00003,1,$25.00,$0.00,'
        ',Nov-06-26,,',
        '1003,12-33333-33333,someone,X,Narnia,999999999999,Other,,1,$5.00,$0.00,$5.00,Nov-06-26,,',
    )
    res = import_orders_report(db, admin, data)
    db.commit()
    assert len(res.created) == 3 and len(res.paid) == 1
    assert [lbl for lbl, _ in res.skipped] == ["12-33333-33333 999999999999"]

    oa = db.query(Order).filter_by(ebay_order_id="12-11111-11111").one()
    assert oa.status == OS.AWAITING_SHIPMENT and oa.total_amount == Decimal("120.50") and oa.ship_country == "US"
    assert oa.source == "report" and oa.ship_by is not None
    ob = db.query(Order).filter_by(ebay_order_id="12-22222-22222/222222222222").one()
    assert ob.quantity == 2 and ob.total_amount == Decimal("70.00") and ob.ship_country == "GB"
    assert ob.status == OS.AWAITING_PAYMENT
    db.refresh(b)
    db.refresh(a)
    assert b.remaining == 1 and b.status == "LISTED" and a.status == "SOLD"

    # 同じレポートをもう一度：二重登録しない。支払日が入っていれば支払を記録
    data2 = report(
        '1002,12-22222-22222,buyer_gb,Jane,United Kingdom,222222222222,Card B,S-00002,2,$30.00,$10.00,'
        ',Nov-06-26,Nov-07-26,Nov-11-26',
        '1001,12-11111-11111,buyer_us,John,United States,111111111111,Card A,S-00001,1,$120.50,$0.00,'
        '$120.50,Nov-05-26,Nov-05-26,Nov-07-26',
    )
    res2 = import_orders_report(db, admin, data2)
    db.commit()
    assert not res2.created and len(res2.paid) == 1 and len(res2.unchanged) == 1
    db.refresh(ob)
    assert ob.status == OS.AWAITING_SHIPMENT
    assert db.query(Order).count() == 3


def test_import_links_manually_recorded_order(db, users):
    from app.adapters.manual import ManualOrderAdapter
    from app.models import Order
    from app.services.order_import import import_orders_report
    admin = users["admin"]
    it, lst = _listed(db, "S-00009", "444444444444")
    ManualOrderAdapter().record_sale(db, admin, lst, buyer="b", amount=Decimal("50"), currency="USD",
                                     ship_country="US")
    db.commit()
    res = import_orders_report(db, admin, report(
        '1,12-44444-44444,b,B,US,444444444444,Card,S-00009,1,$50.00,$0.00,$50.00,Nov-06-26,Nov-06-26,'))
    db.commit()
    assert len(res.linked) == 1 and len(res.paid) == 1 and not res.created
    assert db.query(Order).one().ebay_order_id == "12-44444-44444"


def test_import_rejects_wrong_file(db, users):
    import pytest
    from app.services.errors import ServiceError
    from app.services.order_import import import_orders_report
    with pytest.raises(ServiceError):
        import_orders_report(db, users["admin"], b"foo,bar\n1,2\n")


def test_import_route(admin_client, users):
    r = admin_client.post("/orders/import", files={"file": ("orders.csv", report(), "text/csv")})
    assert r.status_code == 303
    r = admin_client.get("/orders")
    assert "eBayの注文レポートを取り込む" in r.text


def test_partial_multi_order_then_full(db, users):
    """まとめ買いの片方だけが先に取り込まれても、あとで全体を取り込めばもう片方も記録される。"""
    from app.models import Order
    from app.services.order_import import import_orders_report
    admin = users["admin"]
    _listed(db, "S-00011", "555555555555", fmt="fixed")
    _listed(db, "S-00012", "666666666666", fmt="fixed")
    db.commit()
    a = '1,12-55555-55555,b,B,US,555555555555,A,S-00011,1,$10.00,$0.00,,Nov-06-26,,'
    b = '1,12-55555-55555,b,B,US,666666666666,B,S-00012,1,$12.00,$0.00,,Nov-06-26,,'
    import_orders_report(db, admin, report(a))
    res = import_orders_report(db, admin, report(a, b))
    db.commit()
    assert len(res.created) == 1 and len(res.unchanged) == 1
    assert db.query(Order).count() == 2
