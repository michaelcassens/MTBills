#!/usr/bin/env python3
"""
Montana Bill Watch: track LC drafts and introduced bills, screen them for
possible effects on tribal lands and communities, and produce alert digests.

Sources
  1. Bill Explorer CSV exports (LC drafts) -> `ingest-csv` diffs each export
     against what's already stored and records new requests / status changes.
  2. LegiScan API (introduced bills + full text) -> `legiscan-sync` pulls only
     bills whose change_hash moved, downloads new text versions, extracts text.

Usage
  python billwatch.py init
  python billwatch.py ingest-csv path/to/Filtered_Bills.csv
  python billwatch.py legiscan-sessions
  python billwatch.py legiscan-sync
  python billwatch.py screen            # re-score everything (after editing terms)
  python billwatch.py digest [--send]   # alerts not yet included in a digest
  python billwatch.py report            # all flagged items -> CSV
  python billwatch.py feed              # write docs/feed.json for the mobile app
  python billwatch.py serve             # open the web interface at http://localhost:8000
  python billwatch.py run [--send]      # inbox CSVs + LegiScan + digest (for a scheduler)
"""
import argparse
import base64
import csv
import datetime as dt
import html
import io
import json
import os
import re
import shutil
import smtplib
import sqlite3
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
LEGISCAN_URL = "https://api.legiscan.com/"
LC_RE = re.compile(r"\bLC\s?(\d{3,4})\b", re.I)
LEGISCAN_STATUS = {1: "Introduced", 2: "Engrossed", 3: "Enrolled", 4: "Passed",
                   5: "Vetoed", 6: "Failed"}


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
def now():
    """UTC timestamp, so the app shows correct local times wherever the job runs."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    data = Path(cfg.get("data_dir", "data"))
    if not data.is_absolute():
        data = Path(path).resolve().parent / data
    cfg["_data"] = data
    for sub in ("texts", "digests", "inbox", "inbox/processed"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    return cfg


SCHEMA = """
CREATE TABLE IF NOT EXISTS lc (
  lc TEXT, session TEXT, title TEXT, requestor TEXT, by_request_of TEXT,
  status TEXT, status_date TEXT, url TEXT, first_seen TEXT, last_changed TEXT,
  PRIMARY KEY (lc, session));
CREATE TABLE IF NOT EXISTS bills (
  bill_id INTEGER PRIMARY KEY, number TEXT, lc TEXT, title TEXT, description TEXT,
  status TEXT, last_action TEXT, last_action_date TEXT, sponsors TEXT,
  state_link TEXT, url TEXT, change_hash TEXT, calendar TEXT,
  first_seen TEXT, last_changed TEXT);
CREATE TABLE IF NOT EXISTS bill_texts (
  doc_id INTEGER PRIMARY KEY, bill_id INTEGER, date TEXT, type TEXT,
  path TEXT, text TEXT, fetched TEXT);
CREATE TABLE IF NOT EXISTS flags (
  item_key TEXT PRIMARY KEY, score INTEGER, tier TEXT, matches TEXT,
  snippets TEXT, basis TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, item_key TEXT, kind TEXT,
  detail TEXT, digested TEXT);
