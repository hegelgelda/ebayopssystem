"""cert番号からカード情報を引く（F0-1）。

①自社Courtyardカタログ（cert_catalog）②PSA（Public API → cert検証ページ）③手入力、の順。
取得した値は画面で人が確認してから確定する。
"""
from __future__ import annotations

import html as htmllib
import logging
import re
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import get_config
from app.models import CardCatalog, CertCatalog

log = logging.getLogger(__name__)

CERT_RE = re.compile(r"(\d{7,10})")


def normalize_cert(raw: str) -> str:
    """バーコード／QR（https://www.psacard.com/cert/12345678 等）から cert番号を取り出す。"""
    raw = (raw or "").strip()
    m = re.search(r"/cert/(\d+)", raw)
    if m:
        return m.group(1)
    digits = re.sub(r"\D", "", raw)
    return digits


def make_model_id(set_id: str | None, card_number: str | None) -> str | None:
    if not set_id or not card_number:
        return None
    return f"{set_id.strip()}-{card_number.strip()}"


def _from_card_catalog(db: Session, model_id: str | None) -> dict[str, Any]:
    if not model_id:
        return {}
    cc = db.get(CardCatalog, model_id)
    if not cc:
        return {}
    return {"model_id": cc.model_id, "set_id": cc.set_id, "card_number": cc.card_number,
            "card_name_jp": cc.name_jp, "card_name_en": cc.name_en, "set_name": cc.set_name}


class PSALookupError(Exception):
    """PSA API の失敗（利用者に見せる日本語の理由つき）。"""


def _ci(d: dict, *names):
    """大文字小文字・区切りの違いを気にせずキーを引く（PSA の応答の表記ゆれ対策）。"""
    norm = {re.sub(r"[^a-z]", "", k.lower()): v for k, v in (d or {}).items()}
    for n in names:
        v = norm.get(re.sub(r"[^a-z]", "", n.lower()))
        if v not in (None, ""):
            return v
    return None


def _psa_api(cert: str) -> dict[str, Any] | None:
    """PSA Public API（無料は1日100回）。トークンは .env の PSA_API_TOKEN。"""
    token = get_config().psa_api_token
    if not token:
        return None
    r = httpx.get(f"https://api.psacard.com/publicapi/cert/GetByCertNumber/{cert}",
                  headers={"Authorization": f"bearer {token}"}, timeout=10)
    if r.status_code == 403 and "approved" in r.text.lower():
        # 2026-11 時点で PSA Public API は承認制（"Access to this API is limited to approved customers."）
        raise PSALookupError("PSA API は承認された利用者のみ使えます（PSA_API_TOKEN を削除すると、この確認をしなくなります）")
    if r.status_code in (401, 403):
        raise PSALookupError("PSA API のトークンが無効か期限切れです（PSA_API_TOKEN を更新してください）")
    if r.status_code == 429:
        raise PSALookupError("PSA API の1日の上限に達しました（明日また使えます）")
    if r.status_code == 404:
        raise PSALookupError("PSA にこの cert番号が見つかりません")
    r.raise_for_status()
    js = r.json() or {}
    c = _ci(js, "PSACert") or {}
    if not c:
        msg = _ci(js, "ServerMessage") or "cert番号が見つかりません"
        raise PSALookupError(f"PSA：{msg}")
    return {
        "card_name_en": _title_case(_ci(c, "Subject")), "psa_brand": _ci(c, "Brand"),
        "card_number": _ci(c, "CardNumber"), "psa_variety": _ci(c, "Variety"),
        "grade": _grade_number(_ci(c, "CardGrade", "GradeDescription")),
        "psa_population": _int(_ci(c, "TotalPopulation")), "year": _ci(c, "Year"), "grader": "PSA",
    }


def _title_case(v):
    """PSA は "IRON JUGULIS" のように大文字なので、読みやすく直す（ex / V / VMAX などは残す）。"""
    if not v or not isinstance(v, str) or not v.isupper():
        return v
    keep = {"EX": "ex", "GX": "GX", "V": "V", "VMAX": "VMAX", "VSTAR": "VSTAR", "LV.X": "LV.X", "BREAK": "BREAK"}
    return " ".join(keep.get(w, w.capitalize()) for w in v.split())


