"""Background scraper for Madeira car listings + Telegram alerts.

Run on its own:
    python3 scraper.py --test        # fetch page 1 of each site, print what was parsed (no DB, no Telegram)
    python3 scraper.py --test --save # same, and save the raw HTML to ./debug/ so it can be inspected
    python3 scraper.py --once        # one full cycle (writes DB, sends Telegram)
    python3 scraper.py               # loop forever
Normally app.py starts it in the background for you.
"""
import argparse
import html as htmllib
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

import core
from core import norm, send_telegram, session

try:
    from zoneinfo import ZoneInfo
    MADEIRA = ZoneInfo("Atlantic/Madeira")
except Exception:  # pragma: no cover - tz database missing
    MADEIRA = timezone.utc

POLL_SECONDS = max(60, int(os.environ.get("POLL_SECONDS", "180")))
ALERT_MODE = os.environ.get("ALERT_MODE", "all").strip().lower()      # all | deals
MAX_SILENT_PER_CYCLE = int(os.environ.get("MAX_SILENT_PER_CYCLE", "8"))
QUIET_HOURS = os.environ.get("QUIET_HOURS", "").strip()               # e.g. 01-08 (Madeira time)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
}

STATUS = {}   # per-source info, read by app.py /api/status

# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
STV_LINK = re.compile(r"(?:https://www\.standvirtual\.com)?/carros/anuncio/[\w\-]+\.html")
OLX_LINK = re.compile(r"(?:https://www\.olx\.pt)?/d/anuncio/[\w\-]+\.html")
CJ_LINK = re.compile(r"(?:https://www\.custojusto\.pt)?/madeira/veiculos/[\w\-/]*?-\d{6,9}(?![\w\-/])")

SOURCES = [
    dict(key="stv", family="stv", name="Standvirtual", link=STV_LINK, base="https://www.standvirtual.com", salvage=False,
         urls=["https://www.standvirtual.com/carros/madeira-madeira?search%5Border%5D=created_at_first%3Adesc",
               "https://www.standvirtual.com/carros/madeira-madeira?search%5Border%5D=created_at_first%3Adesc&page=2"],
         fallback=["https://www.standvirtual.com/carros/madeira-madeira"]),
    dict(key="olx", family="olx", name="OLX", link=OLX_LINK, base="https://www.olx.pt", salvage=False,
         urls=["https://www.olx.pt/carros-motos-e-barcos/carros/madeira-madeira/?search%5Border%5D=created_at%3Adesc"],
         fallback=["https://www.olx.pt/carros-motos-e-barcos/madeira-madeira/?search%5Border%5D=created_at%3Adesc"]),
    dict(key="cj", family="cj", name="CustoJusto", link=CJ_LINK, base="https://www.custojusto.pt", salvage=False,
         urls=["https://www.custojusto.pt/madeira/veiculos/carros-usados"], fallback=[]),
    dict(key="cj_salv", family="cj", name="CustoJusto", link=CJ_LINK, base="https://www.custojusto.pt", salvage=True,
         urls=["https://www.custojusto.pt/madeira/veiculos/carros-usados/salvado"], fallback=[]),
]


class Blocked(Exception):
    pass