"""


def connect(cfg):
    db = sqlite3.connect(cfg["_data"] / "billwatch.db")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(bill_texts)")}
    if "url" not in cols:
        db.execute("ALTER TABLE bill_texts ADD COLUMN url TEXT")
    return db


def add_event(db, item_key, kind, detail):
    db.execute("INSERT INTO events (ts,item_key,kind,detail) VALUES (?,?,?,?)",
               (now(), item_key, kind, detail))


# --------------------------------------------------------------------------
# Screening
# --------------------------------------------------------------------------
class Screener:
    def __init__(self, cfg):
        sc = cfg["screening"]
        self.min_score = sc.get("min_score_for_digest", 2)
        self.rules = []  # (tier, weight, label, compiled)
        for tier, spec in sc["tiers"].items():
            for term in spec["terms"]:
                rx = re.compile(r"\b(?:%s)\b" % term, re.I)
                self.rules.append((tier, spec["weight"], term, rx))

    def score(self, text):
        text = re.sub(r"\s+", " ", text or "")
        matches, snippets, total, tiers, last_end = [], [], 0, set(), -1
        for tier, weight, term, rx in self.rules:
            m = rx.search(text)
            if not m:
                continue
            matches.append({"tier": tier, "term": m.group(0)})
            total += weight
            tiers.add(tier)
            if len(snippets) < 4 and len(text) > 200 and m.start() > last_end:  # full text only
                a, b = max(0, m.start() - 110), min(len(text), m.end() + 110)
                last_end = b
                snippets.append(("…" if a else "") + text[a:b] + ("…" if b < len(text) else ""))
        tier = next((t for t in ("direct", "geographic", "indirect") if t in tiers), None)
        return total, tier, matches, snippets


def screen_item(db, scr, item_key, title, full_text=None):
    basis = "full text" if full_text else "title only"
    score, tier, matches, snippets = scr.score(" ".join(filter(None, [title, full_text])))
    old = db.execute("SELECT score, tier FROM flags WHERE item_key=?", (item_key,)).fetchone()
    db.execute("""INSERT INTO flags VALUES (?,?,?,?,?,?,?)
                  ON CONFLICT(item_key) DO UPDATE SET score=excluded.score,
                  tier=excluded.tier, matches=excluded.matches,
                  snippets=excluded.snippets, basis=excluded.basis, updated=excluded.updated""",
               (item_key, score, tier, json.dumps(matches), json.dumps(snippets), basis, now()))
    rank = {"direct": 3, "geographic": 2, "indirect": 1, None: 0}
    if score >= scr.min_score:
        if old is None or old["score"] < scr.min_score:
            add_event(db, item_key, "flagged", f"Matched {tier} terms ({basis})")
        elif rank[tier] > rank[old["tier"]]:
            add_event(db, item_key, "flagged", f"Now matches {tier} terms ({basis})")
    return score


# --------------------------------------------------------------------------
# Source 1: Bill Explorer CSV exports (LC drafts)
# --------------------------------------------------------------------------
def norm_date(v):
    v = (v or "").strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    if v.replace(".", "").isdigit():  # Excel serial date
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(float(v)))).isoformat()
    return v


def clean_status(s):
    return re.sub(r"^\(LC\)\s*", "", (s or "").strip())


def ingest_csv(cfg, path, quiet=False):
    db, scr = connect(cfg), Screener(cfg)
    likely = {s.lower() for s in cfg.get("text_likely_statuses", [])}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    new = changed = 0
    for r in rows:
        lc = (r.get("LC #") or "").strip().upper()
        if not lc:
            continue
        url = (r.get("LC URL") or "").strip()
        m = re.search(r"/bill/(\d+)/", url)
        session = m.group(1) if m else "unknown"
        title = (r.get("Short Title") or "").strip()
        status = clean_status(r.get("Status"))
        key = f"lc:{session}:{lc}"
        cur = db.execute("SELECT status FROM lc WHERE lc=? AND session=?", (lc, session)).fetchone()
        vals = (title, (r.get("Requestor") or "").strip(), (r.get("By Request Of") or "").strip(),
                status, norm_date(r.get("Status Date")), url)
        if cur is None:
            db.execute("INSERT INTO lc VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (lc, session) + vals + (now(), now()))
            add_event(db, key, "new_request", f"New draft request: {title}")
            new += 1
        elif cur["status"] != status:
            db.execute("""UPDATE lc SET title=?,requestor=?,by_request_of=?,status=?,
                          status_date=?,url=?,last_changed=? WHERE lc=? AND session=?""",
                       vals + (now(), lc, session))
            note = " (text may now be viewable)" if status.lower() in likely else ""
            add_event(db, key, "status_change", f"{cur['status']} → {status}{note}")
            changed += 1
        else:
            continue
        screen_item(db, scr, key, title, linked_bill_text(db, lc))
    db.commit()
    if not quiet:
        print(f"{path}: {len(rows)} rows, {new} new, {changed} status changes")
    return new, changed


def linked_bill_text(db, lc):
    """If LegiScan has the introduced bill for this LC, screen on its text too."""
    row = db.execute("""SELECT group_concat(t.text, ' ') AS txt FROM bills b
                        JOIN bill_texts t ON t.bill_id=b.bill_id WHERE b.lc=?""", (lc,)).fetchone()
    return row["txt"] if row and row["txt"] else None


# --------------------------------------------------------------------------
# Source 2: LegiScan (introduced bills + text)
# --------------------------------------------------------------------------
class LegiScan:
    def __init__(self, cfg):
        self.key = os.environ.get("LEGISCAN_API_KEY")
        if not self.key:
            sys.exit("Set the LEGISCAN_API_KEY environment variable first.")
        self.delay = cfg["legiscan"].get("request_delay", 0.3)
        self.s = requests.Session()

    def get(self, op, **params):
        time.sleep(self.delay)
        r = self.s.get(LEGISCAN_URL, params={"key": self.key, "op": op, **params}, timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "OK":
            raise RuntimeError(f"LegiScan {op} error: {data.get('alert', data)}")
        return data


def legiscan_sessions(cfg):
    ls = LegiScan(cfg)
    for s in ls.get("getSessionList", state=cfg["legiscan"]["state"])["sessions"]:
        print(f"{s['session_id']:>6}  {s['session_name']}")


def pdf_to_text(raw):
    from pypdf import PdfReader
    try:
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
    except Exception as e:  # noqa: BLE001
        print(f"  ! could not extract text: {e}")
        return ""


def fetch_doc(ls, doc):
    """Try the state's own download link first (saves API quota), then LegiScan."""
    link = doc.get("state_link")
    if link:
        try:
            r = ls.s.get(link, timeout=60)
            if r.ok and r.content[:4] == b"%PDF":
                return r.content
        except requests.RequestException:
            pass
    data = ls.get("getBillText", id=doc["doc_id"])["text"]
    return base64.b64decode(data["doc"])


