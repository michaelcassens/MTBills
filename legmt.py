"""
Client for the Montana Legislature's Bill Explorer backend (bearbeta.legmt.gov).

These are the same public, read-only endpoints the Bill Explorer website calls.
They are undocumented, so this module keeps every call in one place; if the
Legislature changes them, this is the only file to fix.
"""
import io
import re
import time

import requests

BILL_EXPLORER = "https://bills.legmt.gov/#"


class LegMTError(RuntimeError):
    pass


class LegMT:
    def __init__(self, cfg):
        c = cfg.get("legmt", {})
        self.base = c.get("api", "https://bearbeta.legmt.gov").rstrip("/")
        self.delay = c.get("request_delay", 0.25)
        self.s = requests.Session()
        contact = c.get("contact")
        self.s.headers.update({
            "Accept": "application/json, application/pdf, */*",
            "User-Agent": "MT-Bill-Watch/2.0 (public bill tracking for tribal consultation)"
                          + (f"; contact {contact}" if contact else ""),
            "Origin": "https://bills.legmt.gov", "Referer": "https://bills.legmt.gov/",
        })
        self._sessions = None
        self._cache = {}

    # ---------------------------------------------------------------- http
    def _req(self, method, path, **kw):
        time.sleep(self.delay)
        url = path if path.startswith("http") else self.base + path
        for attempt in range(3):
            try:
                r = self.s.request(method, url, timeout=60, **kw)
            except requests.RequestException as e:
                if attempt == 2:
                    raise LegMTError(f"Could not reach Bill Explorer ({e})") from e
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            ctype = r.headers.get("content-type", "")
            if r.status_code == 403 or ("text/html" in ctype and "json" not in ctype and path.find("getContent") < 0):
                raise LegMTError("Bill Explorer refused the request (it may be blocking automated access). "
                                 "Use the CSV export instead: python billwatch.py ingest-csv <file>")
            r.raise_for_status()
            return r
        raise LegMTError("Bill Explorer did not respond")

    def get_json(self, path, **params):
        return self._req("GET", path, params=params or None).json()

    def post_json(self, path, body, **params):
        return self._req("POST", path, params=params or None, json=body).json()

    # ---------------------------------------------------------------- reference data
    def sessions(self):
        if self._sessions is None:
            self._sessions = {s["id"]: s for s in self.get_json("/legislators/v1/sessions")}
        return self._sessions

    def archive_sessions(self):
        """Sessions before Bill Explorer (1999-2023), keyed by their 5-digit id, e.g. 20231."""
        if not hasattr(self, "_archive"):
            self._archive = {str(s["sessionId"]): s for s in self.get_json("/archive/v1/sessions")}
        return self._archive

    @staticmethod
    def is_archive(session_id):
        return len(str(session_id)) == 5

    def ordinals(self, session_id):
        if self.is_archive(session_id):
            a = self.archive_sessions()[str(session_id)]
            return str(a["ordinal"]), str(a["sessionId"])
        s = self.sessions()[int(session_id)]
        return s["legislature"]["ordinals"], s["ordinals"]  # e.g. ("70", "20271")

    def session_label(self, session_id):
        if self.is_archive(session_id):
            a = self.archive_sessions().get(str(session_id), {})
            return f"{a.get('sessionYear', session_id)}" + (" special" if a.get("sessionTypeCode") == "S" else "")
        s = self.sessions().get(int(session_id), {})
        return (s.get("startDate") or "")[:4] + (" special" if s.get("type") == "SPECIAL" else "")

    def session_for_year(self, year, special=False):
        """Find the session id for a year: Bill Explorer ids for 2025 on, archive ids before."""
        for sid, s in self.sessions().items():
            if (s.get("startDate") or "")[:4] == str(year) and (s.get("type") == "SPECIAL") == special:
                return str(sid)
        for sid, a in self.archive_sessions().items():
            if str(a.get("sessionYear")) == str(year) and (a.get("sessionTypeCode") == "S") == special:
                return sid
        return None

    def _lookup(self, key, loader):
        if key not in self._cache:
            try:
                self._cache[key] = loader()
            except Exception:  # noqa: BLE001 - a missing name should never stop a sync
                self._cache[key] = None
        return self._cache[key]

    def legislators(self):
        return self._lookup("legislators", lambda: {l["id"]: l for l in self.get_json("/legislators/v1/legislators")})

    def organizations(self):
        return self._lookup("orgs", lambda: {o["id"]: o["name"] for o in self.get_json("/legislators/v1/organizations")})

    def committee_name(self, cid):
        def load():
            j = self.get_json(f"/committees/v1/nonStandingCommittees/{cid}")
            return j["committeeDetails"]["committeeCode"]["name"]
        return self._lookup(f"com:{cid}", load)

    def legislator_name(self, lid, last_first=True):
        l = (self.legislators() or {}).get(lid)
        if not l:
            return None
        party = ((l.get("politicalParty") or {}).get("code") or "").strip()
        name = f"{l.get('lastName', '')}, {l.get('firstName', '')}" if last_first else \
            f"{l.get('firstName', '')} {l.get('lastName', '')}"
        return name.strip(", ") + (f" ({party})" if party else "")

    def entity_name(self, kind, eid):
        if eid is None:
            return ""
        if kind == "LEGISLATOR":
            return self.legislator_name(eid) or f"Legislator {eid}"
        if kind == "NON_STANDING_COMMITTEE":
            return self.committee_name(eid) or f"Committee {eid}"
        if kind in ("AGENCY", "ORGANIZATION"):
            return (self.organizations() or {}).get(eid) or f"Organization {eid}"
        return f"{kind.title()} {eid}"

    # ---------------------------------------------------------------- bills and drafts
    def all_items(self, session_id):
        """Every draft request and bill in a session."""
        if self.is_archive(session_id):
            out, page = [], 0
            while True:  # archive records are large, so page through them (offset = page number)
                j = self.post_json("/archive/v1/bills/search", {"sessionId": int(session_id)}, limit=500, offset=page)
                b = j.get("bills", {})
                out += b.get("content", [])
                page += 1
                if not b.get("content") or len(out) >= b.get("totalElements", 0):
                    return out
        j = self.post_json("/bills/v1/bills/filter", [{"sessionIds": [int(session_id)]}], limit=10000, offset=0)
        return j.get("content", j)

    def normalize(self, raw):
        if "billSortNumber" in raw or ("id" in raw and isinstance(raw["id"], dict)):
            return self.normalize_archive(raw)
        return self.normalize_current(raw)

    @staticmethod
    def _entity(role):
        e = role.get("lawEntity") or {}
        if role.get("legislator") or e.get("firstName"):
            return f"{e.get('lastName', '')}, {e.get('firstName', '')}".strip(", ")
        return e.get("lastName") or ""

    def normalize_archive(self, raw):
        acts = sorted(raw.get("billActions") or [], key=lambda a: a.get("date") or "")
        last = acts[-1] if acts else {}
        bt, no = raw.get("billType"), raw.get("billNumber")
        reqs = raw.get("requesters") or []
        return {
            "be_id": None, "session_id": (raw.get("id") or {}).get("sessionId"),
            "lc": (raw.get("id") or {}).get("billDraftNumber"), "title": raw.get("shortTitle") or "",
            "bill_number": f"{bt} {no}" if bt and no else None, "bill_type": bt if no else None, "bill_no": no,
            "requester": "; ".join(self._entity(r) for r in reqs),
            "requester_type": "LEGISLATOR" if any(r.get("legislator") for r in reqs) else "ORGANIZATION",
            "by_request_of": "; ".join(self._entity(r) for r in raw.get("requestOf") or []),
            "sponsor": "; ".join(self._entity(r) for r in raw.get("primarySponsorBillRoles") or []),
            "subjects": [((s.get("subject") or {}).get("description")) for s in raw.get("subjects") or []
                         if (s.get("subject") or {}).get("description")],
            "status": re.sub(r"^\((LC|H|S|C)\)\s*", "", ((last.get("actionType") or {}).get("description") or "")),
            "status_date": (last.get("date") or "")[:10],
            "fiscal_note_expected": bool(raw.get("hasFiscalNote")),
            "history": [{"date": (a.get("date") or "")[:10],
                         "status": (a.get("actionType") or {}).get("description")} for a in acts][-15:],
        }

    def normalize_current(self, raw):
        """Flatten one Bill Explorer record into the fields Bill Watch stores."""
        d = raw.get("draft") or {}
        sts = sorted(d.get("billStatuses") or [], key=lambda s: s.get("timeStamp") or "")
        last = sts[-1] if sts else {}
        bt = (raw.get("billType") or {}).get("code")
        number = f"{bt} {raw['billNumber']}" if bt and raw.get("billNumber") else None
        bro = d.get("byRequestOfs") or []
        return {
            "be_id": raw.get("id"), "session_id": raw.get("sessionId") or d.get("sessionId"),
            "lc": d.get("draftNumber"), "title": d.get("shortTitle") or "",
            "bill_number": number, "bill_type": bt, "bill_no": raw.get("billNumber"),
            "requester": self.entity_name(d.get("requesterType"), d.get("requesterId")),
            "requester_type": d.get("requesterType"),
            "by_request_of": "; ".join(self.entity_name(b.get("billRequestType"), b.get("byRequestOfId")) for b in bro),
            "sponsor": self.legislator_name(raw.get("sponsorId")) if raw.get("sponsorId") else "",
            "subjects": [((s.get("subjectCode") or {}).get("description")) for s in d.get("subjects") or []
                         if (s.get("subjectCode") or {}).get("description")],
            "status": re.sub(r"^\((LC|H|S)\)\s*", "", ((last.get("billStatusCode") or {}).get("name") or "")),
            "status_date": (last.get("timeStamp") or "")[:10],
            "fiscal_note_expected": bool(d.get("fiscalNote")),
            "history": [{"date": (s.get("timeStamp") or "")[:10],
                         "status": (s.get("billStatusCode") or {}).get("name")} for s in sts][-15:],
        }

    @staticmethod
    def explorer_url(session_id, lc, introduced):
        if len(str(session_id)) == 5:  # archived session (2023 and earlier)
            return f"{BILL_EXPLORER}/bill/{session_id}/{lc}"
        return f"{BILL_EXPLORER}/{'laws' if introduced else 'lc'}/bill/{session_id}/{lc}"

    # ---------------------------------------------------------------- documents
    def _doc_list(self, endpoint, session_id, **params):
        leg, ses = self.ordinals(session_id)
        try:
            j = self.get_json(f"/docs/v1/documents/{endpoint}", legislatureOrdinal=leg, sessionOrdinal=ses, **params)
        except requests.HTTPError:
            return []
        docs = []
        for x in j or []:
            link = next((a.get("stringValue") for a in x.get("attributes") or [] if a.get("name") == "DocumentLink"), None)
            docs.append({"doc_id": x["id"], "file": x.get("fileName", ""), "date": x.get("date", ""),
                         "url": link or f"{self.base}/docs/v1/documents/getContent?documentId={x['id']}"})
        return docs

    def lc_text_docs(self, session_id, lc):
        return self._doc_list("getBillLcs", session_id, lcnumber=lc)

    def bill_versions(self, session_id, bill_type, bill_no):
        return latest_per_version(self._doc_list("getBillVersions", session_id, billType=bill_type, billNumber=bill_no))

    def fiscal_notes(self, session_id, bill_type, bill_no):
        return latest_per_version(self._doc_list("getBillFiscalNotes", session_id, billType=bill_type, billNumber=bill_no))

    def amendments(self, session_id, bill_type, bill_no):
        return self._doc_list("getBillAmendments", session_id, billType=bill_type, billNumber=bill_no)

    def content(self, doc_id):
        r = self._req("GET", "/docs/v1/documents/getContent", params={"documentId": doc_id})
        return r.content


