#!/usr/bin/env python3
"""
Montana Bill Watch: track LC drafts and bills, screen them for tribal interests,
consultation provisions, and AIGPI Pathways areas, track amendments, and publish
a feed for the Bill Watch app plus push/email alerts.

Usage
  python billwatch.py init
  python billwatch.py sync                 # pull drafts, text, versions, fiscal notes from Bill Explorer
  python billwatch.py backfill --year 2023 # load a past session (1999 on) for retrospective AIGPI review
  python billwatch.py ingest-csv FILE.csv  # fallback if Bill Explorer blocks automated access
  python billwatch.py reviews              # pull AIGPI review decisions from Supabase
  python billwatch.py screen               # re-score everything after editing config.yaml
  python billwatch.py digest [--send]      # alert on new activity
  python billwatch.py feed                 # write the app feed into docs/
  python billwatch.py report [--out F]     # flagged items -> CSV
  python billwatch.py serve                # open the app locally at http://localhost:8000
  python billwatch.py run [--send]         # everything above in order (for the scheduler)
"""
import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import shutil
import smtplib
import sqlite3
import sys
import zlib
from email.message import EmailMessage
from pathlib import Path

import requests
import yaml

from analysis import Analyzer, compare_fiscal, compare_versions
from legmt import LegMT, LegMTError, pdf_to_text, version_name

HERE = Path(__file__).resolve().parent
SESSION_LABELS = {"2": "2025", "3": "2027"}  # fallback; real labels are saved at sync time


def session_label(db, sid):
    return meta_get(db, f"label:{sid}") or SESSION_LABELS.get(str(sid)) or str(sid)


# --------------------------------------------------------------------------- setup
def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    base = Path(path).resolve().parent
    data = Path(cfg.get("data_dir", "data"))
    cfg["_data"] = data if data.is_absolute() else base / data
    site = Path(cfg.get("site_dir", "docs"))
    cfg["_site"] = site if site.is_absolute() else base / site
    for sub in ("texts", "digests", "inbox", "inbox/processed"):
        (cfg["_data"] / sub).mkdir(parents=True, exist_ok=True)
    cfg["_site"].mkdir(parents=True, exist_ok=True)
    return cfg


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  key TEXT PRIMARY KEY, session_id TEXT, lc TEXT, be_id INTEGER, title TEXT,
  bill_number TEXT, bill_type TEXT, bill_no INTEGER, requester TEXT, requester_type TEXT,
  by_request_of TEXT, sponsor TEXT, subjects TEXT, status TEXT, status_date TEXT,
  history TEXT, url TEXT, source TEXT, first_seen TEXT, last_changed TEXT, text_checked TEXT);
CREATE TABLE IF NOT EXISTS docs (
  doc_id INTEGER PRIMARY KEY, key TEXT, kind TEXT, version TEXT, file TEXT, date TEXT,
  url TEXT, path TEXT, text TEXT, fetched TEXT);
