"""SKUラベルの印刷と、読み取りで在庫を開く機能。"""
from __future__ import annotations

import re

from tests.test_categories import intake


def sheet_ids(html: str) -> set[int]:
    return {int(x) for x in re.findall(r'class="lbl" data-id="(\d+)"', html)}
from tests.test_flow import done_id, register


def test_label_sheet_and_mark_printed(admin_client, db, users):
    from app.models import Item
    single = done_id(intake(admin_client, category="single", model_id="SV2a-201", condition="nm",
                            card_name_jp="リザードンex"))
    box = done_id(intake(admin_client, category="box", card_name_en="151 Booster Box", set_id="SV2a"))
    psa = done_id(register(admin_client, cert="31313131"))

    # 未印刷の一覧：PSA は既定で除く
    page = admin_client.get("/labels").text
    assert sheet_ids(page) == {single, box} and page.count("<svg") == 2
    assert f"S-{single:05d}" in page and f"X-{box:05d}" in page
    assert sheet_ids(admin_client.get("/labels?psa=1").text) == {single, box, psa}
    # 入荷画面に未印刷の件数とリンク
    assert "ラベル未印刷 2件" in admin_client.get(f"/intake?done={single}").text

    # 指定した在庫だけ（在庫一覧のチェックボックスは ids=1&ids=2 で来る）
    assert sheet_ids(admin_client.get(f"/labels?ids={box}&ids={psa}").text) == {box, psa}
    assert sheet_ids(admin_client.get(f"/labels?ids={single},{box}").text) == {single, box}

    # 印刷済みにすると未印刷の一覧から消える
    r = admin_client.post("/labels/printed", data={"item_id": [str(single), str(box)]})
    assert r.json() == {"marked": 2}
    db.expire_all()
    assert db.get(Item, single).label_printed_at is not None
    assert "印刷が必要な在庫はありません" in admin_client.get("/labels").text


def test_qr_contains_sku():
    import segno
    from app.services.labels import qr_svg
    assert not segno.make("S-00012", error="m", micro=False).is_micro
    svg = str(qr_svg("S-00012"))
    assert svg.startswith("<svg") and "viewBox" in svg and "width=" not in svg.split(">")[0]


def test_scan_opens_item(admin_client, db, users):
    single = done_id(intake(admin_client, category="single", model_id="SV2a-201", condition="nm"))
    psa = done_id(register(admin_client, cert="42424242"))
    sku = f"S-{single:05d}"
    assert admin_client.get(f"/scan?code={sku}").headers["location"] == f"/items/{single}"
    assert admin_client.get(f"/scan?code={sku.lower()}&to=photos").headers["location"] == f"/photos/{single}"
    # PSA ラベルの QR（URL）でも開ける
    r = admin_client.get("/scan?code=https://www.psacard.com/cert/42424242/psa")
    assert r.headers["location"] == f"/items/{psa}"
    r = admin_client.get("/scan?code=NOPE-1", headers={"referer": "https://testserver/items"})
    assert r.status_code == 303 and r.headers["location"] == "https://testserver/items"


def test_psa_intake_has_number_search(admin_client, users):
    page = admin_client.get("/intake").text
    assert 'id="psa-num-q"' in page and "wireSearch('psa-num'" in page
