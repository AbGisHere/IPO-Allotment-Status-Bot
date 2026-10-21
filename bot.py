#!/usr/bin/env python3
"""Telegram bot that pings you the moment an IPO's allotment goes live, and checks
whether you got shares.

Data sources:
  - Investorgain live IPO table (upcoming/open/closed IPOs, BoA dates, registrar)
  - MUFG Intime (formerly Link Intime): https://in.mpms.mufg.com/Initial_Offer/public-issues.html
  - KFintech:                           https://ipostatus.kfintech.com

Stdlib only (+ the system `openssl` binary for MUFG's token encryption).
Configure with TELEGRAM_BOT_TOKEN (env var or .env file next to this script).
See HELP_TEXT below / send /help to the bot for commands.
"""

__version__ = "1.0.0"

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
FAST_INTERVAL = int(os.environ.get("FAST_INTERVAL", "30"))   # a watched IPO's BoA date has arrived
SLOW_INTERVAL = int(os.environ.get("SLOW_INTERVAL", "300"))  # nothing due yet
STATUS_RETRY_SECS = 3 * 3600  # keep retrying "record not found" PANs this long after allotment goes live

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

IG_BASE = "https://www.investorgain.com"
IG_LIST = IG_BASE + "/report/live-ipo-gmp/331/"
MUFG_BASE = "https://in.mpms.mufg.com/Initial_Offer/"
MUFG_PAGE = MUFG_BASE + "public-issues.html"
MUFG_KEY = b"8080808080808080"  # AES key/IV hardcoded in MUFG's page JS
KFIN_PAGE = "https://ipostatus.kfintech.com/"
KFIN_API = "https://0uz601ms56.execute-api.ap-south-1.amazonaws.com/prod/api/query"

MUFG, KFIN = "MUFG Intime", "KFintech"
REGISTRAR_URLS = {MUFG: MUFG_PAGE, KFIN: KFIN_PAGE}
STATUS_LABELS = {"U": "Upcoming", "O": "Open", "CT": "Closing today", "C": "Closed"}

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

HELP_TEXT = """🤖 <b>IPO Allotment Bot</b>

<b>Finding IPOs</b>
/upcoming — upcoming, open and closed IPOs with their allotment (BoA) date and registrar

<b>Allotment alerts</b>
/select &lt;name&gt; — watch an IPO, e.g. <code>/select TONBO</code>. I work out its registrar (MUFG Intime or KFintech) and message you the moment allotment goes live
/unselect &lt;name&gt; — stop watching
/list — what you're watching
/check — check the registrars right now
/clear — wipe this chat (Telegram only lets me delete messages from the last 48h)
/all on|off — also alert on <i>every</i> new allotment, not just watched ones

<b>Did I get it?</b>
/pan &lt;PAN&gt; [label] — save a PAN, e.g. <code>/pan ABCDE1234F Mom</code>. Add as many as you like
/pan — list saved PANs
/pan remove &lt;PAN or label&gt; — delete one
/status &lt;name&gt; — check allotment for all saved PANs, e.g. <code>/status TONBO</code>
/status — check every watched IPO that's out

When a watched IPO goes live I check your PANs automatically and tell you if you got shares.

<i>Name matching is fuzzy: "tonbo", "TONBO" or "Tonbo Imaging" all work.</i>"""

BOT_COMMANDS = [
    ("upcoming", "Upcoming / open / closed IPOs"),
    ("select", "Watch an IPO: /select TONBO"),
    ("unselect", "Stop watching an IPO"),
    ("list", "Show watched IPOs"),
    ("status", "Did I get allotment? /status TONBO"),
    ("pan", "Add/list PANs: /pan ABCDE1234F"),
    ("check", "Check registrars now"),
    ("all", "Alert on every new allotment: on|off"),
    ("clear", "Clear this chat"),
    ("help", "How to use this bot"),
]


def load_env():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def log(*args):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *args, flush=True)