def legiscan_sync(cfg, ls=None):
    ls = ls or LegiScan(cfg)
    db, scr = connect(cfg), Screener(cfg)
    lcfg = cfg["legiscan"]
    params = {"state": lcfg["state"]}
    if lcfg.get("session_id"):
        params = {"id": lcfg["session_id"]}
    master = ls.get("getMasterListRaw", **params)["masterlist"]
    items = [v for v in master.values() if isinstance(v, dict) and "bill_id" in v]
    known = {r["bill_id"]: r["change_hash"] for r in db.execute("SELECT bill_id, change_hash FROM bills")}
    todo = [i for i in items if known.get(i["bill_id"]) != i["change_hash"]]
    print(f"LegiScan: {len(items)} bills in session, {len(todo)} new or changed")

    for n, item in enumerate(todo, 1):
        b = ls.get("getBill", id=item["bill_id"])["bill"]
        m = LC_RE.search(b.get("state_link") or "")
        lc = f"LC{int(m.group(1)):04d}" if m else None
        hist = b.get("history") or []
        last = hist[-1] if hist else {}
        status = LEGISCAN_STATUS.get(b.get("status"), str(b.get("status")))
        cal = [c for c in (b.get("calendar") or []) if c.get("date", "") >= dt.date.today().isoformat()]
        row = (b["bill_id"], b.get("bill_number"), lc, b.get("title"), b.get("description"), status,
               last.get("action"), last.get("date"),
               ", ".join(s.get("name", "") for s in b.get("sponsors") or []),
               b.get("state_link"), b.get("url"), item["change_hash"], json.dumps(cal))
        key = f"bill:{b['bill_id']}"
        prev = db.execute("SELECT last_action, calendar FROM bills WHERE bill_id=?", (b["bill_id"],)).fetchone()
        db.execute("""INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(bill_id) DO UPDATE SET number=excluded.number, lc=excluded.lc,
                      title=excluded.title, description=excluded.description, status=excluded.status,
                      last_action=excluded.last_action, last_action_date=excluded.last_action_date,
                      sponsors=excluded.sponsors, state_link=excluded.state_link, url=excluded.url,
                      change_hash=excluded.change_hash, calendar=excluded.calendar,
                      last_changed=excluded.last_changed""", row + (now(), now()))
        if prev is None:
            add_event(db, key, "introduced", f"{b.get('bill_number')} introduced: {b.get('title')}")
        else:
            if last.get("action") and last.get("action") != prev["last_action"]:
                add_event(db, key, "action", f"{last.get('date')}: {last.get('action')}")
            if json.dumps(cal) != prev["calendar"] and cal:
                c = cal[0]
                add_event(db, key, "hearing",
                          f"Scheduled {c.get('type','')}: {c.get('date')} {c.get('time','')} "
                          f"{c.get('location','')}".strip())

        if lcfg.get("fetch_text", True):
            have = {r["doc_id"] for r in db.execute("SELECT doc_id FROM bill_texts WHERE bill_id=?", (b["bill_id"],))}
            for doc in b.get("texts") or []:
                if doc["doc_id"] in have:
                    continue
                try:
                    raw = fetch_doc(ls, doc)
                except Exception as e:  # noqa: BLE001
                    print(f"  ! {b.get('bill_number')} doc {doc['doc_id']}: {e}")
                    continue
                fname = f"{b.get('bill_number')}_{doc.get('type','text')}_{doc['doc_id']}.pdf".replace(" ", "")
                p = cfg["_data"] / "texts" / fname
                p.write_bytes(raw)
                db.execute("""INSERT OR REPLACE INTO bill_texts
                              (doc_id,bill_id,date,type,path,text,fetched,url) VALUES (?,?,?,?,?,?,?,?)""",
                           (doc["doc_id"], b["bill_id"], doc.get("date"), doc.get("type"),
                            str(p), pdf_to_text(raw), now(), doc.get("state_link") or doc.get("url")))
                if prev is not None:
                    add_event(db, key, "new_text", f"New text version: {doc.get('type')} ({doc.get('date')})")

        full = linked_bill_text(db, lc) if lc else None
        if not full:
            t = db.execute("SELECT group_concat(text,' ') AS t FROM bill_texts WHERE bill_id=?", (b["bill_id"],)).fetchone()
            full = t["t"] if t else None
        screen_item(db, scr, key, f"{b.get('title')} {b.get('description') or ''}", full)
        if lc:  # re-screen the matching LC draft with the bill text
            for s in db.execute("SELECT session, title FROM lc WHERE lc=?", (lc,)).fetchall():
                screen_item(db, scr, f"lc:{s['session']}:{lc}", s["title"], full)
        db.commit()
        if n % 25 == 0:
            print(f"  …{n}/{len(todo)}")
    db.commit()