CREATE INDEX IF NOT EXISTS docs_key ON docs(key, kind);
CREATE TABLE IF NOT EXISTS analysis (
  key TEXT PRIMARY KEY, tags TEXT, tribal TEXT, tribal_score INTEGER, signals TEXT,
  consultation TEXT, consultation_evidence TEXT, mca TEXT, basis TEXT, gap INTEGER,
  text_version TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, kind TEXT, from_label TEXT, to_label TEXT,
  summary TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS reviews (
  key TEXT PRIMARY KEY, tribal_interest TEXT, consultation TEXT, aigpi_tracking INTEGER,
  public_note TEXT, reviewer TEXT, reviewed_version TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS calendar (key TEXT PRIMARY KEY, hearings TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, item_key TEXT, kind TEXT, detail TEXT,
  digested TEXT, priority INTEGER DEFAULT 0);
"""


def connect(cfg):
    db = sqlite3.connect(cfg["_data"] / "billwatch.db")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    migrate_v1(db)
    return db


def migrate_v1(db):
    """Carry the version 1 database (lc/flags/bills tables) forward."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ev_cols = {r[1] for r in db.execute("PRAGMA table_info(events)")}
    if "priority" not in ev_cols:
        db.execute("ALTER TABLE events ADD COLUMN priority INTEGER DEFAULT 0")
    if "lc" in tables:
        db.execute("""INSERT OR IGNORE INTO items (key, session_id, lc, title, requester, by_request_of,
                      status, status_date, url, source, first_seen, last_changed)
                      SELECT 'lc:'||session||':'||lc, session, lc, title, requestor, by_request_of,
                      status, status_date, url, 'csv', first_seen, last_changed FROM lc""")
        db.execute("DROP TABLE lc")
    for t in ("flags", "bills", "bill_texts"):
        if t in tables:
            db.execute(f"DROP TABLE {t}")
    db.commit()


def add_event(db, key, kind, detail, priority=0):
    db.execute("INSERT INTO events (ts,item_key,kind,detail,priority) VALUES (?,?,?,?,?)",
               (now(), key, kind, detail, priority))


def meta_get(db, k):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def meta_set(db, k, v):
    db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def pack(text):
    """Store document text compressed; bill text is large and the database lives in git."""
    return zlib.compress((text or "").encode("utf-8"), 9) if text else b""


def unpack(blob):
    if not blob:
        return ""
    if isinstance(blob, str):
        return blob
    return zlib.decompress(blob).decode("utf-8")


def jl(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else [])
    except (TypeError, ValueError):
        return default if default is not None else []


# --------------------------------------------------------------------------- screening
def latest_text(db, key):
    """Latest bill version if introduced, otherwise latest LC draft text."""
    for kind in ("version", "lc"):
        r = db.execute("""SELECT version, text FROM docs WHERE key=? AND kind=? AND length(text) > 0
                          ORDER BY date DESC LIMIT 1""", (key, kind)).fetchone()
        if r:
            return unpack(r["text"]), (r["version"] if kind == "version" else "LC draft")
    return None, None


def screen_item(db, an, key):
    it = db.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
    if not it:
        return None
    text, ver = latest_text(db, key)
    r = an.analyze(it["title"], text, jl(it["subjects"]), it["requester"], it["by_request_of"])
    old = db.execute("SELECT * FROM analysis WHERE key=?", (key,)).fetchone()
    db.execute("""INSERT INTO analysis VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(key) DO UPDATE SET tags=excluded.tags, tribal=excluded.tribal,
                  tribal_score=excluded.tribal_score, signals=excluded.signals,
                  consultation=excluded.consultation, consultation_evidence=excluded.consultation_evidence,
                  mca=excluded.mca, basis=excluded.basis, gap=excluded.gap,
                  text_version=excluded.text_version, updated=excluded.updated""",
               (key, json.dumps(r["tags"]), r["tribal"], r["tribal_score"], json.dumps(r["signals"]),
                r["consultation"], json.dumps(r["consultation_evidence"]), json.dumps(r["mca"]),
                r["basis"], int(r["gap"]), ver, now()))
    rank = {"none": 0, "possible": 1, "likely": 2}
    if r["tribal"] != "none" and (old is None or rank[r["tribal"]] > rank.get(old["tribal"], 0)):
        add_event(db, key, "flagged", f"Tribal interests {r['tribal']} ({r['basis']})", 1 if r["tribal"] == "likely" else 0)
    if r["gap"] and r["basis"] == "full text" and (old is None or not old["gap"] or old["basis"] != "full text"):
        add_event(db, key, "gap", "Text has no tribal consultation provision"
                  + (" (notice only)" if r["consultation"] == "notice only" else ""), 1)
    return r


def rescreen(cfg, session=None):
    db, an = connect(cfg), Analyzer(cfg)
    q, args = "SELECT key FROM items", ()
    if session:
        q, args = q + " WHERE session_id=?", (str(session),)
    keys = [r["key"] for r in db.execute(q, args)]
    for k in keys:
        screen_item(db, an, k)
    db.commit()
    print(f"Re-screened {len(keys)} items")


# --------------------------------------------------------------------------- Bill Explorer sync
def upsert_item(db, cfg, n, source="legmt"):
    """Store one normalized record; return (key, what_changed)."""
    key = f"lc:{n['session_id']}:{n['lc']}"
    url = LegMT.explorer_url(n["session_id"], n["lc"], bool(n["bill_number"]))
    cur = db.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
    vals = dict(session_id=str(n["session_id"]), lc=n["lc"], be_id=n.get("be_id"), title=n["title"],
                bill_number=n.get("bill_number"), bill_type=n.get("bill_type"), bill_no=n.get("bill_no"),
                requester=n.get("requester", ""), requester_type=n.get("requester_type"),
                by_request_of=n.get("by_request_of", ""), sponsor=n.get("sponsor", ""),
                subjects=json.dumps(n.get("subjects") or []), status=n["status"],
                status_date=n.get("status_date"), history=json.dumps(n.get("history") or []),
                url=url, source=source)
    if cur is None:
        cols = ",".join(["key"] + list(vals) + ["first_seen", "last_changed"])
        db.execute(f"INSERT INTO items ({cols}) VALUES ({','.join('?' * (len(vals) + 3))})",
                   [key] + list(vals.values()) + [now(), now()])
        what = "introduced" if n.get("bill_number") else "new"
        add_event(db, key, "new_request" if what == "new" else "introduced",
                  f"New draft request: {n['title']}" if what == "new"
                  else f"{n['bill_number']} introduced: {n['title']}")
        return key, what
    changed = None
    if n.get("bill_number") and not cur["bill_number"]:
        add_event(db, key, "introduced", f"Introduced as {n['bill_number']}", 1)
        changed = "introduced"
    elif cur["status"] != n["status"]:
        add_event(db, key, "status_change", f"{cur['status']} → {n['status']}")
        changed = "status"
    # keep names from a richer source (CSV has requester names even if a lookup failed)
    for f in ("requester", "by_request_of", "sponsor"):
        if not vals[f] and cur[f]:
            vals[f] = cur[f]
    sets = ",".join(f"{k}=?" for k in vals) + (",last_changed=?" if changed else "")
    db.execute(f"UPDATE items SET {sets} WHERE key=?",
               list(vals.values()) + ([now()] if changed else []) + [key])
    return key, changed


def store_doc(cfg, db, api, key, kind, doc, label):
    raw = api.content(doc["doc_id"])
    safe = re.sub(r"[^\w.()-]", "_", doc["file"] or f"{doc['doc_id']}.pdf")
    p = cfg["_data"] / "texts" / f"{key.replace(':', '_')}__{kind}__{safe}"
    p.write_bytes(raw)
    text = pdf_to_text(raw)
    db.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?,?,?,?,?,?)",
               (doc["doc_id"], key, kind, label, doc["file"], doc["date"], doc["url"], str(p), pack(text), now()))
    return text


