"""既存データの取り込み（8章）：価格参考値CSV、Courtyardカタログ、CARDS定数。いずれも冪等。"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin

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


# ---- tcg-catalog（https://hegelgelda.github.io/tcg-catalog/）----
# ページに埋め込まれた SERIES（セット一覧）と CARDS（{セットID: [{n, en, jp, r}]}）を読む。

_JA = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]")


def has_japanese(s: str | None) -> bool:
    """ひらがな・カタカナ・漢字を含むか（"Poké Pad" や "Nidoran♀" は英語名として扱う）。"""
    return bool(s and _JA.search(s))


LIMITLESS_IMG = "https://limitlesstcg.nyc3.cdn.digitaloceanspaces.com/tpc/{s}/{s}_{n}_R_JP_LG.png"


def _js_const(html: str, name: str):
    m = re.search(rf"(?:const|let|var)\s+{name}\s*=\s*", html)
    if not m:
        return None
    rest = html[m.end():]
    try:
        return json.JSONDecoder().raw_decode(rest)[0]
    except json.JSONDecodeError:
        if name == "CARDS":
            return extract_cards_constant(html)
        return None


def is_tcg_catalog(html: str) -> bool:
    cards = _js_const(html, "CARDS")
    return isinstance(cards, dict) and bool(cards) and all(isinstance(v, list) for v in list(cards.values())[:5]) \
        and _js_const(html, "SERIES") is not None


def parse_tcg_catalog(html: str, base_url: str = "") -> list[dict]:
    series = _js_const(html, "SERIES") or []
    cards = _js_const(html, "CARDS") or {}
    sets = {st["id"]: st for se in series for st in se.get("sets", [])}
    m = re.search(r"PROMO_SETS\s*=\s*new\s+Set\((\[.*?\])\)", html, re.S)
    promo = set(json.loads(m.group(1))) if m else set()
    rows = []
    for sid, lst in cards.items():
        st = sets.get(sid, {})
        for c in lst:
            n = str(c.get("n", "")).strip()
            if not n:
                continue
            en = (c.get("en") or "").strip() or None
            if en and has_japanese(en):  # プロモ等は en に日本語が入っている
                en = None
            if sid in promo:
                img = urljoin(base_url, f"images/promo/{sid}/{sid}_{n}.webp") if base_url else None
            else:
                img = LIMITLESS_IMG.format(s=sid, n=n)
            rarity = c.get("r")
            rows.append({
                "model_id": f"{sid}-{n}", "set_id": sid, "card_number": n,
                "name_jp": (c.get("jp") or "").strip() or None, "name_en": en,
                "set_name": st.get("name"), "set_name_jp": st.get("jp"),
                "rarity": None if rarity in (None, "", "Unknown") else rarity, "image_url": img,
            })
    return rows


def upsert_card_rows(db: Session, rows: list[dict], source: str) -> tuple[int, int]:
    existing = {c.model_id: c for c in db.scalars(select(CardCatalog))}
    ins = upd = 0
    for r in rows:
        row = existing.get(r["model_id"])
        if row is None:
            row = CardCatalog(model_id=r["model_id"])
            db.add(row)
            existing[r["model_id"]] = row
            ins += 1
        else:
            upd += 1
        for k, v in r.items():
            if k != "model_id" and (v is not None or k in ("rarity", "image_url")):
                setattr(row, k, v)
        row.source = source
    db.flush()
    return ins, upd


def import_tcg_catalog(db: Session, url: str) -> dict:
    """tcg-catalog のページを取得してカード一覧に取り込む（冪等）。"""
    import httpx
    from app.services import health
    try:
        r = httpx.get(url, timeout=60, follow_redirects=True)
        r.raise_for_status()
        rows = parse_tcg_catalog(r.text, str(r.url))
        if not rows:
            raise ValueError("カードデータ（SERIES / CARDS）が見つかりません")
    except Exception as e:  # noqa: BLE001
        health.record(db, "tcg-catalog", ok=False, error=str(e))
        raise
    health.record(db, "tcg-catalog", ok=True)
    ins, upd = upsert_card_rows(db, rows, "tcg-catalog")
    apply_set_names(db)
    fix_item_set_names(db)
    return {"inserted": ins, "updated": upd, "sets": len({x["set_id"] for x in rows}), "cards": len(rows)}


def import_cards(db: Session, path: Path) -> int:
    """モデルID → 日本語名/英語名。HTML（CARDS定数）・JSON・CSV に対応。"""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in (".html", ".htm", ".js") and is_tcg_catalog(text):
        ins, upd = upsert_card_rows(db, parse_tcg_catalog(text), "tcg-catalog")
        return ins + upd
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


# ---- Limitless TCG（日本語版の弾一覧）：tcg-catalog に無い新弾・欠けている英語名を補う ----

LIMITLESS_BASE = "https://limitlesstcg.com"
LIMITLESS_UA = "TorelisOps/1.0 (card catalog sync; once a day)"
_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
                                       "Dec"], start=1)}


def _limitless_date(s: str) -> date | None:
    m = re.match(r"\s*(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2,4})", s or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    y = int(m.group(3))
    return date(y + 2000 if y < 100 else y, _MONTHS[m.group(2)], int(m.group(1)))


def parse_limitless_sets(html: str) -> list[dict]:
    """/cards/jp の弾一覧 → [{set_id, name_en, release, count}]（新しい順）。"""
    out = []
    for row in re.findall(r"<tr>\s*(<td><a href=\"/cards/jp/[^\"]+\".*?)</tr>", html, re.S):
        m = re.search(r'href="/cards/jp/([^"/?]+)"[^>]*>.*?>\s*([^<]*?)\s*<span class="code', row, re.S)
        if not m:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        release = _limitless_date(re.sub(r"<[^>]+>", "", tds[1])) if len(tds) > 1 else None
        cnt = re.search(r">\s*(\d+)\s*<span", tds[2]) if len(tds) > 2 else None
        out.append({"set_id": m.group(1), "name_en": _html_unescape(m.group(2)), "release": release,
                    "count": int(cnt.group(1)) if cnt else None})
    return out


def _html_unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s or "").strip()


def parse_limitless_set_page(html: str, set_id: str) -> list[dict]:
    """弾のカード一覧（display=list）→ [{number, name, rarity, image_url, set_name_jp}]。"""
    rows = []
    for tr in re.findall(r'<tr data-hover="([^"]*)">(.*?)</tr>', html, re.S):
        hover, body = tr
        tip = re.search(r'data-tooltip="([^"]*)"', body)
        cells = re.findall(r'<a href="/cards/jp/' + re.escape(set_id) + r'/([^"?]+)[^"]*">\s*(.*?)\s*</a>', body, re.S)
        if len(cells) < 2:
            continue
        number = cells[0][0]
        name = _html_unescape(re.sub(r"<[^>]+>", "", cells[1][1]))
        rarity = _html_unescape(re.sub(r"<[^>]+>", "", cells[-1][1])) if len(cells) >= 4 else None
        img = hover.replace("_XS.png", "_LG.png") if hover else None
        rows.append({"number": number, "name": name or None, "rarity": rarity or None, "image_url": img,
                     "set_name_jp": _html_unescape(tip.group(1)) if tip else None})
    return rows


def import_limitless_jp(db: Session, *, http=None, max_sets: int = 20, since_days: int = 730,
                        refresh_days: int = 120, delay: float = 1.0) -> dict:
    """Limitless の日本語版データで、カード一覧に無い弾・カードを追加し、空の英語名・レアリティ等を埋める。

    tcg-catalog の値は上書きしない（空欄と仮の弾名だけ埋める）。1回に max_sets 弾まで（残りは翌日）。
    """
    import time as _time
    from datetime import timedelta

    import httpx

    from app.services import health
    from app.timeutil import today_jst
    own = http is None
    http = http or httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": LIMITLESS_UA})
    try:
        try:
            r = http.get(f"{LIMITLESS_BASE}/cards/jp")
            r.raise_for_status()
            sets = parse_limitless_sets(r.text)
            if not sets:
                raise ValueError("弾の一覧が見つかりません（ページの形式が変わった可能性）")
        except Exception as e:  # noqa: BLE001
            health.record(db, "limitless", ok=False, error=str(e))
            raise
        today = today_jst()
        by_set: dict[str, list[CardCatalog]] = {}
        for c in db.scalars(select(CardCatalog)):
            by_set.setdefault(c.set_id, []).append(c)

        from app.settings_store import get_setting, set_setting
        names: dict = dict(get_setting(db, "catalog_set_names") or {})

        def wanted(st: dict) -> bool:
            rel = st["release"]
            if rel is None:
                return False
            have = by_set.get(st["set_id"], [])
            if have and st["set_id"] not in names:
                return True  # 古い弾も少しずつ（1日 max_sets 弾まで）弾名を確かめ、空欄を埋める
            if rel < today - timedelta(days=since_days):
                return False
            if not have or rel >= today - timedelta(days=refresh_days):
                return True
            return any(c.name_en is None or c.rarity is None or c.set_name in (None, c.set_id) for c in have)

        cands = [s for s in sets if wanted(s)]
        cands.sort(key=lambda st: (st["set_id"] in names or not by_set.get(st["set_id"]), st["release"]), reverse=True)
        targets = cands[:max_sets]
        res = {"sets_checked": len(targets), "new_sets": [], "inserted": 0, "filled": 0, "errors": []}
        for st in targets:
            sid = st["set_id"]
            try:
                base = f"{LIMITLESS_BASE}/cards/jp/{sid}?display=list&show=all"
                jp = parse_limitless_set_page(http.get(base).raise_for_status().text, sid)
                _time.sleep(delay)
                en = {x["number"]: x for x in parse_limitless_set_page(
                    http.get(base + "&translate=en").raise_for_status().text, sid)}
                _time.sleep(delay)
            except Exception as e:  # noqa: BLE001
                res["errors"].append(f"{sid}: {e}")
                continue
            if not jp:
                continue
            if jp[0].get("set_name_jp"):
                names[sid] = jp[0]["set_name_jp"]
            was_new = not by_set.get(sid)
            ins, filled = _merge_limitless_set(db, sid, jp, en, by_set)
            if ins and was_new:
                res["new_sets"].append(sid)
            by_set.setdefault(sid, [])
            res["inserted"] += ins
            res["filled"] += filled
        db.flush()
        set_setting(db, "catalog_set_names", names, None)
        names_en = dict(get_setting(db, "catalog_set_names_en") or {})
        names_en.update({st["set_id"]: st["name_en"] for st in sets if st["name_en"] and not has_japanese(st["name_en"])})
        set_setting(db, "catalog_set_names_en", names_en, None)
        releases = dict(get_setting(db, "catalog_set_release") or {})
        releases.update({st["set_id"]: st["release"].isoformat() for st in sets if st["release"]})
        set_setting(db, "catalog_set_release", releases, None)
        res["renamed"] = apply_set_names(db)
        res["items_fixed"] = fix_item_set_names(db)
        health.record(db, "limitless", ok=not res["errors"], error="; ".join(res["errors"])[:500] or None)
        return res
    finally:
        if own:
            http.close()


def _merge_limitless_set(db: Session, sid: str, jp: list[dict], en: dict, by_set: dict) -> tuple[int, int]:
    from app.services.cardinfo import normalize_card_number
    have: dict[str, list[CardCatalog]] = {}
    for c in by_set.get(sid, []):
        have.setdefault(normalize_card_number(c.card_number), []).append(c)
    ins = filled = 0
    for x in jp:
        n = x["number"]
        name_en = (en.get(n) or {}).get("name")
        if name_en and has_japanese(name_en):
            name_en = None
        vals = {"name_jp": x["name"], "name_en": name_en, "rarity": x["rarity"], "image_url": x["image_url"]}
        new_vals = {**vals, "set_name": x["set_name_jp"], "set_name_jp": x["set_name_jp"]}
        rows = have.get(normalize_card_number(n))
        if not rows:
            row = CardCatalog(model_id=f"{sid}-{n}", set_id=sid, card_number=n, source="limitless", **new_vals)
            db.add(row)
            by_set.setdefault(sid, []).append(row)
            have[normalize_card_number(n)] = [row]
            ins += 1
            continue
        for row in rows:  # 既存（tcg-catalog）は空欄と仮の弾名だけ埋める
            changed = False
            for k, v in vals.items():
                cur = getattr(row, k)
                if v and cur in (None, ""):
                    setattr(row, k, v)
                    changed = True
            filled += changed
    return ins, filled


# Limitless と記号が違うプロモ（SV-P ↔ SVP など）は英語名を固定で持つ
PROMO_SET_EN = {
    "ADV-P": "ADV Promo", "BW-P": "Black & White Promo", "DP-P": "DP Promo", "DPt-P": "DPt Promo",
    "L-P": "LEGEND Promo", "M-P": "MEGA Promo", "PCG-P": "PCG Promo", "S-P": "Sword & Shield Promo",
    "SM-P": "Sun & Moon Promo", "SV-P": "Scarlet & Violet Promo", "XY-P": "XY Promo",
}
BASIC_ENERGY_EN = {
    "基本草エネルギー": "Basic Grass Energy", "基本炎エネルギー": "Basic Fire Energy",
    "基本水エネルギー": "Basic Water Energy", "基本雷エネルギー": "Basic Lightning Energy",
    "基本超エネルギー": "Basic Psychic Energy", "基本闘エネルギー": "Basic Fighting Energy",
    "基本悪エネルギー": "Basic Darkness Energy", "基本鋼エネルギー": "Basic Metal Energy",
    "基本フェアリーエネルギー": "Basic Fairy Energy",
}


def apply_set_names(db: Session) -> int:
    """弾名を揃える：set_name＝英語名（eBayのタイトル・Set欄に使う）、set_name_jp＝日本語名。

    どちらも Limitless で確かめた名前で、tcg-catalog の誤り・仮名・日本語のままの set_name を直す。
    """
    from app.settings_store import get_setting
    jp = get_setting(db, "catalog_set_names") or {}
    en = {**PROMO_SET_EN, **(get_setting(db, "catalog_set_names_en") or {})}
    n = 0
    for c in db.scalars(select(CardCatalog).where(CardCatalog.name_en.is_(None),
                                                  CardCatalog.name_jp.in_(list(BASIC_ENERGY_EN)))):
        c.name_en = BASIC_ENERGY_EN[c.name_jp]
    ids = set(jp) | set(en)
    for c in db.scalars(select(CardCatalog).where(CardCatalog.set_id.in_(list(ids)))):
        want_en = en.get(c.set_id) or (c.set_name if not has_japanese(c.set_name) else None)
        want_jp = jp.get(c.set_id) or (c.set_name_jp if has_japanese(c.set_name_jp) else
                                       (c.set_name if has_japanese(c.set_name) else c.set_name_jp))
        changed = False
        if want_en and c.set_name != want_en:
            c.set_name, changed = want_en, True
        if want_jp and c.set_name_jp != want_jp:
            c.set_name_jp, changed = want_jp, True
        n += changed
    db.flush()
    return n


def fix_item_set_names(db: Session) -> int:
    """出品前の在庫で弾名が日本語のものを英語名に直す（タイトルが英語になるように）。"""
    from app import audit
    from app.models import Item, ItemStatus as IS
    from app.settings_store import get_setting
    en = {**PROMO_SET_EN, **(get_setting(db, "catalog_set_names_en") or {})}
    n = 0
    for it in db.scalars(select(Item).where(Item.status.in_([IS.INTAKE, IS.PHOTOGRAPHED, IS.PRICED, IS.READY]),
                                            Item.set_id.in_(list(en)))):
        if has_japanese(it.set_name) and en.get(it.set_id):
            audit.log(db, audit.Actor.system(), "item", it.id, "item.set_name_english",
                      before={"set_name": it.set_name}, after={"set_name": en[it.set_id]})
            it.set_name = en[it.set_id]
            it.version += 1
            n += 1
    db.flush()
    return n