def http(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def norm(name):
    """'Tonbo Imaging India Limited - IPO' -> 'tonboimagingindia'"""
    s = name.lower().replace("&", "and")
    s = re.sub(r"\b(limited|ltd|sme|ipo|fpo|nse|bse)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mask_pan(pan):
    return pan[:2] + "xxxxx" + pan[-3:]


# ---------------------------------------------------------------- investorgain (IPO calendar)

def _rsc_text(page):
    """Next.js pages stream their data as self.__next_f.push([1,"..."]) string chunks."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', page)
    return "".join(json.loads('"' + c + '"') for c in chunks)


def _rsc_json(text, key):
    i = text.find(f'"{key}":')
    if i < 0:
        return None
    return json.JSONDecoder().raw_decode(text, i + len(key) + 3)[0]


_ig_cache = {"at": 0, "ipos": []}


def fetch_ipos(max_age=600):
    """IPOs from Investorgain: [{name, slug, status, category, open, close, boa, listing}]."""
    if time.time() - _ig_cache["at"] < max_age:
        return _ig_cache["ipos"]
    rows = _rsc_json(_rsc_text(http(IG_LIST)), "reportTableData") or []
    if not rows:
        raise RuntimeError("Investorgain: couldn't find IPO table")
    strip = lambda v: re.sub(r"GMP:.*$", "", re.sub(r"<[^>]+>", "", str(v))).strip()
    ipos = [{
        "name": r["~ipo_name"].strip(),
        "slug": r.get("~urlrewrite_folder_name", ""),
        "status": r.get("~ipo_status1", ""),
        "category": r.get("~IPO_Category", ""),
        "open": strip(r.get("Open", "")),
        "close": strip(r.get("Close", "")),
        "boa": r.get("~Srt_BoA_Dt", ""),  # YYYY-MM-DD
        "boa_label": strip(r.get("BoA Dt", "")),
        "listing": strip(r.get("Listing", "")),
    } for r in rows if r.get("~ipo_name")]
    _ig_cache.update(at=time.time(), ipos=ipos)
    return ipos


def fetch_registrar(state, slug):
    """Registrar name for an IPO (cached forever in state)."""
    cache = state.setdefault("registrar_cache", {})
    if slug in cache:
        return cache[slug]
    info = _rsc_json(_rsc_text(http(IG_BASE + slug)), "registrarInfo") or []
    name = info[0].get("registrar_name", "").strip() if info else ""
    if name:
        cache[slug] = name
    return name or "Unknown"


def registrar_key(registrar_name):
    """Map a registrar's full name to one we can monitor, or None."""
    n = (registrar_name or "").lower()
    if "kfin" in n or "karvy" in n:
        return KFIN
    if "mufg" in n or "link intime" in n:
        return MUFG
    return None


def find_ipos(query, ipos):
    q = norm(query)
    return [i for i in ipos if q and q in norm(i["name"])]


# ---------------------------------------------------------------- registrars: company lists

def fetch_mufg():
    """{company_id: name} from MUFG Intime's dropdown."""
    body = http(MUFG_BASE + "IPO.aspx/GetDetails", data=b"{}", headers={
        "Content-Type": "application/json; charset=utf-8", "Referer": MUFG_PAGE})
    root = ET.fromstring(json.loads(body)["d"])
    return {t.findtext("company_id").strip(): t.findtext("companyname").strip()
            for t in root.iter("Table")}


_kfin_cache = {"bundle": None, "companies": {}}


def fetch_kfin():
    """{clientId: name}. KFintech hardcodes the list in its JS bundle and redeploys when a
    new IPO is added, so only re-download when the bundle hash changes."""
    m = re.search(r'src="\.?/?(static/js/main\.[0-9a-f]+\.js)"', http(KFIN_PAGE))
    if not m:
        raise RuntimeError("KFintech: couldn't find main JS bundle in page")
    bundle = m.group(1)
    if bundle != _kfin_cache["bundle"]:
        js = http(KFIN_PAGE + bundle, headers={"Referer": KFIN_PAGE})
        m2 = re.search(r"JSON\.parse\('(\[\{\"clientId\".*?\])'\)", js)
        if not m2:
            raise RuntimeError("KFintech: couldn't find company list in JS bundle")
        _kfin_cache["companies"] = {c["clientId"]: c["name"].strip() for c in json.loads(m2.group(1))}
        _kfin_cache["bundle"] = bundle
    return _kfin_cache["companies"]


COMPANY_FETCHERS = {MUFG: fetch_mufg, KFIN: fetch_kfin}


def match_company(name, companies):
    """Find an IPO (Investorgain name or raw keyword) in a registrar's {id: name} list."""
    q = norm(name)
    for cid, cname in companies.items():
        if q and q in norm(cname):
            return cid, cname
    return None


# ---------------------------------------------------------------- registrars: allotment status

def _aes_encrypt_b64(text):
    key = MUFG_KEY.hex()
    out = subprocess.run(["openssl", "enc", "-aes-128-cbc", "-K", key, "-iv", key, "-base64", "-A"],
                         input=text.encode(), capture_output=True, check=True)
    return out.stdout.decode().strip()


def status_mufg(cid, pan):
    """-> {'applied', 'allotted', 'category', 'name'} or None if no record."""
    hdrs = {"Content-Type": "application/json; charset=utf-8", "Referer": MUFG_PAGE}
    token = json.loads(http(MUFG_BASE + "IPO.aspx/generateToken", data=b"{}", headers=hdrs))["d"]
    body = json.dumps({"clientid": cid, "PAN": pan, "IFSC": "", "CHKVAL": "1",
                       "token": _aes_encrypt_b64(str(token))}).encode()
    root = ET.fromstring(json.loads(http(MUFG_BASE + "IPO.aspx/SearchOnPan", data=body, headers=hdrs))["d"])
    msg = root.findtext("Table1/Msg")
    if msg:
        raise RuntimeError(f"MUFG: {msg.strip()}")
    rows = root.findall("Table")
    if not rows:
        return None
    return {"applied": sum(int(r.findtext("SHARES") or 0) for r in rows),
            "allotted": sum(int(r.findtext("ALLOT") or 0) for r in rows),
            "category": rows[0].findtext("PEMNDG", "").strip(),
            "name": rows[0].findtext("NAME1", "").strip()}


def status_kfin(cid, pan):
    headers = {"reqparam": pan, "client_id": cid, "Origin": KFIN_PAGE.rstrip("/"), "Referer": KFIN_PAGE}
    for attempt in range(4):
        try:
            body = http(KFIN_API + "?type=pan", headers=headers)
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 * 2 ** attempt)
                continue
            raise
    data = json.loads(body)
    rows = data.get("data", data) if isinstance(data, dict) else data
    rows = rows if isinstance(rows, list) else [rows]
    if not rows:
        return None
    return {"applied": sum(int(float(r.get("App_Shares") or 0)) for r in rows),
            "allotted": sum(int(float(r.get("All_Shares") or 0)) for r in rows),
            "category": str(rows[0].get("category", "")).strip(),
            "name": str(rows[0].get("Name", "")).strip()}


STATUS_FETCHERS = {MUFG: status_mufg, KFIN: status_kfin}


def format_status(label, res):
    who = esc(label)
    if res is None:
        return f"• {who}: no application found"
    cat = f" ({esc(res['category'])})" if res["category"] else ""
    if res["allotted"] > 0:
        return f"• {who}: 🎉 <b>Allotted {res['allotted']}</b> of {res['applied']} shares{cat}"
    return f"• {who}: 😞 Not allotted (applied {res['applied']}){cat}"


# ---------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"owner_chat_id": None, "watchlist": [], "pans": [], "notify_all": False,
            "seen": {}, "registrar_cache": {}, "tg_offset": 0}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def pan_label(p):
    return f"{p['label']} ({mask_pan(p['pan'])})" if p.get("label") else mask_pan(p["pan"])