def fetch_documents(cfg, db, api, an, key):
    """Pull new LC text, bill versions, fiscal notes, and amendments; record what changed."""
    it = db.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
    sid = it["session_id"]
    have = {r["doc_id"] for r in db.execute("SELECT doc_id FROM docs WHERE key=?", (key,))}
    prior_versions = db.execute("SELECT version, text FROM docs WHERE key=? AND kind IN ('lc','version') "
                                "AND length(text) > 0 ORDER BY date", (key,)).fetchall()
    prev_text = unpack(prior_versions[-1]["text"]) if prior_versions else None
    prev_label = prior_versions[-1]["version"] if prior_versions else None
    new_text = False

    def record_version(text, label, title):
        nonlocal prev_text, prev_label, new_text
        new_text = True
        if prev_text:
            ch = compare_versions(an, prev_text, text, prev_label, label, it["title"])
            db.execute("INSERT INTO changes (key,kind,from_label,to_label,summary,created) VALUES (?,?,?,?,?,?)",
                       (key, "version", prev_label, label, json.dumps(ch), now()))
            bits = [f"{ch['n_added']} passages added, {ch['n_removed']} removed"]
            if ch["consultation_change"]:
                bits.append(f"consultation {ch['consultation_change']}")
            if ch["tribal_change"]:
                bits.append(f"tribal interests {ch['tribal_change']}")
            add_event(db, key, "amended", f"{title}: " + "; ".join(bits), 2 if ch["consultation_removed"] else 0)
            if ch["consultation_removed"]:
                add_event(db, key, "consultation_removed",
                          f"Consultation language removed between {prev_label} and {label}", 2)
        else:
            add_event(db, key, "new_text", f"{title} posted")
        prev_text, prev_label = text, label

    if it["bill_number"]:
        for d in api.bill_versions(sid, it["bill_type"], it["bill_no"]):
            if d["doc_id"] in have:
                continue
            label = version_name(d["version"])
            text = store_doc(cfg, db, api, key, "version", d, label)
            if text:
                record_version(text, label, f"New text: {label}")
        fiscal = db.execute("SELECT version, text FROM docs WHERE key=? AND kind='fiscal' ORDER BY date",
                            (key,)).fetchall()
        for d in api.fiscal_notes(sid, it["bill_type"], it["bill_no"]):
            if d["doc_id"] in have:
                continue
            label = f"Fiscal note {d['version']}"
            text = store_doc(cfg, db, api, key, "fiscal", d, label)
            if fiscal and text:
                ch = compare_fiscal(unpack(fiscal[-1]["text"]), text, fiscal[-1]["version"], label)
                db.execute("INSERT INTO changes (key,kind,from_label,to_label,summary,created) VALUES (?,?,?,?,?,?)",
                           (key, "fiscal", fiscal[-1]["version"], label, json.dumps(ch), now()))
                amt = ", ".join(ch["amounts_added"][:4])
                add_event(db, key, "fiscal", f"Revised {label.lower()}" + (f" (new figures: {amt})" if amt else ""))
            else:
                add_event(db, key, "fiscal", f"{label} posted")
            fiscal = list(fiscal) + [{"version": label, "text": pack(text)}]
        for d in api.amendments(sid, it["bill_type"], it["bill_no"]):
            if d["doc_id"] in have:
                continue
            db.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (d["doc_id"], key, "amendment", None, d["file"], d["date"], d["url"], None, b"", now()))
            add_event(db, key, "amendment", f"Amendment filed: {d['file']}")
    else:
        for d in api.lc_text_docs(sid, it["lc"])[-1:]:
            if d["doc_id"] in have:
                continue
            text = store_doc(cfg, db, api, key, "lc", d, "LC draft")
            if text:
                record_version(text, "LC draft", "Draft text")
    db.execute("UPDATE items SET text_checked=? WHERE key=?", (now(), key))
    return new_text


def legmt_sync(cfg, session_id=None, api=None, quiet=False, text_for="changed", max_text=None):
    """text_for: 'changed' (new/changed items in text-likely statuses), 'introduced' (all bills), or 'none'."""
    api = api or LegMT(cfg)
    db, an = connect(cfg), Analyzer(cfg)
    sid = str(session_id or cfg["legmt"].get("current_session_id", 3))
    first = meta_get(db, f"legmt_synced:{sid}") is None
    likely = {s.lower() for s in cfg.get("text_likely_statuses", [])}
    try:
        meta_set(db, f"label:{sid}", api.session_label(sid))
    except Exception:  # noqa: BLE001 - a label is cosmetic
        pass
    raws = api.all_items(sid)
    print(f"Bill Explorer: {len(raws)} drafts and bills in the {session_label(db, sid)} session")
    todo = []
    for raw in raws:
        n = api.normalize(raw)
        if not n["lc"]:
            continue
        key, what = upsert_item(db, cfg, n)
        it = db.execute("SELECT bill_number, status, text_checked FROM items WHERE key=?", (key,)).fetchone()
        wants = (it["bill_number"] or it["status"].lower() in likely)
        if text_for == "introduced":
            wants = bool(it["bill_number"]) and not it["text_checked"]
        elif text_for == "changed":
            wants = wants and (what or not it["text_checked"])
        else:
            wants = False
        if wants:
            todo.append(key)
        else:
            screen_item(db, an, key)
    db.commit()

    limit = max_text if max_text is not None else cfg["legmt"].get("max_text_per_run", 600)
    if cfg["legmt"].get("fetch_text", True) and todo:
        print(f"Fetching documents for {min(len(todo), limit)} of {len(todo)} items")
        for i, key in enumerate(todo[:limit], 1):
            try:
                fetch_documents(cfg, db, api, an, key)
            except LegMTError:
                raise
            except Exception as e:  # noqa: BLE001 - one bad PDF shouldn't stop the run
                print(f"  ! {key}: {e}")
            screen_item(db, an, key)
            if i % 25 == 0:
                db.commit()
                print(f"  …{i}")
        for key in todo[limit:]:
            screen_item(db, an, key)
        if len(todo) > limit:
            print(f"  {len(todo) - limit} left for the next run")
    db.commit()
    if first or quiet:
        n = db.execute("UPDATE events SET digested='baseline' WHERE digested IS NULL AND item_key LIKE ?",
                       (f"lc:{sid}:%",)).rowcount
        print(f"Baseline load: {n} events recorded without alerts")
    meta_set(db, f"legmt_synced:{sid}", now())
    db.commit()


