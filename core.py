"""Shared helpers: database, Telegram, issue detection, deal scoring, alert text."""
import os
import re
import sqlite3
import statistics
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

APP_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(APP_DIR, ".env"))
DB_PATH = os.environ.get("CARFINDER_DB", os.path.join(APP_DIR, "carfinder.db"))

# A listing at or below this fraction of the typical price counts as a deal / steal.
DEAL_RATIO = float(os.environ.get("DEAL_RATIO", "0.80"))
STEAL_RATIO = float(os.environ.get("STEAL_RATIO", "0.65"))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def session():
    """Commit-and-close connection (the plain `with db()` form never closes)."""
    con = db()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


NEW_COLUMNS = {
    "mileage": "INTEGER",
    "fuel": "TEXT DEFAULT ''",
    "broken": "INTEGER DEFAULT 0",          # 0 ok, 1 mechanical issue, 2 body damage/salvage
    "broken_reason": "TEXT DEFAULT ''",
    "median_price": "REAL",
    "deal_ratio": "REAL",
    "market_flag": "TEXT DEFAULT ''",
    "alert_type": "TEXT DEFAULT ''",
    "price_prev": "REAL",
}


def init_db():
    with session() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_id TEXT,
            url TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            make TEXT DEFAULT '',
            model TEXT DEFAULT '',
            year INTEGER,
            price REAL,
            location TEXT DEFAULT '',
            island TEXT DEFAULT 'Madeira',
            condition TEXT DEFAULT 'Unknown',
            description TEXT DEFAULT '',
            published_at TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            image_url TEXT DEFAULT '',
            is_demo INTEGER DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_source_id
        ON listings(source, source_id) WHERE source_id IS NOT NULL AND source_id <> '';
        CREATE TABLE IF NOT EXISTS saved_searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            keywords TEXT DEFAULT '',
            make TEXT DEFAULT '',
            model TEXT DEFAULT '',
            min_price REAL,
            max_price REAL,
            condition TEXT DEFAULT '',
            island TEXT DEFAULT 'Madeira',
            telegram_enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_state (
            key TEXT PRIMARY KEY,
            baselined INTEGER DEFAULT 0,
            zero_streak INTEGER DEFAULT 0,
            warned INTEGER DEFAULT 0,
            last_ok TEXT,
            last_count INTEGER DEFAULT 0,
            last_error TEXT DEFAULT ''
        );
        """)
        have = {r["name"] for r in con.execute("PRAGMA table_info(listings)")}
        for col, decl in NEW_COLUMNS.items():
            if col not in have:
                con.execute("ALTER TABLE listings ADD COLUMN %s %s" % (col, decl))
        count = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        if count == 0:
            ts = now_iso()
            demo = [
                ("Demo source", "demo-1", "https://example.com/demo-bmw-1", "DEMO — BMW 320d (example listing)", "BMW", "3xx", 2012, 6500, "Funchal", "Madeira", "Used", "Demonstration data only.", ts, ts, ts, "", 1),
                ("Demo source", "demo-2", "https://example.com/demo-vw-1", "DEMO — Volkswagen Golf (example, engine fault)", "Volkswagen", "golf", 2009, 1800, "Câmara de Lobos", "Madeira", "Engine fault", "Demonstration data only.", ts, ts, ts, "", 1),
                ("Demo source", "demo-3", "https://example.com/demo-audi-1", "DEMO — Audi A3 (example listing)", "Audi", "a3", 2015, 9200, "Machico", "Madeira", "Used", "Demonstration data only.", ts, ts, ts, "", 1),
            ]
            con.executemany("""INSERT OR IGNORE INTO listings
                (source,source_id,url,title,make,model,year,price,location,island,condition,description,published_at,first_seen_at,last_seen_at,image_url,is_demo)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", demo)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def telegram_configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def send_telegram(message, silent=False):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False, "Telegram is not configured."
    payload = {"chat_id": chat_id, "text": message[:4000], "disable_web_page_preview": False}
    if silent:
        payload["disable_notification"] = True
    try:
        response = requests.post("https://api.telegram.org/bot%s/sendMessage" % token, json=payload, timeout=12)
        response.raise_for_status()
        return True, "sent"
    except requests.RequestException as exc:
        return False, str(exc)


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower()