def rescreen(cfg):
    db, scr = connect(cfg), Screener(cfg)
    n = 0
    for r in db.execute("SELECT lc, session, title FROM lc").fetchall():
        screen_item(db, scr, f"lc:{r['session']}:{r['lc']}", r["title"], linked_bill_text(db, r["lc"]))
        n += 1
    for r in db.execute("SELECT bill_id, title, description FROM bills").fetchall():
        t = db.execute("SELECT group_concat(text,' ') AS t FROM bill_texts WHERE bill_id=?", (r["bill_id"],)).fetchone()
        screen_item(db, scr, f"bill:{r['bill_id']}", f"{r['title']} {r['description'] or ''}", t["t"])
        n += 1
    db.commit()
    print(f"Re-screened {n} items")


# --------------------------------------------------------------------------
# Output: digest + report
# --------------------------------------------------------------------------
def describe(db, key):
    if key.startswith("lc:"):
        _, session, lc = key.split(":")
        r = db.execute("SELECT * FROM lc WHERE lc=? AND session=?", (lc, session)).fetchone()
        b = db.execute("SELECT number FROM bills WHERE lc=?", (lc,)).fetchone()
        return {"id": lc + (f" / {b['number']}" if b else ""), "title": r["title"],
                "who": r["requestor"], "status": r["status"], "link": r["url"],
                "link_text": f"Open {lc} in Bill Explorer", "pdfs": []}
    r = db.execute("SELECT * FROM bills WHERE bill_id=?", (int(key.split(":")[1]),)).fetchone()
    texts = db.execute("SELECT path, url, type FROM bill_texts WHERE bill_id=? ORDER BY date", (r["bill_id"],)).fetchall()
    pdfs = [t["path"] for t in texts]
    return {"id": r["number"] + (f" ({r['lc']})" if r["lc"] else ""), "title": r["title"],
            "who": r["sponsors"], "status": f"{r['status']}: {r['last_action'] or ''}",
            "link": r["state_link"] or r["url"], "link_text": f"Open {r['number']} in Bill Explorer",
            "pdfs": pdfs, "calendar": json.loads(r["calendar"] or "[]"),
            "text_url": next((t["url"] for t in reversed(texts) if t["url"]), None),
            "text_type": texts[-1]["type"] if texts else None}