def fetch_latest_version(cfg, db, api, key):
    """Only the final text of a bill (enrolled if it passed): one PDF instead of the whole history."""
    it = db.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
    vers = api.bill_versions(it["session_id"], it["bill_type"], it["bill_no"])
    if vers:
        store_doc(cfg, db, api, key, "version", vers[-1], version_name(vers[-1]["version"]))
    db.execute("UPDATE items SET text_checked='latest-only' WHERE key=?", (key,))


def backfill(cfg, session_id=None, year=None, api=None, limit=None):
    """Load a past session for retrospective review, in two passes that can resume where they stopped:
    1. the final text of every introduced bill, screened;
    2. for bills with tribal interests, every version, fiscal note, and amendment, so the review
       shows where consultation language was added or removed during the session.
    Text of bills with no tribal signal is not kept, to keep the database small."""
    api = api or LegMT(cfg)
    if year and not session_id:
        session_id = api.session_for_year(year)
        if not session_id:
            sys.exit(f"No Montana legislative session found for {year}. Regular sessions are in odd years.")
    sid = str(session_id)
    legmt_sync(cfg, sid, api=api, quiet=True, text_for="none")
    db, an = connect(cfg), Analyzer(cfg)
    label = session_label(db, sid)
    budget = limit or cfg["legmt"].get("max_backfill_per_run", 1500)

    first = [r["key"] for r in db.execute(
        "SELECT key FROM items WHERE session_id=? AND bill_number IS NOT NULL AND text_checked IS NULL "
        "ORDER BY bill_type, bill_no", (sid,))][:budget]
    print(f"Backfill {label}, pass 1: final text of {len(first)} bills")
    for i, key in enumerate(first, 1):
        try:
            fetch_latest_version(cfg, db, api, key)
        except LegMTError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"  ! {key}: {e}")
        r = screen_item(db, an, key)
        if r and r["tribal"] == "none":
            db.execute("UPDATE docs SET text=? WHERE key=?", (b"", key))
        if i % 50 == 0:
            db.commit()
            print(f"  …{i}/{len(first)}")
    db.commit()
    budget -= len(first)

    flagged = [r["key"] for r in db.execute(
        """SELECT i.key FROM items i JOIN analysis a ON a.key=i.key WHERE i.session_id=?
           AND i.text_checked='latest-only' AND a.tribal != 'none'""", (sid,))][:max(budget, 0)]
    print(f"Backfill {label}, pass 2: full history of {len(flagged)} bills with tribal interests")
    for i, key in enumerate(flagged, 1):
        db.execute("DELETE FROM docs WHERE key=?", (key,))  # rebuild in date order so diffs run forward
        try:
            fetch_documents(cfg, db, api, an, key)
        except LegMTError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"  ! {key}: {e}")
        screen_item(db, an, key)
        if i % 25 == 0:
            db.commit()
            print(f"  …{i}/{len(flagged)}")
    db.execute("UPDATE events SET digested='baseline' WHERE digested IS NULL AND item_key LIKE ?", (f"lc:{sid}:%",))
    db.commit()
    left = db.execute("""SELECT COUNT(*) FROM items i LEFT JOIN analysis a ON a.key=i.key WHERE i.session_id=?
                         AND i.bill_number IS NOT NULL AND (i.text_checked IS NULL OR
                         (i.text_checked='latest-only' AND a.tribal != 'none'))""", (sid,)).fetchone()[0]
    n = db.execute("SELECT COUNT(*) FROM analysis a JOIN items i ON i.key=a.key "
                   "WHERE i.session_id=? AND a.tribal!='none'", (sid,)).fetchone()[0]
    print(f"Backfill {label}: {n} items with tribal interests are in the review queue."
          + (f" {left} bills still to do; run the backfill again to continue." if left else " Complete."))


# --------------------------------------------------------------------------- CSV fallback
def norm_date(v):
    v = (v or "").strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    return v


def ingest_csv(cfg, path, quiet=False):
    db, an = connect(cfg), Analyzer(cfg)
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    new = changed = 0
    for r in rows:
        lc = (r.get("LC #") or "").strip().upper()
        if not lc:
            continue
        m = re.search(r"/bill/(\d+)/", r.get("LC URL") or "")
        bill = (r.get("Bill #") or r.get("Bill") or "").strip() or None
        bm = re.match(r"([A-Z]+)\s*(\d+)", bill or "")
        n = {"session_id": m.group(1) if m else cfg["legmt"].get("current_session_id", 3), "lc": lc,
             "title": (r.get("Short Title") or "").strip(),
             "bill_number": f"{bm.group(1)} {bm.group(2)}" if bm else None,
             "bill_type": bm.group(1) if bm else None, "bill_no": int(bm.group(2)) if bm else None,
             "requester": (r.get("Requestor") or "").strip(),
             "by_request_of": (r.get("By Request Of") or "").strip(),
             "sponsor": (r.get("Primary Sponsor") or "").strip(),
             "status": re.sub(r"^\((LC|H|S)\)\s*", "", (r.get("Status") or "").strip()),
             "status_date": norm_date(r.get("Status Date"))}
        key, what = upsert_item(db, cfg, n, source="csv")
        new += what in ("new", "introduced") and 1 or 0
        changed += what in ("status",) and 1 or 0
        screen_item(db, an, key)
    db.commit()
    if not quiet:
        print(f"{path}: {len(rows)} rows, {new} new, {changed} status changes")


