"""
Screening logic for Bill Watch.

Three independent results per item:
  * Pathways tags          -- which AIGPI priority areas a bill touches
  * Tribal interests       -- likely / possible / none, with the signals that fired
  * Consultation provision -- present / notice only / absent / unknown (no text yet)

The "gap" that matters most: tribal interests likely or possible, but the text
has no consultation provision (only notice, or nothing).
"""
import difflib
import re

MCA_RE = re.compile(r"(?<![\d-])(\d{1,2})-(\d{1,2})-(\d{3,4})(?![\d-])")
TRIBAL_WORDS = re.compile(
    r"\b(tribes?|tribal(ly)?|indians?|indigenous|reservations?|native americans?|"
    r"government-to-government|sovereign(ty)?)\b", re.I)
MONEY_RE = re.compile(r"\$\s?\d[\d,]*(\.\d+)?( (million|billion))?", re.I)


def _rx(term):
    return re.compile(r"\b(?:%s)\b" % term, re.I)


# ------------------------------------------------------------------ text cleanup
def _strip_line_numbers(lines, where):
    """Montana bill PDFs carry a line number on every line (1, 2, 3... restarting each page).
    Strip it only when most lines follow that running sequence, so real numbers survive."""
    tail = re.compile(r"\d+\s*$") if where == "end" else re.compile(r"^\s*\d+")
    body = [l for l in lines if l.strip()]
    if len(body) < 5 or sum(bool(tail.search(l)) for l in body) < 0.6 * len(body):
        return lines, False
    out, last, hits = [], 0, 0
    for l in lines:
        s = l.rstrip() if where == "end" else l.lstrip()
        done = False
        for c in (last + 1, 1, last + 2):
            n = str(c)
            if where == "end" and s.endswith(n):
                out.append(s[: -len(n)]); last, done = c, True
            elif where == "start" and s.startswith(n) and (len(s) == len(n) or not s[len(n)].isdigit()):
                out.append(s[len(n):]); last, done = c, True
            if done:
                hits += 1
                break
        if not done:
            out.append(l)
    if hits < 0.6 * len(body):
        return lines, False
    return out, True


def clean_text(text):
    """Normalize PDF-extracted bill text: drop line numbers, rejoin lines."""
    if not text:
        return ""
    lines = text.replace("\r", "").split("\n")
    lines, done = _strip_line_numbers(lines, "end")
    if not done:
        lines, _ = _strip_line_numbers(lines, "start")
    t = " ".join(l.strip() for l in lines if l.strip())
    t = re.sub(r"(\w)-\s+(\w)", r"\1-\2", t)          # "state- tribal" -> "state-tribal"
    t = re.sub(r"- \d+ -\s*Authorized Print Version[^A-Z]*", " ", t)  # page footers
    return re.sub(r"\s+", " ", t).strip()


def sentences(text):
    t = clean_text(text) if "\n" in (text or "") else (text or "")
    parts = re.split(r"(?<=[.;:])\s+(?=[A-Z(\[\"“])", t)
    return [p.strip() for p in parts if len(p.strip()) > 20]