def match_psa_to_catalog(db: Session, brand: str | None, card_number: str | None) -> CardCatalog | None:
    """PSA の Brand（例 "POKEMON JAPANESE SV4M-FUTURE FLASH"）に含まれるセットIDと型番から、カード一覧のカードを特定する。"""
    if not brand or not card_number:
        return None
    up = brand.upper()
    n = normalize_card_number(str(card_number))
    sets = [sid for (sid,) in db.execute(select(CardCatalog.set_id).distinct()) if sid]
    hits = [sid for sid in sets if re.search(rf"(?<![A-Z0-9]){re.escape(sid.upper())}(?![A-Z0-9])", up)]
    for sid in sorted(hits, key=len, reverse=True):  # 長いセットID（SV4M など）を優先
        card = db.scalar(select(CardCatalog).where(
            CardCatalog.set_id == sid, func.upper(func.ltrim(CardCatalog.card_number, "0")) == n))
        if card:
            return card
    return None


def _int(v) -> int | None:
    try:
        return int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _grade_number(v: str | None) -> str | None:
    if not v:
        return None
    m = re.search(r"(\d+(?:\.\d)?)", str(v))
    return m.group(1) if m else str(v)


def parse_psa_cert_html(page: str) -> dict[str, Any]:
    """PSA cert検証ページの「ラベル → 値」の組を拾う（ページ構造の変更に備えて緩く読む）。"""
    pairs: dict[str, str] = {}
    for m in re.finditer(r"<(dt|th)[^>]*>(.*?)</\1>\s*<(dd|td)[^>]*>(.*?)</\3>", page, re.S | re.I):
        k = re.sub(r"<[^>]+>", " ", m.group(2))
        v = re.sub(r"<[^>]+>", " ", m.group(4))
        pairs[" ".join(htmllib.unescape(k).split()).lower()] = " ".join(htmllib.unescape(v).split())
    if not pairs:
        return {}

    def pick(*names):
        for n in names:
            for k, v in pairs.items():
                if k == n or k.startswith(n):
                    return v
        return None

    return {
        "card_name_en": pick("subject", "player"),
        "set_name": pick("brand/title", "brand", "set"),
        "card_number": pick("card number", "card #"),
        "grade": _grade_number(pick("item grade", "grade")),
        "psa_population": _int(pick("psa population", "population")),
        "year": pick("year"),
        "grader": "PSA",
    }


def _psa_scrape(cert: str) -> dict[str, Any] | None:
    r = httpx.get(f"https://www.psacard.com/cert/{cert}/psa", timeout=8, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 (compatible; ebay-ops-tool)"})
    if r.status_code in (403, 429, 503):
        # PSA のサイトは Cloudflare の Bot 対策で自動取得を受け付けない（2026-11 確認）
        if get_config().psa_api_token:
            return None
        raise PSALookupError("PSA のサイトからは自動取得できません。PSA API のトークン（PSA_API_TOKEN）を設定すると自動で入ります")
    if r.status_code != 200:
        return None
    data = parse_psa_cert_html(r.text)
    return data or None


def normalize_card_number(raw: str) -> str:
    """「201/165」「No.201」「 0201 」→「201」。英字付き（SV-P 001 等）は大文字にしてそのまま。"""
    s = (raw or "").strip().upper()
    s = re.sub(r"^NO\.?\s*", "", s)
    s = s.split("/", 1)[0].strip()
    return s.lstrip("0") or s


def search_by_number(db: Session, number: str, set_q: str = "", limit: int = 30) -> list[CardCatalog]:
    """シングル用：型番で候補を出す。セット（ID・名前の一部）で絞り込める。"""
    n = normalize_card_number(number)
    if not n:
        return []
    stmt = select(CardCatalog).where(or_(
        func.upper(func.ltrim(CardCatalog.card_number, "0")) == n,
        func.upper(CardCatalog.card_number).like(f"{n}/%"),
        func.upper(func.ltrim(CardCatalog.card_number, "0")).like(f"{n}/%"),
    ))
    set_q = (set_q or "").strip()
    if set_q:
        like = f"%{set_q}%"
        stmt = stmt.where(or_(CardCatalog.set_id.ilike(like), CardCatalog.set_name.ilike(like),
                              CardCatalog.set_name_jp.ilike(like)))
    rows = list(db.scalars(stmt.order_by(CardCatalog.set_id).limit(max(limit * 8, 200))).all())
    # 同じ弾・同じ番号（"1" と "001"）は1件にまとめ、英語名・レアリティのある行を残す
    best: dict[tuple[str, str], CardCatalog] = {}
    for c in rows:
        k = (c.set_id, normalize_card_number(c.card_number))
        cur = best.get(k)
        if cur is None or _richness(c) > _richness(cur):
            best[k] = c
    from app.settings_store import get_setting
    release = get_setting(db, "catalog_set_release") or {}
    # 新しい弾を先に（発売日が分からない弾は後ろ）
    out = sorted(best.values(), key=lambda c: (release.get(c.set_id, ""), c.set_id), reverse=True)
    return out[:limit]


def _richness(c: CardCatalog) -> int:
    return sum(1 for v in (c.name_en, c.rarity, c.image_url, c.name_jp) if v)


def search_sets(db: Session, q: str = "", limit: int = 30) -> list[dict[str, str | None]]:
    """パック・BOX用：カード一覧に登録されているセットの一覧。"""
    stmt = (select(CardCatalog.set_id, func.max(CardCatalog.set_name), func.max(CardCatalog.set_name_jp))
            .group_by(CardCatalog.set_id))
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(CardCatalog.set_id.ilike(like), CardCatalog.set_name.ilike(like),
                              CardCatalog.set_name_jp.ilike(like)))
    return [{"set_id": sid, "set_name": name, "set_name_jp": jp if jp != name else None}
            for sid, name, jp in db.execute(stmt.order_by(CardCatalog.set_id).limit(limit))]