# ---------------------------------------------------------------- telegram

class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **params):
        data = urllib.parse.urlencode(params).encode()
        # long-poll timeout (getUpdates) + headroom for the HTTP request itself
        return json.loads(http(self.base + method, data=data, timeout=int(params.get("timeout", 0)) + 15))

    def send(self, chat_id, text):
        for i in range(0, len(text), 4000):  # Telegram's limit is 4096 chars
            try:
                self.call("sendMessage", chat_id=chat_id, text=text[i:i + 4000], parse_mode="HTML",
                          disable_web_page_preview="true")
            except Exception as e:
                log("send failed:", e)


# ---------------------------------------------------------------- core

def check_statuses(state, item, pans, final=False):
    """Check allotment for `pans` on a found item. Returns (lines, pans_still_not_found)."""
    found = item["found"]
    fetch = STATUS_FETCHERS[found["registrar"]]
    lines, missing = [], []
    for p in pans:
        try:
            res = fetch(found["cid"], p["pan"])
        except Exception as e:
            log(f"status check failed for {item['name']}:", e)
            lines.append(f"• {esc(pan_label(p))}: ⚠️ lookup failed ({esc(str(e))[:80]})")
            continue
        if res is None and not final:
            missing.append(p)
        else:
            lines.append(format_status(pan_label(p), res))
    return lines, missing