VERSION_RE = re.compile(r"_(\w+?)(?:\(\d+\))?\.pdf$", re.I)  # HB0021_x.pdf in older sessions


def version_label(fname):
    """HB0178_1.pdf -> '1'; HB0178_2(14).pdf -> '2'; HB0178_X.pdf -> 'X'."""
    m = VERSION_RE.search(fname or "")
    return m.group(1).upper() if m else (fname or "?")


VERSION_NAMES = {"1": "Introduced", "2": "2nd reading", "3": "3rd reading", "X": "Enrolled"}


def version_name(label):
    return f"Version {label}" + (f" ({VERSION_NAMES[label]})" if label in VERSION_NAMES else "")


def latest_per_version(docs):
    """Bill Explorer keeps reprints (HB0178_2(1).pdf ... (14)); keep the newest of each version."""
    best = {}
    for d in docs:
        v = version_label(d["file"])
        if v not in best or d["date"] > best[v]["date"]:
            best[v] = dict(d, version=v)
    order = lambda v: (1, 0) if v == "X" else (0, int(v)) if v.isdigit() else (0, 999)
    return [best[v] for v in sorted(best, key=order)]


def pdf_to_text(raw):
    from pypdf import PdfReader
    try:
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
    except Exception as e:  # noqa: BLE001
        print(f"  ! could not read PDF: {e}")
        return ""