def log(msg):
    print("[%s] %s" % (datetime.now(MADEIRA).strftime("%H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def fetch(url, http):
    r = http.get(url, headers=HEADERS, timeout=20)
    if r.status_code in (403, 429, 503):
        raise Blocked("HTTP %d from %s" % (r.status_code, urlparse(url).netloc))
    if r.status_code == 404:
        raise requests.HTTPError("404 not found: %s" % url)
    r.raise_for_status()
    return r.content.decode("utf-8", errors="replace")   # requests guesses latin-1 when no charset is sent


# --------------------------------------------------------------------------- #
# HTML -> text helpers
# --------------------------------------------------------------------------- #
_BLOCK = re.compile(r"</?(?:p|div|li|ul|ol|h[1-6]|article|section|tr|td|table|header|footer|main|nav|br|button|figure|dl|dt|dd)\b[^>]*>", re.I)


def to_text(fragment):
    t = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", fragment)
    t = _BLOCK.sub("\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = htmllib.unescape(t).replace(" ", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n")]
    return "\n".join(ln for ln in lines if ln)


PRICE_RE = re.compile(r"(\d{1,3}(?:[  .]\d{3})+|\d{3,7})(?:,\d{1,2})?\s*(?:€|EUR\b)")


def prices_in(text):
    out = []
    for m in PRICE_RE.finditer(text):
        v = int(re.sub(r"\D", "", m.group(1)))
        if v >= 100:
            out.append(v)
    return out


def canonical(raw, base):
    url = raw.split("?")[0].split("#")[0]
    return base + url if url.startswith("/") else url


def extract_cards(page, link_re, base):
    spots, seen = [], set()
    for m in link_re.finditer(page):
        if page[max(0, m.start() - 6):m.start()] not in ('href="', "href='"):
            continue
        url = canonical(m.group(0), base)
        if url in seen:
            continue
        seen.add(url)
        start = page.rfind("<", 0, m.start())
        spots.append((start if start >= 0 else m.start(), url))
    cards = []
    for i, (pos, url) in enumerate(spots):
        end = spots[i + 1][0] if i + 1 < len(spots) else min(len(page), pos + 20000)
        cards.append((url, page[pos:end]))
    return cards


def anchor_title(chunk, path):
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', chunk, re.S | re.I):
        if path in m.group(1):
            lines = to_text(m.group(2)).split("\n")
            for ln in lines:
                if len(ln) >= 3 and ln.lower() not in ("top", "destaque", "novo", "montra", "pro"):
                    return ln
    h = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", chunk, re.S | re.I)
    if h:
        t = to_text(h.group(1)).split("\n")[0]
        if t:
            return t
    return ""


def first_image(chunk):
    for m in re.finditer(r'<img\b[^>]*?(?:src|data-src)=["\'](https?://[^"\']+)["\']', chunk, re.I):
        u = m.group(1)
        if any(x in u.lower() for x in (".svg", "logo", "no-image", "s=140x24", "s=95x24")):
            continue
        return u
    return ""


# --------------------------------------------------------------------------- #
# Date parsing (Portuguese listing dates -> UTC ISO string)
# --------------------------------------------------------------------------- #
PT_MONTHS = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6, "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}


def parse_pt_datetime(s):
    s = norm(s)
    if not s or "para o topo" in s:
        return None          # bumped ads: original publish time unknown
    now = datetime.now(MADEIRA)
    try:
        m = re.search(r"\b(hoje|ontem)\b\D{0,6}(\d{1,2}):(\d{2})", s)
        if m:
            day = (now - timedelta(days=1 if m.group(1) == "ontem" else 0)).date()
            dt = datetime(day.year, day.month, day.day, int(m.group(2)), int(m.group(3)), tzinfo=MADEIRA)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        m = re.search(r"(\d{1,2}) de ([a-z]+) de (\d{4})", s)
        if m and m.group(2)[:3] in PT_MONTHS:
            dt = datetime(int(m.group(3)), PT_MONTHS[m.group(2)[:3]], int(m.group(1)), tzinfo=MADEIRA)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        m = re.search(r"(\d{1,2}) ([a-z]{3})[a-z]*[\s.,]*(\d{1,2}):(\d{2})", s)
        if m and m.group(2) in PT_MONTHS:
            dt = datetime(now.year, PT_MONTHS[m.group(2)], int(m.group(1)), int(m.group(3)), int(m.group(4)), tzinfo=MADEIRA)
            if dt > now + timedelta(days=1):
                dt = dt.replace(year=now.year - 1)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None
    return None


# --------------------------------------------------------------------------- #
# Per-site card parsers
# --------------------------------------------------------------------------- #
MARKET_FLAGS = {"abaixo da media": "Below market average", "dentro da media": "Within market average", "acima da media": "Above market average"}


def _int(s):
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else None


def parse_stv(text, title):
    d = {}
    p = prices_in(text)
    d["price"] = max(p) if p else None          # the smaller number on a card is the price-drop amount
    m = re.search(r"first_registration_year\s*(\d{4})", text)
    d["year"] = int(m.group(1)) if m else None
    m = re.search(r"mileage\s*([\d ]+?)\s*km", text)
    d["mileage"] = _int(m.group(1)) if m else None
    m = re.search(r"fuel_type\s*([^\n]+)", text)
    d["fuel"] = m.group(1).strip() if m else ""
    d["location"] = ""
    for ln in text.split("\n"):
        if "(Ilha da Madeira)" in ln:
            d["location"] = ln.split("(Ilha da Madeira)")[0].strip(" -•·")
            break
    nt = norm(text)
    d["market_flag"] = next((v for k, v in MARKET_FLAGS.items() if k in nt), "")
    d["published_at"] = None
    return d


LOC_DATE_RE = re.compile(r"^(?P<loc>.+?)\s+-\s+(?P<date>(?:Para o topo.*|Hoje.*|Ontem.*|\d{1,2} de \w+ de \d{4}))$", re.I)


def parse_olx(text, title):
    d = {"location": "", "published_at": None, "year": None, "mileage": None, "fuel": "", "market_flag": ""}
    after = text.replace(title, "", 1) if title else text
    p = prices_in(after) or prices_in(title)
    d["price"] = p[0] if p else None
    lines = after.split("\n")
    for i, ln in enumerate(lines):
        m = LOC_DATE_RE.match(ln)
        if m:
            d["location"] = m.group("loc").strip()
            d["published_at"] = parse_pt_datetime(m.group("date"))
            tail = " ".join(lines[i + 1:i + 3])
            ym = re.search(r"\b((?:19|20)\d{2})\b", tail)
            km = re.search(r"([\d.]+)\s*km", tail)
            d["year"] = int(ym.group(1)) if ym else None
            d["mileage"] = _int(km.group(1)) if km else None
            break
    return d


def parse_cj(text, title):
    d = {"location": "", "published_at": None, "year": None, "mileage": None, "fuel": "", "market_flag": ""}
    lines = text.split("\n")
    start = 0
    for i, ln in enumerate(lines):
        if title and title in ln:
            start = i + 1
            break
    rest = lines[start:]
    p = prices_in("\n".join(rest))
    d["price"] = p[0] if p else None
    for i, ln in enumerate(rest):
        if "·" in ln:
            attrs = [a.strip() for a in ln.split("·")]
            d["fuel"] = attrs[0]
            ym = re.search(r"((?:19|20)\d{2})", attrs[-1])
            d["year"] = int(ym.group(1)) if ym else None
            if i + 1 < len(rest) and not PRICE_RE.search(rest[i + 1]):
                d["location"] = rest[i + 1]
            break
    for ln in rest:
        n = norm(ln)
        if re.search(r"(hoje|ontem|\d{1,2} [a-z]{3})\D{0,3}\d{1,2}:\d{2}", n):
            d["published_at"] = parse_pt_datetime(ln)
            break
    return d


PARSERS = {"stv": parse_stv, "olx": parse_olx, "cj": parse_cj}

_JUNK_START = re.compile(r"^(motor|caixa|jantes?|pecas?|farol|faroes|portas?|kit|capot|pneus?|bancos?|escape|radiador|turbo|bateria)\b")
_JUNK_ANY = re.compile(r"trator|tractor|giratoria|camiao|barco|lancha|moradia|autocaravana|reboque|scooter|motociclo|\bcc\b|\d{2,3}cc|ducati|kawasaki|yamaha|\bktm\b|vespa|piaggio|supermoto|\bmoto\b|quad\b")


def is_junk(title):
    t = norm(title)
    return bool(_JUNK_START.match(t) or _JUNK_ANY.search(t))


def _iso_utc(value):
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def parse_olx_json(page):
    """OLX embeds every advert as JSON in window.__PRERENDERED_STATE__: much more reliable than scraping text."""
    m = re.search(r"window\.__PRERENDERED_STATE__\s*=\s*", page)
    if not m:
        return []
    try:
        state, _ = json.JSONDecoder().raw_decode(page, m.end())
        if isinstance(state, str):
            state = json.loads(state)
        ads = state["listing"]["listing"]["ads"]
    except (ValueError, KeyError, TypeError):
        return []
    cards = []
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        url = (ad.get("url") or "").split("?")[0]
        title = (ad.get("title") or "").strip()
        if "/d/anuncio/" not in url or not title:
            continue
        if "standvirtual.com" in (ad.get("externalUrl") or ""):
            continue            # same advert, scraped from Standvirtual itself
        loc = ad.get("location") or {}
        city = loc.get("cityName") or (loc.get("city") or {}).get("name") or ""
        region = loc.get("regionName") or (loc.get("region") or {}).get("name") or ""
        if region and "madeira" not in norm(region):
            continue
        reg = (ad.get("price") or {}).get("regularPrice") or {}
        price = reg.get("value")
        year = mileage = None
        fuel = ""
        for p in ad.get("params") or []:
            key, name = norm(p.get("key") or ""), norm(p.get("name") or "")
            val = p.get("normalizedValue") or p.get("value")
            if key == "year":
                year = _int(str(val))
            elif "mileage" in key or "quilomet" in name:
                mileage = _int(str(val))
            elif key in ("petrol", "fuel_type") or "combustivel" in name:
                fuel = str(p.get("value") or "")
        photos = ad.get("photos") or []
        id_m = re.search(r"-ID(\w+)\.html$", url)
        card = {"url": url, "title": title, "source": "OLX", "salvage": False, "image_url": photos[0] if photos else "",
                "price": int(price) if isinstance(price, (int, float)) and price >= 100 else None,
                "year": year, "mileage": mileage, "fuel": fuel, "location": city, "market_flag": "",
                "published_at": _iso_utc(ad.get("createdTime")), "description": (ad.get("description") or "")[:2500],
                "source_id": id_m.group(1) if id_m else ""}
        if not is_junk(title):
            cards.append(card)
    return cards


def parse_page(src, page):
    if src["family"] == "olx":
        cards = parse_olx_json(page)
        if cards:
            return cards
    cards = []
    for url, chunk in extract_cards(page, src["link"], src["base"]):
        text = to_text(chunk)
        title = anchor_title(chunk, urlparse(url).path)
        if not title:
            title = re.sub(r"[-_]+", " ", url.rsplit("/", 1)[-1].rsplit(".", 1)[0]).strip()
        c = {"url": url, "title": title, "source": src["name"], "salvage": src["salvage"], "image_url": first_image(chunk)}
        c.update(PARSERS[src["family"]](text, title))
        m = re.search(r"-ID(\w+)\.html$", url) if src["family"] in ("stv", "olx") else re.search(r"-(\d{6,9})$", url)
        c["source_id"] = m.group(1) if m else ""
        if is_junk(title):
            continue
        cards.append(c)
    return cards


def scrape_source(src, http):
    cards, seen = [], set()

    def collect(urls):
        for i, url in enumerate(urls):
            if i:
                time.sleep(random.uniform(1.0, 2.5))
            try:
                page = fetch(url, http)
            except requests.HTTPError as exc:
                if "404" in str(exc):
                    log("%s: %s -> 404 (skipping)" % (src["key"], url))
                    continue
                raise
            for c in parse_page(src, page):
                if c["url"] not in seen:
                    seen.add(c["url"])
                    cards.append(c)

    collect(src["urls"])
    if not cards and src.get("fallback"):
        collect(src["fallback"])
    return cards


# --------------------------------------------------------------------------- #
# Listing detail page: pull the seller's description so "motor avariado" in the text is caught
# --------------------------------------------------------------------------- #
def page_description(page):
    found = []
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I | re.S):
        if re.search(r'(?:property|name)=["\'](?:og:description|description)["\']', tag, re.I):
            m = re.search(r'content=(?:"([^"]*)"|\'([^\']*)\')', tag, re.S)
            if m:
                found.append(htmllib.unescape(m.group(1) or m.group(2) or ""))
    for raw in re.findall(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', page)[:4]:
        found.append(raw.replace("\\n", " ").replace('\\"', '"').replace("\\/", "/"))
    seen, out = set(), []
    for f in found:
        f = f.strip()
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return " ".join(out)[:2500]


# --------------------------------------------------------------------------- #
# Alert policy
# --------------------------------------------------------------------------- #
def in_quiet_hours():
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", QUIET_HOURS)
    if not m:
        return False
    a, b, h = int(m.group(1)), int(m.group(2)), datetime.now(MADEIRA).hour
    return (a <= h < b) if a < b else (h >= a or h < b)


def decide(kind, ratio):
    loud = kind in ("steal", "deal", "project", "drop") or (kind == "accident" and ratio is not None and ratio <= 0.55)
    if ALERT_MODE == "deals":
        return loud, loud
    return True, loud


def deliver(L, a, ctx, old_price=None):
    send, loud = decide(a["kind"], a["ratio"])
    if not send:
        return False
    if not loud:
        if ctx["silent_sent"] >= MAX_SILENT_PER_CYCLE:
            ctx["silent_skipped"] += 1
            return True          # counted as handled; mentioned in the summary line
        ctx["silent_sent"] += 1
    ok, detail = send_telegram(core.format_alert(L, a, old_price), silent=(not loud) or in_quiet_hours())
    if not ok:
        log("Telegram send failed: %s" % detail)
    return ok


# --------------------------------------------------------------------------- #
# One scrape cycle
# --------------------------------------------------------------------------- #
def get_state(con, key):
    con.execute("INSERT OR IGNORE INTO source_state (key) VALUES (?)", (key,))
    return con.execute("SELECT * FROM source_state WHERE key=?", (key,)).fetchone()


def ingest(src, cards, baselined):
    """Write cards to the DB. Returns (new_ids, drops[(id, old_price)])."""
    new_ids, drops = [], []
    with session() as con:
        for c in cards:
            row = None
            if c.get("source_id"):
                row = con.execute("SELECT * FROM listings WHERE source=? AND source_id=?", (c["source"], c["source_id"])).fetchone()
            if row is None:
                row = con.execute("SELECT * FROM listings WHERE url=?", (c["url"],)).fetchone()
            ts = core.now_iso()
            if row is not None:
                con.execute("UPDATE listings SET last_seen_at=? WHERE id=?", (ts, row["id"]))
                old, new = row["price"], c.get("price")
                if new and old and new != old:
                    con.execute("UPDATE listings SET price=?, price_prev=? WHERE id=?", (new, old, row["id"]))
                    if baselined and not row["is_demo"] and new <= old * 0.92 and old - new >= 300:
                        drops.append((row["id"], old))
                continue
            issue, reason = core.detect_issue(c["title"], c.get("description", ""))
            if c.get("salvage") and not issue:
                issue, reason = "body", "salvage category"
            make, model = core.split_make_model(c["title"])
            condition = {"mech": "Mechanical issue", "body": "Damaged/Salvage"}.get(issue, "Used")
            try:
                cur = con.execute("""INSERT INTO listings
                    (source,source_id,url,title,make,model,year,price,location,island,condition,description,published_at,
                     first_seen_at,last_seen_at,image_url,is_demo,mileage,fuel,broken,broken_reason,market_flag)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
                    (c["source"], c.get("source_id") or None, c["url"], c["title"], make, model, c.get("year"), c.get("price"),
                     c.get("location", ""), "Madeira", condition, c.get("description", ""), c.get("published_at"), ts, ts, c.get("image_url", ""),
                     c.get("mileage"), c.get("fuel", ""), {"mech": 1, "body": 2}.get(issue, 0), reason, c.get("market_flag", "")))
                new_ids.append(cur.lastrowid)
            except sqlite3.IntegrityError:
                continue
    return new_ids, drops


def alert_new(listing_id, http, ctx):
    with session() as con:
        row = con.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    L = dict(row)
    issue = {1: "mech", 2: "body"}.get(L["broken"], "")
    reason = L["broken_reason"] or ""
    desc = L.get("description") or ""
    if not desc:
        try:
            desc = page_description(fetch(L["url"], http))
        except Exception as exc:  # detail page is a bonus; never block an alert on it
            log("detail fetch failed for %s: %s" % (L["url"], exc))
    if desc:
        i2, r2 = core.detect_issue(L["title"], desc)
        if i2 == "mech" and issue != "mech":
            issue, reason = "mech", r2
        elif i2 == "body" and not issue:
            issue, reason = "body", r2
    L.update(issue=issue, reason=reason, description=desc or L["description"])
    with session() as con:
        a = core.assess(con, L)
        con.execute("""UPDATE listings SET description=?, broken=?, broken_reason=?, condition=?, median_price=?, deal_ratio=?, alert_type=?
                       WHERE id=?""",
                    (L["description"], {"mech": 1, "body": 2}.get(issue, 0), reason,
                     {"mech": "Mechanical issue", "body": "Damaged/Salvage"}.get(issue, "Used"),
                     a["median"], a["ratio"], a["kind"], listing_id))
    sent = deliver(L, a, ctx)
    if not sent:
        core.notify_matching_saved_searches(L)


def alert_drop(listing_id, old_price, ctx):
    with session() as con:
        row = con.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        L = dict(row)
        L.update(issue={1: "mech", 2: "body"}.get(L["broken"], ""), reason=L["broken_reason"] or "")
        a = core.assess(con, L)
    if a["kind"] in ("new", "deal", "steal"):
        a["kind"] = "drop"
    deliver(L, a, ctx, old_price=old_price)


def record_result(key, count, error="", may_be_empty=False):
    """Track consecutive failures and warn once on Telegram if a source goes dark."""
    warn = None
    with session() as con:
        st = get_state(con, key)
        if error or (count == 0 and not may_be_empty):
            streak = st["zero_streak"] + 1
            warned = st["warned"]
            if streak >= 3 and not warned:
                warn = "⚠️ CarFinder: source '%s' has returned nothing %d times in a row (%s). It may be blocking requests or the site changed its layout." % (
                    key, streak, error or "0 listings parsed")
                warned = 1
            con.execute("UPDATE source_state SET zero_streak=?, warned=?, last_error=?, last_count=? WHERE key=?",
                        (streak, warned, error or "0 listings parsed", count, key))
        else:
            con.execute("UPDATE source_state SET zero_streak=0, warned=0, last_error='', last_count=?, last_ok=? WHERE key=?",
                        (count, core.now_iso(), key))
    if warn:
        log(warn)
        send_telegram(warn)


_backoff = {}


def run_cycle(http=None):
    http = http or requests.Session()
    ctx = {"silent_sent": 0, "silent_skipped": 0}
    announced = []
    for src in SOURCES:
        key = src["key"]
        if time.time() < _backoff.get(key, 0):
            continue
        try:
            cards = scrape_source(src, http)
        except Blocked as exc:
            _backoff[key] = time.time() + 900
            log("%s: blocked (%s), pausing this source for 15 min" % (key, exc))
            record_result(key, 0, error=str(exc))
            STATUS[key] = {"ok": False, "error": str(exc), "at": core.now_iso()}
            continue
        except requests.RequestException as exc:
            log("%s: request failed: %s" % (key, exc))
            record_result(key, 0, error=str(exc))
            STATUS[key] = {"ok": False, "error": str(exc), "at": core.now_iso()}
            continue
        if cards:
            priced = sum(1 for c in cards if c.get("price"))
            if priced / float(len(cards)) < 0.5:
                msg = "parser found prices on only %d of %d listings (page layout differs)" % (priced, len(cards))
                log("%s: %s -> skipping this source until the parser is fixed" % (key, msg))
                record_result(key, 0, error=msg)
                STATUS[key] = {"ok": False, "error": msg, "at": core.now_iso()}
                continue
        record_result(key, len(cards), may_be_empty=src["salvage"])
        with session() as con:
            baselined = bool(get_state(con, key)["baselined"])
        new_ids, drops = ingest(src, cards, baselined)
        if cards and not baselined:
            with session() as con:
                con.execute("UPDATE source_state SET baselined=1 WHERE key=?", (key,))
            announced.append("%s: %d" % (key, len(cards)))
            log("%s: first run, stored %d existing listings silently (no alerts for these)" % (key, len(cards)))
        else:
            log("%s: %d parsed, %d new, %d price drops" % (key, len(cards), len(new_ids), len(drops)))
            for lid in new_ids:
                try:
                    alert_new(lid, http, ctx)
                except Exception as exc:
                    log("alert failed for listing %s: %s" % (lid, exc))
                time.sleep(random.uniform(0.5, 1.5))
            for lid, old in drops:
                try:
                    alert_drop(lid, old, ctx)
                except Exception as exc:
                    log("drop alert failed for listing %s: %s" % (lid, exc))
        STATUS[key] = {"ok": True, "parsed": len(cards), "new": len(new_ids) if baselined else 0, "at": core.now_iso()}
        time.sleep(random.uniform(1.5, 4.0))
    if ctx["silent_skipped"]:
        send_telegram("🚗 +%d more new listings this round (not shown individually). Open the dashboard to see them." % ctx["silent_skipped"], silent=True)
    if announced:
        send_telegram("✅ CarFinder is live. Stored what's already online (%s). From now on you only get alerts for new posts.\n"
                      "🔥 deal = well below typical price · 🔧 project = engine/mechanical problem mentioned" % ", ".join(announced))


# --------------------------------------------------------------------------- #
# Background loop / CLI
# --------------------------------------------------------------------------- #
def _loop(stop):
    while not stop.is_set():
        try:
            run_cycle()
        except Exception as exc:
            log("cycle crashed: %r" % exc)
        stop.wait(POLL_SECONDS * random.uniform(0.8, 1.2))


def start_background():
    core.init_db()
    if not core.telegram_configured():
        log("WARNING: Telegram is not configured (.env). Alerts will only be printed here.")
    stop = threading.Event()
    t = threading.Thread(target=_loop, args=(stop,), daemon=True, name="carfinder-poller")
    t.start()
    return stop


def self_test(save=False):
    http = requests.Session()
    if save:
        os.makedirs("debug", exist_ok=True)
    for src in SOURCES:
        url = src["urls"][0]
        print("\n=== %s  %s" % (src["key"], url))
        try:
            page = fetch(url, http)
        except Exception as exc:
            print("   FETCH FAILED: %s" % exc)
            continue
        print("   downloaded %d characters" % len(page))
        if save:
            with open(os.path.join("debug", "%s.html" % src["key"]), "w", encoding="utf-8") as fh:
                fh.write(page)
        cards = parse_page(src, page)
        print("   parsed %d listings" % len(cards))
        if cards:
            for field in ("price", "year", "mileage", "location", "published_at"):
                have = sum(1 for c in cards if c.get(field))
                print("   %-13s found on %d/%d" % (field, have, len(cards)))
            for c in cards[:3]:
                print("   - %s | %s | %s | %s | %s | %s" % (c["title"], c.get("price"), c.get("year"), c.get("mileage"), c.get("location"), c["url"]))
        time.sleep(random.uniform(1.5, 3.0))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="fetch + parse page 1 of each site, print results, touch nothing")
    ap.add_argument("--save", action="store_true", help="with --test: save raw HTML to ./debug/")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = ap.parse_args()
    if args.test:
        self_test(save=args.save)
        sys.exit(0)
    core.init_db()
    if args.once:
        run_cycle()
        sys.exit(0)
    log("CarFinder scraper running (every ~%ds). Ctrl+C to stop." % POLL_SECONDS)
    stop = start_background()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