def check_registrars(state, tg):
    """Poll registrars for watched IPOs; alert + auto-check PANs when one goes live."""
    chat = state["owner_chat_id"]
    pending = [w for w in state["watchlist"] if not w.get("found")]
    needed = {w["registrar"] for w in pending if w.get("registrar")}
    if any(not w.get("registrar") for w in pending) or state["notify_all"]:
        needed = set(COMPANY_FETCHERS)

    summary = []
    for reg in COMPANY_FETCHERS:
        if reg not in needed:
            continue
        try:
            companies = COMPANY_FETCHERS[reg]()
        except Exception as e:
            log(f"{reg} fetch failed:", e)
            summary.append(f"⚠️ {reg}: fetch failed ({esc(str(e))[:100]})")
            continue
        summary.append(f"✅ {reg}: {len(companies)} IPOs live")

        for w in pending:
            if w.get("found") or (w.get("registrar") and w["registrar"] != reg):
                continue
            hit = match_company(w["name"], companies)
            if not hit:
                continue
            cid, cname = hit
            w["found"] = {"registrar": reg, "cid": cid, "name": cname, "at": time.time()}
            log(f"ALLOTMENT LIVE: {cname} on {reg}")
            msg = (f"🚨 <b>Allotment is out!</b>\n\n<b>{esc(cname)}</b>\n"
                   f"Registrar: {reg}\n{REGISTRAR_URLS[reg]}")
            if state["pans"]:
                lines, missing = check_statuses(state, w, state["pans"])
                w["retry_pans"] = [p["pan"] for p in missing]
                if missing:
                    lines.append(f"• {len(missing)} PAN(s): no record yet. I'll keep checking.")
                msg += "\n\n<b>Your allotment</b>\n" + "\n".join(lines)
            else:
                msg += "\n\nAdd a PAN with /pan to have me check if you got it."
            if chat:
                tg.send(chat, msg)

        seen = set(state["seen"].get(reg, []))
        if state["notify_all"] and reg in state["seen"]:  # never alert on the first-ever fetch
            watched = {w["found"]["cid"] for w in state["watchlist"] if w.get("found")}
            for cid, cname in companies.items():
                if cid not in seen and cid not in watched and chat:
                    tg.send(chat, f"🆕 New allotment live: <b>{esc(cname)}</b>\n"
                                  f"Registrar: {reg}\n{REGISTRAR_URLS[reg]}")
        state["seen"][reg] = list(companies)

    retry_missing_pans(state, tg)
    save_state(state)
    return "\n".join(summary) or "Nothing to check (watchlist empty — try /select)."


def retry_missing_pans(state, tg):
    """Registrars sometimes list an IPO before its data is uploaded; retry not-found PANs."""
    for w in state["watchlist"]:
        if not (w.get("found") and w.get("retry_pans")) or time.time() - w["found"]["at"] < 60:
            continue
        final = time.time() - w["found"]["at"] > STATUS_RETRY_SECS
        pans = [p for p in state["pans"] if p["pan"] in w["retry_pans"]]
        lines, missing = check_statuses(state, w, pans, final=final)
        w["retry_pans"] = [p["pan"] for p in missing]
        if lines and state["owner_chat_id"]:
            tg.send(state["owner_chat_id"], f"<b>{esc(w['found']['name'])}</b> — allotment update\n"
                    + "\n".join(lines))


