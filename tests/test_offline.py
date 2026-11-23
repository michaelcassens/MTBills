"""
Offline end-to-end test: a fake Bill Explorer that returns records shaped like the
real API (captured November 2026), with generated PDFs. Run from the project folder:

    python tests/test_offline.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import billwatch as bw  # noqa: E402
from legmt import LegMT  # noqa: E402


def make_pdf(text):
    from reportlab.pdfgen import canvas
    b = io.BytesIO()
    c = canvas.Canvas(b)
    y, n = 800, 1
    for line in text.split("\n"):
        c.drawString(30, y, f"{line}  {n}")  # bill PDFs carry line numbers
        y -= 14
        n += 1
        if y < 60:
            c.showPage(); y, n = 800, 1
    c.save()
    return b.getvalue()


HANNA = (ROOT / "tests" / "sample_hb21.txt").read_text()
DATA_V1 = """A BILL FOR AN ACT ENTITLED: AN ACT GENERALLY REVISING DATA CENTER LAWS;
REQUIRING WATER USE REPORTING; AND AMENDING SECTIONS 85-2-701 AND 15-6-138, MCA.
Section 1. Water use. A data center that uses water within Lake County or the Flathead Reservation
shall consult with the Confederated Salish and Kootenai Tribes before applying for a permit.
Section 2. The department of natural resources and conservation may adopt rules."""
DATA_V2 = DATA_V1.replace("""shall consult with the Confederated Salish and Kootenai Tribes before applying for a permit.""",
                          """shall report annual water use to the department.""")
FISCAL_1 = "FISCAL NOTE\nExpenditures: General fund $150,000 in FY 2028.\nRevenue impact: none."
FISCAL_2 = "FISCAL NOTE\nExpenditures: General fund $410,000 in FY 2028.\nRevenue impact: none."


def raw(rid, lc, title, status, subjects=(), bill=None, req=("LEGISLATOR", 33), bro=None, sid=3):
    return {"id": rid, "sessionId": sid, "billNumber": bill[1] if bill else None,
            "billType": {"code": bill[0]} if bill else None, "sponsorId": 33 if bill else None,
            "draft": {"draftNumber": lc, "shortTitle": title, "sessionId": sid,
                      "requesterType": req[0], "requesterId": req[1],
                      "byRequestOfs": [{"billRequestType": bro[0], "byRequestOfId": bro[1]}] if bro else [],
                      "subjects": [{"subjectCode": {"description": s}} for s in subjects],
                      "fiscalNote": True,
                      "billStatuses": [{"timeStamp": "2026-11-20T10:00:00",
                                        "billStatusCode": {"name": status}}]}}


class FakeLegMT(LegMT):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.delay = 0
        self.records = {3: [], 2: [], 20191: []}
        self.docs = {}          # (endpoint, key) -> [doc dicts]
        self.blobs = {}

    def get_json(self, path, **params):
        if path.endswith("/archive/v1/sessions"):
            return [{"ordinal": 68, "sessionId": 20231, "sessionYear": 2023, "sessionTypeCode": "R"},
                    {"ordinal": 66, "sessionId": 20191, "sessionYear": 2019, "sessionTypeCode": "R"}]
        if path.endswith("/sessions"):
            return [{"id": 2, "ordinals": "20251", "legislature": {"ordinals": "69"}, "startDate": "2025-01-06", "type": "REGULAR"},
                    {"id": 3, "ordinals": "20271", "legislature": {"ordinals": "70"}, "startDate": "2027-01-04", "type": "REGULAR"}]
        if path.endswith("/legislators"):
            return [{"id": 33, "firstName": "Matt", "lastName": "Regier", "politicalParty": {"code": "R"}}]
        if path.endswith("/organizations"):
            return [{"id": 73, "name": "Department of Natural Resources and Conservation"}]
        if "nonStandingCommittees" in path:
            return {"committeeDetails": {"committeeCode": {"name": "State Tribal Relations Committee"}}}
        ep = path.rsplit("/", 1)[-1]
        key = f"{params.get('sessionOrdinal')}:" + (params.get("lcnumber") or f"{params.get('billType')}{params.get('billNumber')}")
        return self.docs.get((ep, key), [])

    def all_items(self, session_id):
        return self.records[int(session_id)]

    def content(self, doc_id):
        return self.blobs[doc_id]

    def add_doc(self, ep, key, doc_id, fname, date, text, ses="20271"):
        self.blobs[doc_id] = make_pdf(text)
        self.docs.setdefault((ep, f"{ses}:{key}"), []).append(
            {"id": doc_id, "fileName": fname, "date": date,
             "attributes": [{"name": "DocumentLink", "stringValue": f"https://docs.legmt.gov/download-ticket?ticketId={doc_id}"}]})


def main():
    work = Path(tempfile.mkdtemp())
    for f in ("config.yaml", "billwatch.py", "analysis.py", "legmt.py"):
        shutil.copy(ROOT / f, work / f)
    (work / "data").mkdir()
    old_db = ROOT / "data" / "billwatch.db"
    if old_db.exists():
        shutil.copy(old_db, work / "data" / "billwatch.db")   # exercise the v1 migration
    cfg = bw.load_config(work / "config.yaml")
    cfg["push"] = {"enabled": True, "topic": "test", "app_url": "https://example.github.io/billwatch/"}
    sent = []
    bw.requests.post = lambda url, data=None, headers=None, timeout=None: (
        sent.append((headers, data.decode())), type("R", (), {"raise_for_status": lambda s: None})())[1]

    api = FakeLegMT(cfg)
    api.records[3] = [
        raw(5945, "LC0465", "Revise state-tribal consultation laws", "(LC) Drafter Assigned",
            ["Indians", "State Government"], req=("NON_STANDING_COMMITTEE", 29), bro=("NON_STANDING_COMMITTEE", 29)),
        raw(5483, "LC0020", "Generally revise law relating to data centers", "(LC) Drafter Assigned",
            ["Energy"], req=("AGENCY", 73), bro=("ORGANIZATION", 73)),
        raw(5484, "LC0101", "Revise affordable housing tax credit laws", "(LC) Drafter Assigned", ["Housing"]),
        raw(5485, "LC0102", "Revise motor vehicle registration fees", "(LC) Drafter Assigned", ["Motor Vehicles"]),
    ]
    print("--- first sync (baseline) ---")
    bw.legmt_sync(cfg, 3, api=api)

    print("--- second sync: LC0465 text posted; LC0020 introduced with versions and fiscal notes ---")
    api.records[3][0]["draft"]["billStatuses"].append(
        {"timeStamp": "2026-11-22T09:00:00", "billStatusCode": {"name": "(LC) Draft Delivered to Requester"}})
    api.add_doc("getBillLcs", "LC0465", 9001, "LC0465.pdf", "2026-11-22 09:00:00 -0700", HANNA)
    r = api.records[3][1]
    r["billNumber"], r["billType"], r["sponsorId"] = 101, {"code": "HB"}, 33
    r["draft"]["billStatuses"].append({"timeStamp": "2026-11-22T10:00:00", "billStatusCode": {"name": "(H) Introduced"}})
    api.add_doc("getBillVersions", "HB101", 9101, "HB0101_1.pdf", "2026-11-22 10:00:00 -0700", DATA_V1)
    api.add_doc("getBillFiscalNotes", "HB101", 9201, "HB0101_1.pdf", "2026-11-22 10:00:00 -0700", FISCAL_1)
    bw.legmt_sync(cfg, 3, api=api)

    print("--- third sync: HB 101 amended, consultation removed; fiscal note revised ---")
    r["draft"]["billStatuses"].append({"timeStamp": "2026-11-23T10:00:00", "billStatusCode": {"name": "(H) 2nd Reading Passed"}})
    api.add_doc("getBillVersions", "HB101", 9102, "HB0101_2.pdf", "2026-11-23 10:00:00 -0700", DATA_V2)
    api.add_doc("getBillVersions", "HB101", 9103, "HB0101_2(1).pdf", "2026-11-23 11:00:00 -0700", DATA_V2)  # reprint
    api.add_doc("getBillFiscalNotes", "HB101", 9202, "HB0101_2.pdf", "2026-11-23 10:00:00 -0700", FISCAL_2)
    api.add_doc("getBillAmendments", "HB101", 9301, "HB0101.001.001_Amendments-in-Context.pdf", "2026-11-23", "x")
    bw.legmt_sync(cfg, 3, api=api)

    print("--- backfill 2025 ---")
    api.records[2] = [raw(1337, "LC1339", "Establish Hanna's Act updates", "(H) Chapter Number Assigned",
                          ["Law Enforcement"], bill=("HB", 21), sid=2)]
    api.add_doc("getBillVersions", "HB21", 8001, "HB0021_X.pdf", "2025-04-01 10:00:00 -0700", HANNA, ses="20251")
    bw.backfill(cfg, 2, api=api)

    print("--- backfill 2019 (archived session format) ---")
    api.records[20191] = [
        {"id": {"billDraftNumber": "LC0099", "sessionId": 20191}, "billSortNumber": "HB0021", "billType": "HB",
         "billNumber": 21, "shortTitle": "Establish Hanna's Act", "hasFiscalNote": True,
         "requesters": [{"lawEntity": {"firstName": "Rae", "lastName": "Peppers"}, "legislator": {"x": 1}}],
         "requestOf": [{"lawEntity": {"firstName": None, "lastName": "State-Tribal Relations Committee"}, "legislator": None}],
         "primarySponsorBillRoles": [{"lawEntity": {"firstName": "Rae", "lastName": "Peppers"}, "legislator": {"x": 1}}],
         "subjects": [{"subject": {"description": "Indians"}}],
         "billActions": [{"date": "2019-05-07T10:00:00", "actionType": {"description": "Chapter Number Assigned"}}]},
        {"id": {"billDraftNumber": "LC0100", "sessionId": 20191}, "billSortNumber": "HB0022", "billType": "HB",
         "billNumber": 22, "shortTitle": "Revise motor vehicle fees", "requesters": [], "requestOf": [],
         "primarySponsorBillRoles": [], "subjects": [], "billActions": []}]
    api.add_doc("getBillVersions", "HB21", 7001, "HB0021_1.pdf", "2019-01-07 10:00:00 -0700", HANNA, ses="20191")
    api.add_doc("getBillVersions", "HB21", 7002, "HB0021_x.pdf", "2019-05-01 10:00:00 -0700",
                HANNA.replace("Notification to tribal governments.", "Consultation. The department shall consult with tribal governments."), ses="20191")
    api.add_doc("getBillVersions", "HB22", 7101, "HB0022_x.pdf", "2019-05-01 10:00:00 -0700", "AN ACT REVISING MOTOR VEHICLE FEES; " * 20, ses="20191")
    bw.backfill(cfg, year=2019, api=api)

    out = bw.digest(cfg, send=True)
    bw.export_feed(cfg)
    bw.report(cfg, work / "report.csv")

    feed = json.loads((work / "docs" / "feed-2027.json").read_text())
    by = {i["lc"]: i for i in feed["items"]}
    hb = by["LC0020"]
    lc = by["LC0465"]
    checks = {
        "LC0465 likely": lc["tribal"] == "likely",
        "LC0465 notice only (gap)": lc["consultation"] == "notice only" and lc["gap"],
        "LC0465 requester is committee": lc["requester"] == "State Tribal Relations Committee",
        "HB 101 numbered": hb["bill"] == "HB 101",
        "HB 101 reprint collapsed": len(hb["versions"]) == 2,
        "HB 101 consultation now absent": hb["consultation"] == "absent",
        "HB 101 change recorded": any(c.get("consultation_removed") for c in hb["changes"]),
        "HB 101 fiscal change": any(c.get("kind") == "fiscal" and "$410,000" in c["amounts_added"] for c in hb["changes"]),
        "HB 101 agency requester": "Department of Natural Resources" in hb["by_request_of"],
        "HB 101 law area flagged": any("Reserved Water" in d for s in hb["signals"] for d in s["detail"]),
        "Housing tagged, not tribal": by.get("LC0101", {}).get("tags") == ["Housing"] and by["LC0101"]["tribal"] == "none",
        "Motor vehicles not in feed": "LC0102" not in by,
        "2025 feed written": (work / "docs" / "feed-2025.json").exists(),
        "Push marked urgent": bool(sent) and sent[-1][0]["Priority"] == "high",
        "Migrated v1 drafts kept": feed["total_tracked"] >= 4,
        "2019 feed written": (work / "docs" / "feed-2019.json").exists(),
        "2019 Hanna's Act full history": (lambda f: (lambda i: i["tribal"] == "likely" and len(i["versions"]) == 2
            and i["consultation"] == "present" and "#/bill/20191/LC0099" in i["link"]
            and i["by_request_of"] == "State-Tribal Relations Committee")(f["items"][0]))(
            json.loads((work / "docs" / "feed-2019.json").read_text())),
        "Session list labeled": [x["label"] for x in json.loads((work / "docs" / "sessions.json").read_text())["sessions"]] == ["2027", "2025", "2019"],
    }
    for k, v in checks.items():
        print(("PASS " if v else "FAIL ") + k)
    print("Push:", sent[-1] if sent else None)
    print("Digest:", out)
    print((out.with_suffix(".txt")).read_text()[:2500] if out else "")
    print("WORKDIR", work)
    return all(checks.values())


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