# ------------------------------------------------------------------ analyzer
class Analyzer:
    def __init__(self, cfg):
        t = cfg["tribal_interests"]
        self.direct = [(x, _rx(x)) for x in t["direct_terms"]]
        self.geo = [(x, _rx(x)) for x in t["geography_terms"]]
        self.agency = [(x, _rx(x)) for x in t["agency_program_terms"]]
        self.subjects = [s.lower() for s in t.get("official_subjects", [])]
        self.law_areas = [(a["label"], [str(c).strip() for c in a["cites"]]) for a in t["amended_law_areas"]]
        self.agency_requesters = [_rx(r) for r in t.get("agency_requesters", [])]
        self.w = t["weights"]
        self.likely_at, self.possible_at = t["likely_at"], t["possible_at"]
        c = cfg["consultation"]
        self.c_strong = [_rx(x) for x in c["strong_terms"]]
        self.c_near = [_rx(x) for x in c["near_tribal_terms"]]
        self.c_seat = [_rx(x) for x in c.get("seat_terms", [])]
        self.c_body = [_rx(x) for x in c.get("body_terms", [])]
        self.c_notice = [_rx(x) for x in c["notice_only_terms"]]
        self.pathways = {name: [_rx(x) for x in terms] for name, terms in cfg["pathways"].items()}
        self.text_hits_needed = cfg.get("pathways_text_hits", 2)

    @staticmethod
    def mca_sections(text):
        """MCA sections cited in the bill. The title lists every section it amends."""
        found = []
        for m in MCA_RE.finditer(text or ""):
            if int(m.group(1)) <= 90:
                cite = "-".join(m.groups())
                if cite not in found:
                    found.append(cite)
        return found

    def law_area_hits(self, cites):
        hits = []
        for label, prefixes in self.law_areas:
            matched = [c for c in cites if any(c == p or (c.startswith(p) and (p.endswith("-") or len(c) > len(p)))
                                               for p in prefixes)]
            if matched:
                hits.append(f"{label} ({', '.join(matched[:5])})")
        return hits

    @staticmethod
    def _found(rules, hay):
        out = []
        for _, rx in rules:
            m = rx.search(hay)
            if m and m.group(0).lower() not in out:
                out.append(m.group(0).lower())
        return out

    def analyze(self, title, text=None, subjects=None, requester="", by_request_of=""):
        title = title or ""
        body = clean_text(text) if text else ""
        hay = f"{title} {body}"
        signals, score = [], 0

        d = self._found(self.direct, hay)
        if d:
            score += self.w["direct"]
            signals.append({"type": "Direct tribal terms", "detail": d[:8]})
        subj = [s for s in (subjects or []) if any(s.lower().startswith(x) for x in self.subjects)]
        if subj:
            score += self.w["direct"]
            signals.append({"type": "Official subject", "detail": subj})
        cites = self.mca_sections(body)
        areas = self.law_area_hits(cites)
        if areas:
            score += self.w["law_area"]
            signals.append({"type": "Amends tribal-related law", "detail": areas})
        g = self._found(self.geo, hay)
        if g:
            score += self.w["geography"]
            signals.append({"type": "Geography", "detail": g[:8]})
        if score > 0:  # the next two only count alongside another signal
            a = self._found(self.agency, hay)
            if a:
                score += self.w["agency_program"]
                signals.append({"type": "Agency, program, or resource", "detail": a[:6]})
            req = f"{requester or ''} | {by_request_of or ''}"
            if any(rx.search(req) for rx in self.agency_requesters):
                score += self.w["agency_requester"]
                signals.append({"type": "Requested by an agency or board",
                                "detail": [by_request_of or requester]})

        level = "likely" if score >= self.likely_at else "possible" if score >= self.possible_at else "none"
        consult = self.consultation(body) if body else {"status": "unknown", "evidence": []}
        return {
            "tags": self.tags(title, body, subjects),
            "tribal": level, "tribal_score": score, "signals": signals,
            "consultation": consult["status"], "consultation_evidence": consult["evidence"],
            "mca": cites[:60], "basis": "full text" if body else "title only",
            "gap": level in ("likely", "possible") and consult["status"] in ("absent", "notice only"),
        }

    def consultation(self, body):
        evidence, status, notice = [], "absent", False
        for s in sentences(body):
            tribal = bool(TRIBAL_WORDS.search(s))
            strong = any(rx.search(s) for rx in self.c_strong)
            near = tribal and any(rx.search(s) for rx in self.c_near)
            seat = tribal and any(rx.search(s) for rx in self.c_seat) and any(rx.search(s) for rx in self.c_body)
            if strong or near or seat:
                status = "present"
                if sum(e["kind"] == "consultation" for e in evidence) < 4:
                    evidence.append({"kind": "consultation", "text": s[:450]})
            elif tribal and any(rx.search(s) for rx in self.c_notice):
                notice = True
                if sum(e["kind"] == "notice" for e in evidence) < 2:
                    evidence.append({"kind": "notice", "text": s[:450]})
        if status != "present" and notice:
            status = "notice only"
        return {"status": status, "evidence": evidence}

    def tags(self, title, body, subjects):
        head = f"{title} {' '.join(subjects or [])}"
        out = []
        for name, rules in self.pathways.items():
            if any(rx.search(head) for rx in rules) or \
               (body and sum(len(rx.findall(body)) for rx in rules) >= self.text_hits_needed):
                out.append(name)
        return out


# ------------------------------------------------------------------ comparisons
def _sentence_diff(old_text, new_text):
    a, b = sentences(clean_text(old_text)), sentences(clean_text(new_text))
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    added, removed = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("delete", "replace"):
            removed += a[i1:i2]
        if op in ("insert", "replace"):
            added += b[j1:j2]
    return added, removed


def compare_versions(analyzer, old_text, new_text, old_label, new_label, title=""):
    """What changed between two versions: language, flagged terms, MCA sections, consultation."""
    added, removed = _sentence_diff(old_text, new_text)
    o, n = analyzer.analyze(title, old_text), analyzer.analyze(title, new_text)
    flat = lambda r: {f"{s['type']}: {x}" for s in r["signals"] for x in s["detail"]}
    return {
        "kind": "version", "from": old_label, "to": new_label,
        "n_added": len(added), "n_removed": len(removed),
        "added": [s[:500] for s in added[:25]], "removed": [s[:500] for s in removed[:25]],
        "signals_added": sorted(flat(n) - flat(o)), "signals_removed": sorted(flat(o) - flat(n)),
        "mca_added": [c for c in n["mca"] if c not in o["mca"]],
        "mca_removed": [c for c in o["mca"] if c not in n["mca"]],
        "consultation_change": None if o["consultation"] == n["consultation"]
        else f"{o['consultation']} → {n['consultation']}",
        "tribal_change": None if o["tribal"] == n["tribal"] else f"{o['tribal']} → {n['tribal']}",
        "consultation_removed": o["consultation"] == "present" and n["consultation"] != "present",
    }


def compare_fiscal(old_text, new_text, old_label, new_label):
    """Fiscal note change summary: dollar figures that appeared or disappeared."""
    om = {m.group(0) for m in MONEY_RE.finditer(clean_text(old_text))}
    nm = {m.group(0) for m in MONEY_RE.finditer(clean_text(new_text))}
    added, removed = _sentence_diff(old_text, new_text)
    money_lines = lambda xs: [s[:400] for s in xs if MONEY_RE.search(s)][:10]
    return {"kind": "fiscal", "from": old_label, "to": new_label,
            "amounts_added": sorted(nm - om)[:20], "amounts_removed": sorted(om - nm)[:20],
            "added": money_lines(added), "removed": money_lines(removed),
            "n_added": len(added), "n_removed": len(removed)}