# --------------------------------------------------------------------------- LegiScan (optional hearings)
def legiscan_calendar(cfg):
    key = os.environ.get("LEGISCAN_API_KEY")
    if not key or not cfg.get("legiscan", {}).get("enabled"):
        return
    db = connect(cfg)
    s = requests.Session()

    def call(op, **p):
        r = s.get("https://api.legiscan.com/", params={"key": key, "op": op, **p}, timeout=60)
        r.raise_for_status()
        j = r.json()
        if j.get("status") != "OK":
            raise RuntimeError(j)
        return j

    lcfg = cfg["legiscan"]
    params = {"id": lcfg["session_id"]} if lcfg.get("session_id") else {"state": lcfg.get("state", "MT")}
    master = call("getMasterListRaw", **params)["masterlist"]
    seen = jl(meta_get(db, "legiscan_hashes"), {})
    sid = str(cfg["legmt"].get("current_session_id", 3))
    for v in master.values():
        if not isinstance(v, dict) or "bill_id" not in v or seen.get(str(v["bill_id"])) == v["change_hash"]:
            continue
        b = call("getBill", id=v["bill_id"])["bill"]
        m = re.search(r"LC\s?(\d{3,4})", b.get("state_link") or "", re.I)
        if m:
            k = f"lc:{sid}:LC{int(m.group(1)):04d}"
            cal = [c for c in b.get("calendar") or [] if (c.get("date") or "") >= dt.date.today().isoformat()]
            old = db.execute("SELECT hearings FROM calendar WHERE key=?", (k,)).fetchone()
            if cal and (not old or old["hearings"] != json.dumps(cal)):
                c = cal[0]
                add_event(db, k, "hearing", f"Hearing scheduled: {c.get('date')} {c.get('time', '')} "
                                            f"{c.get('location', '')}".strip(), 1)
            db.execute("INSERT OR REPLACE INTO calendar VALUES (?,?,?)", (k, json.dumps(cal), now()))
        seen[str(v["bill_id"])] = v["change_hash"]
    meta_set(db, "legiscan_hashes", json.dumps(seen))
    db.commit()


# --------------------------------------------------------------------------- AIGPI reviews
def pull_reviews(cfg):
    rc = cfg.get("review") or {}
    if not rc.get("supabase_url") or not rc.get("supabase_anon_key"):
        return
    db = connect(cfg)
    key = rc["supabase_anon_key"]
    hdr = {"apikey": key}
    if not key.startswith("sb_"):  # legacy anon keys are JWTs and also go in Authorization
        hdr["Authorization"] = f"Bearer {key}"
    try:
        r = requests.get(rc["supabase_url"].rstrip("/") + "/rest/v1/reviews?select=*", headers=hdr, timeout=60)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"Could not read reviews: {e}")
        return
    n = 0
    for rv in r.json():
        old = db.execute("SELECT * FROM reviews WHERE key=?", (rv["item_key"],)).fetchone()
        if old and old["updated_at"] == rv.get("updated_at"):
            continue
        db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?,?,?,?)",
                   (rv["item_key"], rv.get("tribal_interest"), rv.get("consultation"),
                    int(bool(rv.get("aigpi_tracking"))), rv.get("public_note"), rv.get("reviewer_email"),
                    rv.get("reviewed_version"), rv.get("updated_at")))
        if rv.get("tribal_interest") == "confirmed" and (not old or old["tribal_interest"] != "confirmed"):
            c = {"no": "no consultation provision", "partial": "partial consultation provision",
                 "yes": "has a consultation provision"}.get(rv.get("consultation"), "consultation not yet assessed")
            add_event(db, rv["item_key"], "aigpi_confirmed", f"AIGPI confirmed tribal interests; {c}",
                      2 if rv.get("consultation") in ("no", "partial") else 1)
        n += 1
    db.commit()
    print(f"Reviews: {n} new or updated")


# --------------------------------------------------------------------------- describing items
def item_record(db, key, full=True):
    it = db.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
    a = db.execute("SELECT * FROM analysis WHERE key=?", (key,)).fetchone()
    rv = db.execute("SELECT * FROM reviews WHERE key=?", (key,)).fetchone()
    rec = {
        "key": key, "session": it["session_id"], "lc": it["lc"], "bill": it["bill_number"],
        "id": f"{it['bill_number']} ({it['lc']})" if it["bill_number"] else it["lc"],
        "title": it["title"], "requester": it["requester"], "by_request_of": it["by_request_of"],
        "sponsor": it["sponsor"], "subjects": jl(it["subjects"]), "status": it["status"],
        "status_date": it["status_date"], "link": it["url"],
        "tags": jl(a["tags"]) if a else [], "tribal": a["tribal"] if a else "none",
        "consultation": a["consultation"] if a else "unknown", "gap": bool(a and a["gap"]),
        "basis": a["basis"] if a else "title only", "version": a["text_version"] if a else None,
        "review": dict(rv) if rv else None,
    }
    docs = db.execute("SELECT kind, version, file, date, url FROM docs WHERE key=? ORDER BY date", (key,)).fetchall()
    txt = [d for d in docs if d["kind"] in ("version", "lc")]
    fis = [d for d in docs if d["kind"] == "fiscal"]
    rec["text_url"] = txt[-1]["url"] if txt else None
    rec["fiscal_url"] = fis[-1]["url"] if fis else None
    evs = db.execute("SELECT ts, detail, priority FROM events WHERE item_key=? ORDER BY id DESC LIMIT 8",
                     (key,)).fetchall()
    rec["updated"] = evs[0]["ts"] if evs else it["last_changed"]
    if not full:
        return rec
    rec.update({
        "signals": jl(a["signals"]) if a else [], "score": a["tribal_score"] if a else 0,
        "evidence": jl(a["consultation_evidence"]) if a else [], "mca": (jl(a["mca"]) if a else [])[:15],
        "versions": [{"label": d["version"], "date": d["date"][:10], "url": d["url"]} for d in txt],
        "fiscal": [{"label": d["version"], "date": d["date"][:10], "url": d["url"]} for d in fis],
        "amendments": [{"file": d["file"], "date": d["date"][:10], "url": d["url"]}
                       for d in docs if d["kind"] == "amendment"][-6:],
        "changes": [dict(jl(c["summary"], {}), created=c["created"]) for c in db.execute(
            "SELECT summary, created FROM changes WHERE key=? ORDER BY id DESC LIMIT 3", (key,))],
        "history": jl(it["history"])[-6:],
        "events": [{"ts": e["ts"], "detail": e["detail"], "priority": e["priority"] or 0} for e in evs],
        "hearings": jl((db.execute("SELECT hearings FROM calendar WHERE key=?", (key,)).fetchone() or {"hearings": "[]"})["hearings"]),
    })
    return rec