# --------------------------------------------------------------------------- #
# Broken / problem-car detection (Portuguese listings)
# --------------------------------------------------------------------------- #
_MECH = [   # most specific first: the first hit is the reason shown in the alert
    r"motor fundid\w*", r"fundiu", r"motor partid\w*", r"motor gripad\w*", r"gripou", r"motor estragad\w*",
    r"sem motor", r"falta (?:o )?motor", r"precisa de (?:um )?motor", r"motor com problema\w*", r"motor a precisar",
    r"motor avariad\w*", r"colaca", r"culassa", r"junta da cabeca", r"junta da colaca",
    r"caixa (?:de velocidades )?(?:partida|avariada|estragada)", r"turbo (?:avariado|partido|estragado)",
    r"problema\w* (?:no|de|na|com|do|da) (?:o |a )?(?:motor|caixa|turbo|embraiagem|injec\w*|injet\w*)",
    r"correia de distribuicao partida", r"distribuicao partida",
    r"nao arranca", r"nao pega", r"nao liga", r"nao anda", r"nao engata", r"nao funciona",
    r"mecanica (?:avariad\w*|a precisar)", r"avariad\w*", r"\bavarias?\b",
    r"para repar\w*", r"precisa de repar\w*", r"necessita de repar\w*", r"para recuper\w*", r"a precisar de repar\w*",
    r"para pecas", r"so pecas", r"p/ ?pecas", r"para peca\b",
]
_BODY = [
    r"acidentad\w*", r"sinistrad\w*", r"\bsalvado\b", r"\bbatido\b", r"para abate", r"danos? (?:na|de) (?:frente|traseira|carrocaria)",
]
_NEG = [
    r"sem (?:qualquer |nenhuma )?avaria\w*", r"sem (?:qualquer |nenhum )?problema\w*", r"nunca teve avaria\w*",
    r"nao tem avaria\w*", r"sem acidente\w*", r"nao (?:e|foi) acidentad\w*", r"nao acidentad\w*", r"sem sinistro\w*",
    r"nunca (?:foi )?(?:acidentad\w*|batido)", r"sem batidas?",
]
MECH_RE = [re.compile(p) for p in _MECH]
BODY_RE = [re.compile(p) for p in _BODY]
NEG_RE = [re.compile(p) for p in _NEG]


def detect_issue(*texts):
    """Return ('mech'|'body'|'', matched_phrase)."""
    t = " " + norm(" ".join(x for x in texts if x)) + " "
    for rx in NEG_RE:
        t = rx.sub(" ", t)
    for rx in MECH_RE:
        m = rx.search(t)
        if m:
            return "mech", m.group(0).strip()
    for rx in BODY_RE:
        m = rx.search(t)
        if m:
            return "body", m.group(0).strip()
    return "", ""


# --------------------------------------------------------------------------- #
# Make / model parsing (used to find comparable cars)
# --------------------------------------------------------------------------- #
_BRANDS = [
    "alfa romeo", "land rover", "rolls royce", "aston martin", "mercedes benz", "mercedes", "audi", "bmw", "citroen",
    "dacia", "fiat", "ford", "honda", "hyundai", "kia", "mazda", "mini", "mitsubishi", "nissan", "opel", "vauxhall",
    "peugeot", "porsche", "renault", "seat", "cupra", "skoda", "smart", "suzuki", "toyota", "volvo", "volkswagen", "vw",
    "jeep", "jaguar", "lexus", "saab", "chevrolet", "daihatsu", "subaru", "tesla", "mg", "byd", "lancia", "chrysler",
    "lada", "ssangyong", "maserati", "ferrari", "lamborghini", "bentley", "lotus", "triumph", "ds", "rover", "isuzu",
    "infiniti", "dodge", "cadillac",
]
_BRANDS.sort(key=len, reverse=True)
BRAND_RE = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(b) for b in _BRANDS))
MAKE_DISPLAY = {
    "bmw": "BMW", "mercedes benz": "Mercedes-Benz", "mercedes": "Mercedes-Benz", "vw": "Volkswagen",
    "volkswagen": "Volkswagen", "mini": "MINI", "seat": "SEAT", "ds": "DS", "mg": "MG", "byd": "BYD",
    "citroen": "Citroën", "skoda": "Škoda", "vauxhall": "Opel",
}


def split_make_model(title):
    t = norm(title).replace("-", " ")
    m = BRAND_RE.search(t)
    if not m:
        return "", ""
    key = m.group(0)
    make = MAKE_DISPLAY.get(key, key.title())
    toks = re.findall(r"[a-z0-9]+", t[m.end():])
    while toks and toks[0] in ("serie", "series", "classe", "class", "clase", "modelo"):
        toks = toks[1:]
    model = toks[0] if toks else ""
    if make == "BMW" and re.fullmatch(r"\d{3}[a-z]*", model):
        model = model[0] + "xx"
    if make == "Mercedes-Benz":
        mm = re.fullmatch(r"([a-z]{1,3})\d{2,3}[a-z]*", model)
        if mm:
            model = mm.group(1)
    return make, model


# --------------------------------------------------------------------------- #
# Market comparison and deal scoring
# --------------------------------------------------------------------------- #
def market_stats(con, L):
    """Median price of comparable *working* cars already in the database.
    Returns (median, count, tier). tier 1 = same make+model(+/-3 yrs), tier 2 = rough."""
    make, model, year = L.get("make"), L.get("model"), L.get("year")
    if not make:
        return None, 0, 0
    me = L.get("id") or -1
    base = "SELECT price FROM listings WHERE is_demo=0 AND COALESCE(broken,0)=0 AND price>=700 AND id<>? AND lower(make)=lower(?)"
    if model:
        sql, args = base + " AND lower(model)=lower(?)", [me, make, model]
        tier = 1
        if year:
            sql += " AND year BETWEEN ? AND ?"
            args += [year - 3, year + 3]
        else:
            tier = 2
        prices = [r[0] for r in con.execute(sql, args)]
        if len(prices) >= 3:
            return statistics.median(prices), len(prices), tier
    if year:
        sql = base + " AND year BETWEEN ? AND ?"
        prices = [r[0] for r in con.execute(sql, [me, make, year - 2, year + 2])]
        if len(prices) >= 6:
            return statistics.median(prices), len(prices), 2
    return None, 0, 0