def collect_digest(db, min_score):
    rows = db.execute("""SELECT e.*, f.score, f.tier, f.matches, f.snippets, f.basis
                         FROM events e JOIN flags f ON f.item_key=e.item_key
                         WHERE e.digested IS NULL AND f.score >= ?
                         ORDER BY f.score DESC, e.ts""", (min_score,)).fetchall()
    items = {}
    for r in rows:
        it = items.setdefault(r["item_key"], {"key": r["item_key"], "score": r["score"], "tier": r["tier"],
                                              "matches": json.loads(r["matches"]),
                                              "snippets": json.loads(r["snippets"]),
                                              "basis": r["basis"], "events": []})
        it["events"].append(r["detail"])
    # Fold an LC draft into its introduced bill when both have news
    for key in [k for k in items if k.startswith("lc:")]:
        lc = key.split(":")[2]
        b = db.execute("SELECT bill_id FROM bills WHERE lc=?", (lc,)).fetchone()
        if b and f"bill:{b['bill_id']}" in items:
            items[f"bill:{b['bill_id']}"]["events"] += [f"({lc}) {e}" for e in items.pop(key)["events"]]
    return list(items.values())


TIER_HEAD = {"direct": "Direct references to tribes, tribal lands, or Indian law",
             "geographic": "Mentions reservations, tribal nations, or nearby counties",
             "indirect": "Topics that often affect tribal lands (worth a look)"}


def render(db, items):
    today = dt.date.today().isoformat()
    h = [f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>MT Bill Watch {today}</title>",
         "<style>body{font-family:Arial,sans-serif;color:#1a1a1a;background:#fff;max-width:760px;margin:auto;"
         "line-height:1.5;padding:1rem}a{color:#0645ad}h3{margin-bottom:.2rem}.meta{color:#333}"
         "blockquote{border-left:4px solid #555;margin:.5rem 0;padding-left:.75rem;color:#222}</style></head><body>",
         f"<h1>Montana Bill Watch: {today}</h1>",
         f"<p>{len(items)} flagged item(s) with new activity. Flags come from keyword screening "
         "and need human review before anyone acts on them.</p>"]
    t = [f"MONTANA BILL WATCH: {today}", f"{len(items)} flagged item(s). Keyword screening; review before acting.", ""]
    for tier in ("direct", "geographic", "indirect"):
        group = [i for i in items if i["tier"] == tier]
        if not group:
            continue
        h.append(f"<h2>{html.escape(TIER_HEAD[tier])} ({len(group)})</h2>")
        t += ["=" * 70, TIER_HEAD[tier].upper(), "=" * 70]
        for it in group:
            d = describe(db, it["key"])
            terms = ", ".join(sorted({m["term"].lower() for m in it["matches"]}))
            h.append(f"<h3>{html.escape(d['id'])}: {html.escape(d['title'] or '')}</h3><ul>"
                     f"<li>Requested/sponsored by: {html.escape(d['who'] or '—')}</li>"
                     f"<li>Status: {html.escape(d['status'] or '—')}</li>"
                     f"<li>What's new: {html.escape('; '.join(it['events']))}</li>"
                     f"<li>Matched terms ({html.escape(it['basis'])}): {html.escape(terms)}</li>")
            for c in d.get("calendar") or []:
                h.append(f"<li><strong>Hearing:</strong> {html.escape(str(c.get('date')))} "
                         f"{html.escape(str(c.get('time','')))} {html.escape(str(c.get('location','')))}</li>")
            if d["pdfs"]:
                h.append(f"<li>Latest text saved at: {html.escape(d['pdfs'][-1])}</li>")
            if d["link"]:
                h.append(f"<li><a href='{html.escape(d['link'])}'>{html.escape(d['link_text'])}</a></li>")
            h.append("</ul>")
            for s in it["snippets"][:3]:
                h.append(f"<blockquote>{html.escape(s)}</blockquote>")
            t += [f"\n{d['id']}: {d['title']}", f"  By: {d['who'] or '—'}", f"  Status: {d['status']}",
                  f"  New: {'; '.join(it['events'])}", f"  Terms ({it['basis']}): {terms}",
                  f"  Link: {d['link']}"] + ([f"  Latest text: {d['pdfs'][-1]}"] if d["pdfs"] else [])
            t += [f"  > {s}" for s in it["snippets"][:2]]
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