def next_interval(state):
    today = date.today().isoformat()
    due = any(not w.get("found") and (not w.get("boa") or w["boa"] <= today) for w in state["watchlist"])
    retrying = any(w.get("retry_pans") for w in state["watchlist"])
    return FAST_INTERVAL if due else (120 if retrying or state["notify_all"] else SLOW_INTERVAL)


# ---------------------------------------------------------------- commands

def cmd_upcoming(state, tg, chat_id, arg):
    tg.send(chat_id, "⏳ Fetching IPO calendar…")
    ipos = [i for i in fetch_ipos() if i["status"] in STATUS_LABELS]
    with ThreadPoolExecutor(8) as ex:
        regs = list(ex.map(lambda i: _safe_registrar(state, i["slug"]), ipos))
    save_state(state)
    watched = {w["name"] for w in state["watchlist"]}
    groups = {}
    for ipo, reg in zip(ipos, regs):
        groups.setdefault(ipo["status"], []).append((ipo, reg))
    out = ["📅 <b>IPO calendar</b>"]
    for status in ("C", "CT", "O", "U"):
        if status not in groups:
            continue
        out.append(f"\n<b>{STATUS_LABELS[status]}</b>")
        for ipo, reg in sorted(groups[status], key=lambda x: x[0]["boa"]):
            mark = "👀 " if ipo["name"] in watched else ""
            sme = " [SME]" if ipo["category"] == "SME" else ""
            tracked = "" if registrar_key(reg) else " ⚠️"
            out.append(f"{mark}<b>{esc(ipo['name'])}</b>{sme}\n"
                       f"   {ipo['open']}–{ipo['close']} · Allotment <b>{esc(ipo['boa_label'])}</b>"
                       f" · Lists {esc(ipo['listing'])}\n   Registrar: {esc(reg)}{tracked}")
    out.append("\n/select &lt;name&gt; to get alerted.  ⚠️ = registrar I can't monitor yet.")
    tg.send(chat_id, "\n".join(out))


def _safe_registrar(state, slug):
    try:
        return fetch_registrar(state, slug)
    except Exception as e:
        log("registrar lookup failed:", slug, e)
        return "Unknown"


def cmd_select(state, tg, chat_id, arg):
    if not arg:
        return tg.send(chat_id, "Usage: /select TONBO")
    matches = find_ipos(arg, fetch_ipos())
    if len(matches) > 1:
        exact = [m for m in matches if norm(m["name"]) == norm(arg)]
        if len(exact) != 1:
            names = "\n".join(f"• {esc(m['name'])}" for m in matches)
            return tg.send(chat_id, f"Multiple matches, be more specific:\n{names}")
        matches = exact
    if matches:
        ipo = matches[0]
        reg_full = fetch_registrar(state, ipo["slug"])
        item = {"name": ipo["name"], "registrar": registrar_key(reg_full),
                "registrar_full": reg_full, "boa": ipo["boa"], "found": None}
    else:  # not on the calendar: fall back to watching a raw keyword on both registrars
        item = {"name": arg, "registrar": None, "registrar_full": None, "boa": None, "found": None}

    if any(norm(w["name"]) == norm(item["name"]) for w in state["watchlist"]):
        return tg.send(chat_id, f"Already watching <b>{esc(item['name'])}</b>.")
    if item["registrar_full"] and not item["registrar"]:
        return tg.send(chat_id, f"<b>{esc(item['name'])}</b>'s registrar is {esc(item['registrar_full'])}, "
                                f"which I can't monitor yet (only MUFG Intime and KFintech).")
    state["watchlist"].append(item)
    save_state(state)

    if item["registrar"]:
        msg = (f"👀 Watching <b>{esc(item['name'])}</b>\nRegistrar: {item['registrar']}\n"
               f"Allotment expected: {esc(item['boa'] or '?')}")
    else:
        msg = (f"👀 Watching for “{esc(item['name'])}” on MUFG Intime and KFintech "
               f"(it isn't on the IPO calendar, so I'll match the name as-is).")
    if not state["pans"]:
        msg += "\n\nTip: add your PAN with /pan so I can tell you if you got shares."
    tg.send(chat_id, msg)
    check_registrars(state, tg)
    if not item["found"]:
        tg.send(chat_id, "Not out yet. I'll message you the moment it is.")


