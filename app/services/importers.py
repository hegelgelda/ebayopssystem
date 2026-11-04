"""既存データの取り込み（8章）：価格参考値CSV、Courtyardカタログ、CARDS定数。いずれも冪等。"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CardCatalog, CertCatalog, MarketPrice
from app.services.cardinfo import make_model_id

SOURCES = {"courtyard_buyback", "courtyard_p2p", "pokeca", "snkrdunk", "pricecharting"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _dec(v) -> Decimal | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("$", "").replace("¥", "").replace("%", "")
    if s in ("", "-", "nan", "NaN", "None"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _int(v) -> int | None:
    d = _dec(v)
    return int(d) if d is not None else None


def upsert_market_price(db: Session, model_id: str, source: str, metric: str, value, currency: str | None,
                        count, as_of: date) -> bool:
    row = db.scalar(select(MarketPrice).where(MarketPrice.model_id == model_id, MarketPrice.source == source,
                                              MarketPrice.metric == metric, MarketPrice.as_of == as_of))
    new = row is None
    if new:
        row = MarketPrice(model_id=model_id, source=source, metric=metric, as_of=as_of)
        db.add(row)
    row.value, row.currency, row.count = _dec(value), currency, _int(count)
    return new


def import_market_long(db: Session, path: Path) -> tuple[int, int]:
    """縦持ち形式：model_id, source, metric, value, currency, count, as_of"""
    ins = upd = 0
    for r in _read_csv(path):
        src = (r.get("source") or "").strip()
        if src not in SOURCES:
            raise ValueError(f"不明な source: {src}")
        if upsert_market_price(db, r["model_id"].strip(), src, (r.get("metric") or "value").strip(), r.get("value"),
                               (r.get("currency") or "").strip() or None, r.get("count"),
                               date.fromisoformat(r["as_of"].strip())):
            ins += 1
        else:
            upd += 1
    return ins, upd


def import_market_mapped(db: Session, path: Path, mapping: dict) -> tuple[int, int]:
    """横持ちCSVを列マッピング（config/market_import.json の1エントリ）で取り込む。

    mapping = {"model_id": "列名", "as_of": "列名" or {"value": "2026-10-31"},
               "metrics": [{"source": .., "metric": .., "value": "列名", "currency": "USD", "count": "列名"}]}
    """
    ins = upd = 0
    for r in _read_csv(path):
        mid = (r.get(mapping["model_id"]) or "").strip()
        if not mid:
            continue
        a = mapping.get("as_of")
        as_of = date.fromisoformat(a["value"]) if isinstance(a, dict) else date.fromisoformat(r[a].strip()[:10])
        for m in mapping["metrics"]:
            if m["source"] not in SOURCES:
                raise ValueError(f"不明な source: {m['source']}")
            v = r.get(m["value"])
            if _dec(v) is None:
                continue
            if upsert_market_price(db, mid, m["source"], m.get("metric", "value"), v, m.get("currency"),
                                   r.get(m["count"]) if m.get("count") else None, as_of):
                ins += 1
            else:
                upd += 1
    return ins, upd


def import_cert_catalog(db: Session, path: Path, cols: dict | None = None) -> int:
    """Courtyardカタログ：cert番号 → モデルID ほか。列名は cols で変更できる。"""
    c = {"cert_number": "cert_number", "model_id": "model_id", "card_name": "card_name", "set_name": "set_name",
         "card_number": "card_number", "grader": "grader", "grade": "grade", **(cols or {})}
    n = 0
    for r in _read_csv(path):
        cert = re.sub(r"\D", "", r.get(c["cert_number"]) or "")
        if not cert:
            continue
        row = db.get(CertCatalog, cert) or CertCatalog(cert_number=cert)
        for k in ("model_id", "card_name", "set_name", "card_number", "grader", "grade"):
            v = (r.get(c[k]) or "").strip() or None
            setattr(row, k, v)
        db.merge(row)
        n += 1
    return n


def _js_literal_to_json(s: str) -> str:
    """CARDS 定数（JSオブジェクト/配列リテラル）をJSONに寄せる。"""
    s = re.sub(r"//[^\n]*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"'((?:[^'\\]|\\.)*)'", lambda m: json.dumps(m.group(1).replace("\\'", "'"), ensure_ascii=False), s)
    s = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', s)
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def extract_cards_constant(html: str) -> list[dict]:
    m = re.search(r"(?:const|let|var)\s+CARDS\s*=\s*", html)
    if not m:
        raise ValueError("CARDS 定数が見つかりません")
    start = m.end()
    opener = html[start]
    closer = {"[": "]", "{": "}"}[opener]
    depth, i, in_str, q = 0, start, False, ""
    while i < len(html):
        ch = html[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == q:
                in_str = False
        elif ch in "'\"`":
            in_str, q = True, ch
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                break
        i += 1
    data = json.loads(_js_literal_to_json(html[start:i + 1]))
    if isinstance(data, dict):
        data = [{"model_id": k, **(v if isinstance(v, dict) else {"name_jp": v})} for k, v in data.items()]
    return data


def import_cards(db: Session, path: Path) -> int:
    """モデルID → 日本語名/英語名。HTML（CARDS定数）・JSON・CSV に対応。"""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in (".html", ".htm", ".js"):
        rows = extract_cards_constant(text)
    elif path.suffix.lower() == ".json":
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = [{"model_id": k, **v} for k, v in rows.items()]
    else:
        rows = list(csv.DictReader(io.StringIO(text)))

    def pick(r: dict, *names):
        for n in names:
            if r.get(n) not in (None, ""):
                return str(r[n]).strip()
        return None

    n = 0
    for r in rows:
        set_id = pick(r, "set_id", "setId", "set", "series")
        num = pick(r, "card_number", "number", "no", "num", "cardNumber")
        mid = pick(r, "model_id", "modelId", "id", "key") or make_model_id(set_id, num)
        if not mid:
            continue
        if (not set_id or not num) and "-" in mid:
            sid, nn = mid.split("-", 1)
            set_id, num = set_id or sid, num or nn
        row = db.get(CardCatalog, mid) or CardCatalog(model_id=mid, set_id=set_id or "", card_number=num or "")
        row.set_id, row.card_number = set_id or row.set_id, num or row.card_number
        row.name_jp = pick(r, "name_jp", "nameJp", "jp", "name_ja", "name") or row.name_jp
        row.name_en = pick(r, "name_en", "nameEn", "en", "english") or row.name_en
        row.set_name = pick(r, "set_name", "setName", "set_name_en") or row.set_name
        db.merge(row)
        n += 1
    return n
