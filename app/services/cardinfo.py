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


def _psa_api(cert: str) -> dict[str, Any] | None:
    token = get_config().psa_api_token
    if not token:
        return None
    r = httpx.get(f"https://api.psacard.com/publicapi/cert/GetByCertNumber/{cert}",
                  headers={"Authorization": f"bearer {token}"}, timeout=8)
    r.raise_for_status()
    c = (r.json() or {}).get("PSACert") or {}
    if not c:
        return None
    return {
        "card_name_en": c.get("Subject"), "set_name": c.get("Brand"), "card_number": c.get("CardNumber"),
        "grade": _grade_number(c.get("CardGrade") or c.get("GradeDescription")),
        "psa_population": _int(c.get("TotalPopulation")), "year": c.get("Year"), "grader": "PSA",
    }


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
    return list(db.scalars(stmt.order_by(CardCatalog.set_id).limit(limit)).all())


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
        for name, fn in (("psa_api", _psa_api), ("psa_page", _psa_scrape)):
            try:
                data = fn(cert)
            except Exception as e:  # noqa: BLE001 - 失敗は手入力にフォールバック
                log.info("PSA lookup %s failed for %s: %s", name, cert, e)
                result["errors"].append(f"{name}: {e.__class__.__name__}")
                continue
            if data:
                fields = {k: v for k, v in data.items() if v is not None}
                result.update(source="psa", fields=fields)
                # 型番とカード名でモデルID候補を探す
                num = fields.get("card_number")
                if num:
                    result["candidates"] = [c for c in candidates(db, num)
                                            if c.card_number.lstrip("0") == str(num).lstrip("0")][:10]
                if len(result["candidates"]) == 1:
                    fields.update({k: v for k, v in _from_card_catalog(db, result["candidates"][0].model_id).items() if v})
                return result
    return result