def candidates(db: Session, q: str, limit: int = 20) -> list[CardCatalog]:
    q = (q or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    return list(db.scalars(select(CardCatalog).where(or_(
        CardCatalog.model_id.ilike(like), CardCatalog.name_jp.ilike(like), CardCatalog.name_en.ilike(like),
        CardCatalog.set_name.ilike(like), CardCatalog.card_number.ilike(like))).limit(limit)).all())


def lookup(db: Session, cert: str, *, allow_network: bool = True) -> dict[str, Any]:
    """戻り値: {"source": courtyard|psa|manual, "fields": {...}, "candidates": [...], "errors": [...]}"""
    result: dict[str, Any] = {"source": "manual", "fields": {"grader": "PSA"}, "candidates": [], "errors": []}
    cc = db.get(CertCatalog, cert)
    if cc:
        f = {"model_id": cc.model_id, "card_name_en": cc.card_name, "set_name": cc.set_name,
             "card_number": cc.card_number, "grader": cc.grader or "PSA", "grade": cc.grade}
        if cc.model_id and "-" in cc.model_id:
            sid, num = cc.model_id.split("-", 1)
            f.setdefault("set_id", sid)
            f["set_id"] = sid
            f["card_number"] = f.get("card_number") or num
        f.update({k: v for k, v in _from_card_catalog(db, cc.model_id).items() if v})
        result.update(source="courtyard", fields={k: v for k, v in f.items() if v is not None})
        return result

    if allow_network:
        from app.services import health
        # PSA のサイト（cert ページ）は Cloudflare の Bot 対策で自動取得できないため問い合わせない。
        # PSA Public API はトークンがあるときだけ使う（承認制）。無ければ型番検索とグレードの手入力で登録する。
        for name, fn in (("psa_api", _psa_api),):
            try:
                data = fn(cert)
                if name == "psa_api" and get_config().psa_api_token:
                    health.record(db, "psa-api", ok=True)
            except PSALookupError as e:
                result["errors"].append(str(e))
                if "期限切れ" in str(e):
                    health.record(db, "psa-api", ok=False, error=str(e))
                continue
            except Exception as e:  # noqa: BLE001 - 失敗は手入力にフォールバック
                log.info("PSA lookup %s failed for %s: %s", name, cert, e)
                if name == "psa_api":
                    health.record(db, "psa-api", ok=False, error=str(e))
                    result["errors"].append(f"PSA API に接続できません（{e.__class__.__name__}）")
                continue
            if data:
                brand, variety = data.pop("psa_brand", None), data.pop("psa_variety", None)
                fields = {k: v for k, v in data.items() if v is not None}
                result.update(source="psa", fields=fields, psa={"brand": brand, "variety": variety})
                card = match_psa_to_catalog(db, brand, fields.get("card_number"))
                if card:
                    # カード一覧で特定できた：モデルID・日本語名・英語名・セットを埋める
                    fields.update({k: v for k, v in _from_card_catalog(db, card.model_id).items() if v})
                    result["matched"] = card.model_id
                else:
                    if brand:
                        fields.setdefault("set_name", brand)
                    num = fields.get("card_number")
                    if num:
                        result["candidates"] = search_by_number(db, str(num))[:10]
                return result
    return result
    return result