def cmd_unselect(state, tg, chat_id, arg):
    q = norm(arg)
    keep = [w for w in state["watchlist"] if not (q and q in norm(w["name"]))]
    removed = [w["name"] for w in state["watchlist"] if w not in keep]
    state["watchlist"] = keep
    save_state(state)
    tg.send(chat_id, f"Stopped watching: {esc(', '.join(removed))}" if removed else "No match in your watchlist.")


def cmd_list(state, tg, chat_id, arg):
    if not state["watchlist"]:
        return tg.send(chat_id, "Not watching anything. Try /upcoming then /select &lt;name&gt;.")
    lines = []
    for w in state["watchlist"]:
        st = f"✅ out on {w['found']['registrar']}" if w.get("found") else f"⏳ expected {w.get('boa') or '?'}"
        lines.append(f"• <b>{esc(w['name'])}</b> ({w.get('registrar') or 'both registrars'}) — {st}")
    tg.send(chat_id, "<b>Watching</b>\n" + "\n".join(lines)
            + ("\n\nAlso alerting on IPOs you haven't selected (/all off to stop)." if state["notify_all"]
               else "\n\nOnly alerting on the IPOs above (/all on to also get every other IPO)."))


def cmd_status(state, tg, chat_id, arg):
    if not state["pans"]:
        return tg.send(chat_id, "Add a PAN first: /pan ABCDE1234F")
    if arg:
        q = norm(arg)
        items = [w for w in state["watchlist"] if q in norm(w["name"])]
        if not items:  # not watched: look it up on the fly
            ipos = find_ipos(arg, fetch_ipos())
            if len(ipos) == 1:
                reg = registrar_key(fetch_registrar(state, ipos[0]["slug"]))
                items = [{"name": ipos[0]["name"], "registrar": reg, "boa": ipos[0]["boa"], "found": None}]
            else:
                items = [{"name": arg, "registrar": None, "boa": None, "found": None}]
    else:
        items = [w for w in state["watchlist"] if w.get("found")]
        if not items:
            return tg.send(chat_id, "None of your watched IPOs are out yet. Use /status &lt;name&gt;.")

    for w in items:
        if not w.get("found"):  # check whether it has gone live since the last poll
            for reg in ([w["registrar"]] if w.get("registrar") else COMPANY_FETCHERS):
                hit = match_company(w["name"], COMPANY_FETCHERS[reg]())
                if hit:
                    w["found"] = {"registrar": reg, "cid": hit[0], "name": hit[1], "at": time.time()}
                    break
        if not w.get("found"):
            tg.send(chat_id, f"⏳ <b>{esc(w['name'])}</b>: allotment isn't out yet"
                             + (f" (expected {esc(w['boa'])})." if w.get("boa") else "."))
            continue
        lines, _ = check_statuses(state, w, state["pans"], final=True)
        tg.send(chat_id, f"<b>{esc(w['found']['name'])}</b> ({w['found']['registrar']})\n" + "\n".join(lines))
    save_state(state)


def cmd_pan(state, tg, chat_id, arg, message_id=None):
    parts = arg.split()
    if not parts:
        if not state["pans"]:
            return tg.send(chat_id, "No PANs saved. Add one: /pan ABCDE1234F [label]")
        return tg.send(chat_id, "<b>Saved PANs</b>\n" + "\n".join(f"• {esc(pan_label(p))}" for p in state["pans"]))
    if parts[0].lower() == "remove" and len(parts) > 1:
        key = " ".join(parts[1:])
        before = len(state["pans"])
        state["pans"] = [p for p in state["pans"]
                         if p["pan"] != key.upper() and (p.get("label") or "").lower() != key.lower()]
        save_state(state)
        return tg.send(chat_id, "Removed." if len(state["pans"]) < before else "No such PAN.")

    pan = parts[0].upper()
    if not PAN_RE.match(pan):
        return tg.send(chat_id, "That doesn't look like a PAN (format: ABCDE1234F).")
    label = " ".join(parts[1:])[:30]
    state["pans"] = [p for p in state["pans"] if p["pan"] != pan] + [{"pan": pan, "label": label}]
    save_state(state)
    if message_id:  # don't leave the PAN sitting in the chat history
        try:
            tg.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
    tg.send(chat_id, f"🔐 Saved {esc(pan_label({'pan': pan, 'label': label}))}. "
                     f"(I deleted your message so the PAN isn't left in the chat.)")