def digest(cfg, send=False):
    db, scr = connect(cfg), Screener(cfg)
    items = collect_digest(db, scr.min_score)
    if not items:
        print("No new flagged activity.")
        return None
    html_body, text_body = render(db, items)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    out = cfg["_data"] / "digests" / f"digest_{stamp}.html"
    out.write_text(html_body, encoding="utf-8")
    out.with_suffix(".txt").write_text(text_body, encoding="utf-8")
    print(f"Digest: {out} ({len(items)} items)")
    if send and cfg.get("push", {}).get("enabled"):
        send_push(cfg, db, items)
    if send and cfg.get("email", {}).get("enabled"):
        n_direct = sum(i["tier"] == "direct" for i in items)
        subj = f"{cfg['email'].get('subject_prefix','')} {len(items)} flagged ({n_direct} direct)".strip()
        if not send_email(cfg, subj, html_body, text_body):
            return out  # leave events undigested so they go out next time
    db.execute("UPDATE events SET digested=? WHERE digested IS NULL", (now(),))
    db.commit()
    return out


def send_push(cfg, db, items):
    """Send one push notification per digest via ntfy (https://ntfy.sh or self-hosted)."""
    pc = cfg["push"]
    topic = os.environ.get("BILLWATCH_NTFY_TOPIC") or pc.get("topic")
    if not topic:
        print("No ntfy topic set; skipping push.")
        return False
    n_direct = sum(i["tier"] == "direct" for i in items)
    hearing = any("hearing" in e.lower() for i in items for e in i["events"])
    lines = []
    for it in items[:6]:
        d = describe(db, it["key"])
        lines.append(f"{d['id']}: {d['title']}")
    if len(items) > 6:
        lines.append(f"…and {len(items) - 6} more")
    headers = {"Title": f"{len(items)} flagged bill update(s), {n_direct} direct",
               "Priority": "high" if (n_direct or hearing) else "default",
               "Tags": "scroll"}
    if pc.get("app_url"):
        headers["Click"] = pc["app_url"]
    token = os.environ.get("BILLWATCH_NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(f"{pc.get('server', 'https://ntfy.sh').rstrip('/')}/{topic}",
                          data="\n".join(lines).encode("utf-8"), headers=headers, timeout=30)
        r.raise_for_status()
        print("Push sent")
        return True
    except requests.RequestException as e:
        print(f"Push failed: {e}")
        return False