# --------------------------------------------------------------------------- feed for the app
def screen_missing(cfg, db):
    """Score any item that has never been screened (e.g. carried over from version 1)."""
    missing = [r["key"] for r in db.execute(
        "SELECT i.key FROM items i LEFT JOIN analysis a ON a.key=i.key WHERE a.key IS NULL")]
    if missing:
        an = Analyzer(cfg)
        for k in missing:
            screen_item(db, an, k)
        db.execute("UPDATE events SET digested='baseline' WHERE digested IS NULL AND kind='flagged' "
                   "AND item_key IN (%s)" % ",".join("?" * len(missing)), missing) if len(missing) < 900 else \
            db.execute("UPDATE events SET digested='baseline' WHERE digested IS NULL AND kind='flagged'")
        db.commit()
        print(f"Screened {len(missing)} items that had no score yet")


def export_feed(cfg):
    db = connect(cfg)
    screen_missing(cfg, db)
    site = cfg["_site"]
    rc = cfg.get("review") or {}
    (site / "config.js").write_text(
        "window.BILLWATCH = " + json.dumps({
            "supabaseUrl": rc.get("supabase_url"), "supabaseAnonKey": rc.get("supabase_anon_key"),
            "currentSession": str(cfg["legmt"].get("current_session_id", 3))}) + ";\n", encoding="utf-8")
    sessions = []
    sids = [r["session_id"] for r in db.execute("SELECT DISTINCT session_id FROM items")]
    years = {sid: session_label(db, sid) for sid in sids}
    for sid in sorted(sids, key=lambda x: years[x], reverse=True):
        keys = db.execute("""SELECT i.key, a.tribal, a.tags FROM items i LEFT JOIN analysis a ON a.key=i.key
                             LEFT JOIN reviews r ON r.key=i.key WHERE i.session_id=? AND
                             (a.tribal != 'none' OR (a.tags IS NOT NULL AND a.tags != '[]') OR r.key IS NOT NULL)""",
                          (sid,)).fetchall()
        items = [item_record(db, k["key"], full=(k["tribal"] != "none")) for k in keys]
        items.sort(key=lambda x: x["updated"] or "", reverse=True)
        total = db.execute("SELECT COUNT(*) FROM items WHERE session_id=?", (sid,)).fetchone()[0]
        fname = f"feed-{years[sid].replace(' ', '-')}.json"
        feed = {"generated": now(), "session": sid, "label": years[sid],
                "total_tracked": total, "count": len(items), "items": items}
        (site / fname).write_text(json.dumps(feed, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        sessions.append({"id": sid, "label": years[sid], "file": fname, "count": len(items),
                         "flagged": sum(1 for i in items if i["tribal"] != "none")})
        print(f"Feed: {fname} ({len(items)} of {total} items)")
    cur = str(cfg["legmt"].get("current_session_id", 3))
    (site / "sessions.json").write_text(json.dumps({"generated": now(), "current": cur, "sessions": sessions}),
                                        encoding="utf-8")
    legacy = site / "feed.json"
    if legacy.exists():
        legacy.unlink()


# --------------------------------------------------------------------------- digest + alerts
GROUPS = [
    ("removed", "Consultation language removed by amendment"),
    ("confirmed", "Confirmed by AIGPI"),
    ("gap", "Tribal interests likely, no consultation provision"),
    ("likely", "Tribal interests likely"),
    ("possible", "Possible tribal interests"),
]


def collect_digest(db):
    rows = db.execute("""SELECT e.item_key, e.detail, e.kind, e.priority FROM events e
                         LEFT JOIN analysis a ON a.key=e.item_key LEFT JOIN reviews r ON r.key=e.item_key
                         WHERE e.digested IS NULL AND (a.tribal IN ('likely','possible')
                         OR r.tribal_interest='confirmed') ORDER BY e.id""").fetchall()
    items = {}
    for r in rows:
        it = items.setdefault(r["item_key"], {"key": r["item_key"], "events": [], "kinds": set(), "priority": 0})
        it["events"].append(r["detail"])
        it["kinds"].add(r["kind"])
        it["priority"] = max(it["priority"], r["priority"] or 0)
    out = []
    for it in items.values():
        rec = item_record(db, it["key"])
        rv = rec["review"] or {}
        group = ("removed" if "consultation_removed" in it["kinds"] else
                 "confirmed" if rv.get("tribal_interest") == "confirmed" else
                 "gap" if rec["gap"] and rec["tribal"] == "likely" else rec["tribal"])
        out.append(dict(it, rec=rec, group=group))
    order = [g for g, _ in GROUPS]
    out.sort(key=lambda x: (order.index(x["group"]) if x["group"] in order else 9, -x["priority"]))
    return out


CONSULT_TEXT = {"present": "Consultation provision found", "notice only": "Notice to tribes only, no consultation",
                "absent": "No consultation provision found", "unknown": "No text yet"}


def render(items):
    today = dt.date.today().isoformat()
    h = [f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>Bill Watch {today}</title>",
         "<style>body{font-family:Arial,sans-serif;color:#1a1a1a;background:#fff;max-width:760px;margin:auto;"
         "line-height:1.5;padding:1rem}a{color:#0645ad}h3{margin-bottom:.2rem}"
         "blockquote{border-left:4px solid #555;margin:.5rem 0;padding-left:.75rem}</style></head><body>",
         f"<h1>Montana Bill Watch: {today}</h1>",
         f"<p>{len(items)} item(s) with new activity. Screening flags are leads for AIGPI review, "
         "not conclusions.</p>"]
    t = [f"MONTANA BILL WATCH: {today}", f"{len(items)} item(s). Screening flags need AIGPI review.", ""]
    for g, head in GROUPS:
        group = [i for i in items if i["group"] == g]
        if not group:
            continue
        h.append(f"<h2>{html.escape(head)} ({len(group)})</h2>")
        t += ["=" * 70, head.upper(), "=" * 70]
        for it in group:
            d = it["rec"]
            sig = "; ".join(f"{s['type']}: {', '.join(s['detail'][:4])}" for s in d["signals"])
            who = d["requester"] + (f" (by request of {d['by_request_of']})"
                                    if d["by_request_of"] and d["by_request_of"] != d["requester"] else "")
            h.append(f"<h3>{html.escape(d['id'])}: {html.escape(d['title'] or '')}</h3><ul>"
                     f"<li>Requested by: {html.escape(who or '—')}</li>"
                     + (f"<li>Sponsor: {html.escape(d['sponsor'])}</li>" if d["sponsor"] else "") +
                     f"<li>Status: {html.escape(d['status'] or '—')}</li>"
                     f"<li>What's new: {html.escape('; '.join(it['events']))}</li>"
                     f"<li>Consultation: {html.escape(CONSULT_TEXT.get(d['consultation'], d['consultation']))}</li>"
                     f"<li>Pathways: {html.escape(', '.join(d['tags']) or '—')}</li>"
                     f"<li>Why flagged ({html.escape(d['basis'])}): {html.escape(sig)}</li>"
                     f"<li><a href='{html.escape(d['link'])}'>Open {html.escape(d['lc'])} in Bill Explorer</a></li>"
                     + (f"<li><a href='{html.escape(d['text_url'])}'>Read the latest text (PDF)</a></li>" if d["text_url"] else "")
                     + "</ul>")
            for e in d["evidence"][:2]:
                h.append(f"<blockquote>{html.escape(e['text'])}</blockquote>")
            t += [f"\n{d['id']}: {d['title']}", f"  Requested by: {who or '—'}", f"  Status: {d['status']}",
                  f"  New: {'; '.join(it['events'])}",
                  f"  Consultation: {CONSULT_TEXT.get(d['consultation'], d['consultation'])}",
                  f"  Pathways: {', '.join(d['tags']) or '—'}", f"  Why: {sig}", f"  Link: {d['link']}"]
    h.append("</body></html>")
    return "\n".join(h), "\n".join(t)


def send_email(cfg, subject, html_body, text_body):
    e = cfg["email"]
    pw = os.environ.get("BILLWATCH_SMTP_PASSWORD")
    if not pw:
        print("BILLWATCH_SMTP_PASSWORD not set; skipping email.")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, e["from_addr"], ", ".join(e["to_addrs"])
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(e["smtp_host"], e["smtp_port"], timeout=60) as s:
        s.starttls()
        s.login(e["username"], pw)
        s.send_message(msg)
    print(f"Emailed digest to {len(e['to_addrs'])} recipient(s)")
    return True


def send_push(cfg, items):
    pc = cfg["push"]
    topic = os.environ.get("BILLWATCH_NTFY_TOPIC") or pc.get("topic")
    if not topic:
        print("No ntfy topic set; skipping push.")
        return False
    urgent = [i for i in items if i["group"] in ("removed", "confirmed", "gap") or i["priority"] >= 2]
    lines = [f"{i['rec']['id']}: {i['rec']['title']}" for i in (urgent + [i for i in items if i not in urgent])[:6]]
    if len(items) > 6:
        lines.append(f"…and {len(items) - 6} more")
    title = f"{len(items)} bill update(s)" + (f", {len(urgent)} need attention" if urgent else "")
    headers = {"Title": title, "Priority": "high" if urgent else "default", "Tags": "scroll"}
    if pc.get("app_url"):
        headers["Click"] = pc["app_url"]
    if os.environ.get("BILLWATCH_NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['BILLWATCH_NTFY_TOKEN']}"
    try:
        r = requests.post(f"{pc.get('server', 'https://ntfy.sh').rstrip('/')}/{topic}",
                          data="\n".join(lines).encode("utf-8"), headers=headers, timeout=30)
        r.raise_for_status()
        print("Push sent")
        return True
    except requests.RequestException as e:
        print(f"Push failed: {e}")
        return False


def digest(cfg, send=False):
    db = connect(cfg)
    items = collect_digest(db)
    if not items:
        db.execute("UPDATE events SET digested=? WHERE digested IS NULL", (now(),))
        db.commit()
        print("No new activity on items with tribal interests.")
        return None
    html_body, text_body = render(items)
    out = cfg["_data"] / "digests" / f"digest_{dt.datetime.now():%Y-%m-%d_%H%M}.html"
    out.write_text(html_body, encoding="utf-8")
    out.with_suffix(".txt").write_text(text_body, encoding="utf-8")
    print(f"Digest: {out} ({len(items)} items)")
    if send and cfg.get("push", {}).get("enabled"):
        send_push(cfg, items)
    if send and cfg.get("email", {}).get("enabled"):
        subj = f"{cfg['email'].get('subject_prefix', '')} {len(items)} update(s)".strip()
        if not send_email(cfg, subj, html_body, text_body):
            return out
    db.execute("UPDATE events SET digested=? WHERE digested IS NULL", (now(),))
    db.commit()
    return out


def report(cfg, out):
    db = connect(cfg)
    keys = [r["key"] for r in db.execute("""SELECT i.key FROM items i JOIN analysis a ON a.key=i.key
                                            WHERE a.tribal != 'none' ORDER BY i.session_id DESC, a.tribal_score DESC""")]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session", "id", "title", "tribal_interests", "consultation", "gap", "pathways",
                    "signals", "requester", "by_request_of", "sponsor", "status", "aigpi_review",
                    "aigpi_consultation", "link", "latest_text"])
        for k in keys:
            d = item_record(db, k)
            rv = d["review"] or {}
            w.writerow([session_label(db, d["session"]), d["id"], d["title"], d["tribal"],
                        d["consultation"], "yes" if d["gap"] else "", "; ".join(d["tags"]),
                        "; ".join(f"{s['type']}: {', '.join(s['detail'])}" for s in d["signals"]),
                        d["requester"], d["by_request_of"], d["sponsor"], d["status"],
                        rv.get("tribal_interest") or "unreviewed", rv.get("consultation") or "",
                        d["link"], d["text_url"] or ""])
    print(f"Wrote {len(keys)} items to {out}")


# --------------------------------------------------------------------------- local web interface
def serve(cfg, port=8000, open_browser=True):
    import functools
    import http.server
    import webbrowser

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0].endswith("sessions.json"):
                export_feed(cfg)
            super().do_GET()

        def log_message(self, *args):
            pass

    export_feed(cfg)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                            functools.partial(Handler, directory=str(cfg["_site"])))
    url = f"http://localhost:{port}/"
    print(f"Bill Watch is running at {url}  (press Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


# --------------------------------------------------------------------------- run everything
def run(cfg, send=False):
    inbox = cfg["_data"] / "inbox"
    for p in sorted(inbox.glob("*.csv"), key=lambda x: x.stat().st_mtime):
        ingest_csv(cfg, p)
        shutil.move(str(p), inbox / "processed" / f"{dt.datetime.now():%Y%m%d_%H%M%S}_{p.name}")
    if cfg.get("legmt", {}).get("enabled", True):
        try:
            legmt_sync(cfg)
        except LegMTError as e:
            print(f"Bill Explorer: {e}")
    try:
        legiscan_calendar(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"LegiScan: {e}")
    pull_reviews(cfg)
    digest(cfg, send)
    export_feed(cfg)


def main():
    ap = argparse.ArgumentParser(description="Montana Bill Watch")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("sync"); p.add_argument("--session"); p.add_argument("--max-text", type=int)
    p = sub.add_parser("backfill"); p.add_argument("--year", help="e.g. 2025, 2023, 2021 ... back to 1999")
    p.add_argument("--session", help="session id instead of a year (e.g. 20172 for the 2017 special)")
    p.add_argument("--limit", type=int, help="most bills to fetch this run (default 1500)")
    p = sub.add_parser("ingest-csv"); p.add_argument("files", nargs="+")
    p.add_argument("--no-alerts", action="store_true")
    sub.add_parser("reviews")
    p = sub.add_parser("screen"); p.add_argument("--session")
    p = sub.add_parser("digest"); p.add_argument("--send", action="store_true")
    sub.add_parser("feed")
    p = sub.add_parser("report"); p.add_argument("--out", default="flagged_items.csv")
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8000)
    p = sub.add_parser("run"); p.add_argument("--send", action="store_true")
    a = ap.parse_args()
    cfg = load_config(a.config)
    try:
        if a.cmd == "init":
            connect(cfg)
            print(f"Ready. Data folder: {cfg['_data']}")
        elif a.cmd == "sync":
            legmt_sync(cfg, a.session, max_text=a.max_text)
        elif a.cmd == "backfill":
            if not (a.year or a.session):
                sys.exit("Say which session: backfill --year 2023")
            backfill(cfg, a.session, year=a.year, limit=a.limit)
        elif a.cmd == "ingest-csv":
            for f in a.files:
                ingest_csv(cfg, f)
            if a.no_alerts:
                db = connect(cfg)
                db.execute("UPDATE events SET digested='baseline' WHERE digested IS NULL")
                db.commit()
                print("Loaded as a baseline; these won't appear in the next digest.")
        elif a.cmd == "reviews":
            pull_reviews(cfg)
        elif a.cmd == "screen":
            rescreen(cfg, a.session)
        elif a.cmd == "digest":
            digest(cfg, a.send)
        elif a.cmd == "feed":
            export_feed(cfg)
        elif a.cmd == "report":
            report(cfg, a.out)
        elif a.cmd == "serve":
            serve(cfg, a.port)
        elif a.cmd == "run":
            run(cfg, a.send)
    except LegMTError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