def assess(con, L):
    """Decide what kind of alert a listing deserves."""
    median, n, tier = market_stats(con, L)
    price = L.get("price")
    ratio = None
    if median and price and price >= 500:
        ratio = price / median
    kind = "new"
    if L.get("issue") == "mech":
        kind = "project"
    elif L.get("issue") == "body":
        kind = "accident"
    elif ratio is not None:
        deal = DEAL_RATIO if tier == 1 else DEAL_RATIO - 0.10
        steal = STEAL_RATIO if tier == 1 else STEAL_RATIO - 0.10
        if ratio <= steal:
            kind = "steal"
        elif ratio <= deal:
            kind = "deal"
    return {
        "kind": kind, "reason": L.get("reason", ""), "median": median, "n": n, "tier": tier,
        "ratio": ratio, "suspicious": bool(ratio is not None and ratio < 0.35 and kind in ("steal", "deal")),
    }


# --------------------------------------------------------------------------- #
# Alert text
# --------------------------------------------------------------------------- #
def money(p):
    return "€{:,.0f}".format(p) if p is not None else "price not stated"


def _ago(iso):
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    if mins < 0 or mins > 2880:
        return ""
    if mins < 60:
        return "posted %d min ago" % mins
    return "posted %d h ago" % (mins // 60)


def format_alert(L, a, old_price=None):
    kind = a["kind"]
    price = L.get("price")
    if kind == "drop":
        head = "📉 PRICE DROP: %s → %s" % (money(old_price), money(price))
    elif kind == "steal":
        head = "🔥🔥 STEAL: %d%% below typical" % round((1 - a["ratio"]) * 100)
    elif kind == "deal":
        head = "🔥 DEAL: %d%% below typical" % round((1 - a["ratio"]) * 100)
    elif kind == "project":
        head = "🔧 PROJECT (mechanical): %s" % (a["reason"] or "issue mentioned")
    elif kind == "accident":
        head = "💥 DAMAGED / SALVAGE: %s" % (a["reason"] or "damage mentioned")
    else:
        head = "🚗 New listing"
    facts = [money(price)]
    if L.get("year"):
        facts.append(str(L["year"]))
    if L.get("mileage"):
        facts.append("{:,} km".format(int(L["mileage"])))
    if L.get("fuel"):
        facts.append(L["fuel"])
    lines = [head, L.get("title", ""), " · ".join(facts)]
    place = " · ".join(x for x in [L.get("location") or "Madeira", L.get("source"), _ago(L.get("published_at"))] if x)
    lines.append(place)
    if a.get("median") and price:
        gap = a["median"] - price
        label = "Working ones go for about" if kind == "project" else "Typical price"
        rough = " (rough match)" if a["tier"] == 2 else ""
        lines.append("%s: %s from %d comparable%s%s" % (label, money(a["median"]), a["n"], "" if a["n"] == 1 else "s", rough))
        if kind == "project" and gap > 0:
            lines.append("Gap to cover repair + your margin: %s" % money(gap))
    if a.get("suspicious"):
        lines.append("⚠️ Suspiciously cheap. Check for scam or typo before anything else.")
    if L.get("market_flag"):
        lines.append("Site says: %s" % L["market_flag"])
    lines.append(L.get("url", ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Saved searches (dashboard feature)
# --------------------------------------------------------------------------- #
def listing_matches_search(row, search):
    text = " ".join([row["title"] or "", row["make"] or "", row["model"] or "", row["description"] or ""]).lower()
    if search["keywords"] and not all(k.lower() in text for k in search["keywords"].split() if k.strip()):
        return False
    if search["make"] and search["make"].lower() not in (row["make"] or "").lower():
        return False
    if search["model"] and search["model"].lower() not in (row["model"] or "").lower():
        return False
    if search["min_price"] is not None and (row["price"] is None or row["price"] < search["min_price"]):
        return False
    if search["max_price"] is not None and (row["price"] is None or row["price"] > search["max_price"]):
        return False
    if search["condition"] and search["condition"].lower() not in (row["condition"] or "").lower():
        return False
    return (row["island"] or "").lower() == (search["island"] or "Madeira").lower()


def notify_matching_saved_searches(listing):
    with session() as con:
        searches = con.execute("SELECT * FROM saved_searches WHERE telegram_enabled=1").fetchall()
    for s in searches:
        if listing_matches_search(listing, s):
            price = "€{:,.0f}".format(listing["price"]) if listing["price"] is not None else "not stated"
            msg = "🚗 New CarFinder match: %s\n%s\nPrice: %s\nLocation: %s\nSource: %s\n%s" % (
                s["name"], listing["title"], price, listing["location"] or "Madeira", listing["source"], listing["url"])
            send_telegram(msg)