def cmd_check(state, tg, chat_id, arg):
    tg.send(chat_id, check_registrars(state, tg))


def cmd_all(state, tg, chat_id, arg):
    if arg.lower() not in ("on", "off"):
        return tg.send(chat_id, "Usage: /all on  or  /all off")
    state["notify_all"] = arg.lower() == "on"
    save_state(state)
    tg.send(chat_id, f"Alert on every new allotment: {arg.lower()}")


def cmd_clear(state, tg, chat_id, arg, message_id=None):
    """Bots can't list chat history, but message ids in a private chat are sequential,
    so delete every id from the last clear up to this /clear (deleteMessages skips gaps)."""
    if not message_id:
        return
    start = max(state.get("clear_from", 1), message_id - 2000)
    ids = list(range(start, message_id + 1))
    for i in range(0, len(ids), 100):  # API limit: 100 ids per call
        try:
            tg.call("deleteMessages", chat_id=chat_id, message_ids=json.dumps(ids[i:i + 100]))
        except Exception as e:  # e.g. every id in the batch is older than 48h
            log("deleteMessages batch failed:", e)
    state["clear_from"] = message_id + 1
    save_state(state)


COMMANDS = {
    "/upcoming": cmd_upcoming, "/select": cmd_select, "/add": cmd_select,
    "/unselect": cmd_unselect, "/remove": cmd_unselect, "/list": cmd_list,
    "/status": cmd_status, "/pan": cmd_pan, "/check": cmd_check, "/all": cmd_all,
    "/clear": cmd_clear,
}


def handle_command(state, tg, chat_id, text, message_id=None):
    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()

    if state["owner_chat_id"] is None and cmd == "/start":
        state["owner_chat_id"] = chat_id
        save_state(state)
    if chat_id != state["owner_chat_id"]:
        return tg.send(chat_id, "This bot is private.")

    if cmd in ("/start", "/help"):
        return tg.send(chat_id, HELP_TEXT)
    fn = COMMANDS.get(cmd)
    if not fn:
        return tg.send(chat_id, "Unknown command. Try /help")
    try:
        if fn in (cmd_pan, cmd_clear):
            fn(state, tg, chat_id, arg, message_id)
        else:
            fn(state, tg, chat_id, arg)
    except Exception as e:
        log(f"{cmd} failed:", e)
        tg.send(chat_id, f"⚠️ {cmd} failed: {esc(str(e))[:200]}")


def main():
    load_env()
    state = load_state()

    if "--check-once" in sys.argv:  # dry run: print calendar + registrar lists, no Telegram needed
        for ipo in fetch_ipos():
            if ipo["status"] in STATUS_LABELS:
                print(f"{STATUS_LABELS[ipo['status']]:<14} BoA {ipo['boa']}  {ipo['name']}"
                      f"  [{fetch_registrar(state, ipo['slug'])}]")
        for reg, fetch in COMPANY_FETCHERS.items():
            print(f"{reg}: {len(fetch())} IPOs live")
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env (see README.md)")
    tg = Telegram(token)
    try:
        tg.call("setMyCommands", commands=json.dumps([{"command": c, "description": d} for c, d in BOT_COMMANDS]))
    except Exception as e:
        log("setMyCommands failed:", e)
    log(f"Bot running. Watching: {[w['name'] for w in state['watchlist']]}")

    next_check = 0
    while True:
        if time.time() >= next_check:
            try:
                check_registrars(state, tg)
            except Exception as e:
                log("check error:", e)
            next_check = time.time() + next_interval(state)
        try:
            wait = max(1, min(25, int(next_check - time.time())))
            res = tg.call("getUpdates", offset=state["tg_offset"], timeout=wait)
            for upd in res.get("result", []):
                state["tg_offset"] = upd["update_id"] + 1
                save_state(state)
                msg = upd.get("message") or {}
                if msg.get("text", "").startswith("/"):
                    handle_command(state, tg, msg["chat"]["id"], msg["text"], msg.get("message_id"))
        except Exception as e:
            log("telegram error:", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