def export_feed(cfg):
    """Write the JSON feed the mobile app reads."""
    db, scr = connect(cfg), Screener(cfg)
    site = Path(cfg.get("site_dir", "docs"))
    if not site.is_absolute():
        site = cfg["_data"].parent / site
    site.mkdir(parents=True, exist_ok=True)
    out = []
    for f in db.execute("SELECT * FROM flags WHERE score>=? ORDER BY score DESC", (scr.min_score,)).fetchall():
        key = f["item_key"]
        if key.startswith("lc:"):  # skip drafts that already became a tracked bill
            lc = key.split(":")[2]
            if db.execute("SELECT 1 FROM bills WHERE lc=?", (lc,)).fetchone():
                continue
        d = describe(db, key)
        evs = db.execute("SELECT ts, detail FROM events WHERE item_key=? ORDER BY ts DESC LIMIT 8", (key,)).fetchall()
        out.append({
            "key": key, "kind": "draft" if key.startswith("lc:") else "bill",
            "id": d["id"], "title": d["title"], "who": d["who"], "status": d["status"],
            "tier": f["tier"], "score": f["score"], "basis": f["basis"],
            "terms": sorted({m["term"].lower() for m in json.loads(f["matches"])}),
            "snippets": json.loads(f["snippets"])[:3],
            "link": d["link"], "text_url": d.get("text_url"), "text_type": d.get("text_type"),
            "hearings": d.get("calendar") or [],
            "updated": evs[0]["ts"] if evs else f["updated"],
            "events": [{"ts": e["ts"], "detail": e["detail"]} for e in evs],
        })
    out.sort(key=lambda x: x["updated"], reverse=True)
    feed = {"generated": now(), "count": len(out), "items": out}
    (site / "feed.json").write_text(json.dumps(feed, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Feed: {site / 'feed.json'} ({len(out)} items)")


def serve(cfg, port=8000, open_browser=True):
    """Local web interface: serves the app and rebuilds feed.json from the db on every load."""
    import functools
    import http.server
    import webbrowser
    site = Path(cfg.get("site_dir", "docs"))
    if not site.is_absolute():
        site = cfg["_data"].parent / site

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0].endswith("feed.json"):
                export_feed(cfg)  # always show what's in the database right now
            super().do_GET()

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                            functools.partial(Handler, directory=str(site)))
    url = f"http://localhost:{port}/"
    print(f"Bill Watch is running at {url}  (press Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


def report(cfg, out):
    db, scr = connect(cfg), Screener(cfg)
    rows = db.execute("SELECT * FROM flags WHERE score>=? ORDER BY score DESC", (scr.min_score,)).fetchall()
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "title", "tier", "score", "basis", "matched_terms", "status", "who", "link"])
        for r in rows:
            d = describe(db, r["item_key"])
            terms = ", ".join(sorted({m["term"].lower() for m in json.loads(r["matches"])}))
            w.writerow([d["id"], d["title"], r["tier"], r["score"], r["basis"], terms,
                        d["status"], d["who"], d["link"]])
    print(f"Wrote {len(rows)} flagged items to {out}")


def run(cfg, send=False):
    inbox = cfg["_data"] / "inbox"
    for p in sorted(inbox.glob("*.csv"), key=lambda x: x.stat().st_mtime):
        ingest_csv(cfg, p)
        shutil.move(str(p), inbox / "processed" / f"{dt.datetime.now():%Y%m%d_%H%M%S}_{p.name}")
    if os.environ.get("LEGISCAN_API_KEY"):
        legiscan_sync(cfg)
    else:
        print("LEGISCAN_API_KEY not set; skipping LegiScan.")
    digest(cfg, send)
    export_feed(cfg)


def main():
    ap = argparse.ArgumentParser(description="Montana Bill Watch")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("ingest-csv"); p.add_argument("files", nargs="+")
    p.add_argument("--no-alerts", action="store_true", help="load as a baseline without creating alerts")
    sub.add_parser("legiscan-sessions")
    sub.add_parser("legiscan-sync")
    sub.add_parser("screen")
    sub.add_parser("feed")
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8000)
    p = sub.add_parser("digest"); p.add_argument("--send", action="store_true")
    p = sub.add_parser("report"); p.add_argument("--out", default="flagged_items.csv")
    p = sub.add_parser("run"); p.add_argument("--send", action="store_true")
    a = ap.parse_args()
    cfg = load_config(a.config)

    if a.cmd == "init":
        connect(cfg); print(f"Ready. Data folder: {cfg['_data']}")
    elif a.cmd == "ingest-csv":
        for f in a.files:
            ingest_csv(cfg, f)
        if a.no_alerts:
            db = connect(cfg); db.execute("UPDATE events SET digested='baseline' WHERE digested IS NULL"); db.commit()
            print("Baseline loaded; existing items won't appear in the next digest.")
    elif a.cmd == "legiscan-sessions":
        legiscan_sessions(cfg)
    elif a.cmd == "legiscan-sync":
        legiscan_sync(cfg)
    elif a.cmd == "screen":
        rescreen(cfg)
    elif a.cmd == "feed":
        export_feed(cfg)
    elif a.cmd == "serve":
        serve(cfg, a.port)
    elif a.cmd == "digest":
        digest(cfg, a.send)
    elif a.cmd == "report":
        report(cfg, a.out)
    elif a.cmd == "run":
        run(cfg, a.send)


if __name__ == "__main__":
    main()
