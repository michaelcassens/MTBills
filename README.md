# Montana Bill Watch

Screens every Montana LC draft and bill for **tribal interests** and **tribal consultation**, tags each with **AIGPI Pathways** areas, tracks what amendments change, and gives AIGPI reviewers a place to confirm what goes on the public tracker for the Montana American Indian Caucus.

## What it does

**Collects** from Bill Explorer's own data service (the same one bills.legmt.gov uses): every draft request with its requester, by-request-of, status, and official subjects; draft text once posted; every bill version; fiscal notes; amendments. The Bill Explorer CSV export still works as a fallback.

**Screens** each item three ways (all term lists live in `config.yaml`):

| Result | How |
|---|---|
| **Tribal interests**: likely, possible, or none | Points from: direct tribal terms; Bill Explorer's "Indians" subject code; amending a tribal-related section of the MCA (IEFA, ICWA, water compacts, Hanna's Act, state-tribal relations, and more); geography (reservations, nations, hub cities Missoula, Great Falls, Billings, nearby counties); and, only alongside another signal, creating or changing an agency program, board, rule, or grant, or being requested by an agency. |
| **Consultation provision**: present, notice only, absent, or unknown | Read sentence by sentence. "Consult with tribes," state-tribal agreements, tribal seats on a board count. The standard "send a copy of [this act] to each tribal government" counts as **notice only**, not consultation. |
| **Pathways tags** | Tribal consultation; substance misuse and mental health; housing; education; economic development and tourism; law enforcement and criminal justice. |

**The key flag** is *tribal interests likely or possible, but no consultation provision*. Those land in **Needs attention**.

**Tracks amendments.** Each new version is compared with the last: language added and removed, flags gained or lost, MCA sections added or dropped, and whether consultation language was removed (a priority alert). Revised fiscal notes show which dollar figures changed.

**AIGPI review.** Reviewers sign in on the app with an email link, then mark each item *confirmed*, *not implicated*, or *not yet reviewed*, and record whether the text has a consultation provision (yes, partial, no). Confirmed items get the **AIGPI tracking** flag. Items amended after review show *Amended since AIGPI review* and return to the queue. Public notes show to everyone; internal notes are reviewers only. Every change is logged.

**Publishes** everything to the app (public, per your choice): views for Needs attention, Tribal interests, AIGPI tracking, Review queue (reviewers only), and All Pathways, plus a 2027/2025 session switch, Pathways filter, and search.

## Setup

### 1. GitHub (runs everything, free)
1. Create a repository and upload this folder, including the hidden `.github` folder and `data/billwatch.db`.
2. **Settings → Actions → General → Workflow permissions:** Read and write.
3. **Settings → Pages:** deploy from branch `main`, folder `/docs`. The app is at `https://<user>.github.io/<repo>/`.
4. **Settings → Secrets and variables → Actions:** add `BILLWATCH_NTFY_TOPIC` (a long random name) for push alerts. `LEGISCAN_API_KEY` is optional now.
5. **Actions → Bill Watch → Run workflow → run.** The first run loads all current drafts as a baseline (no alert flood) and fetches posted text, up to 600 items per run; the rest come in on the next runs.

### 2. 2025 retrospective
**Actions → Bill Watch → Run workflow → backfill-2025.** It reads the text of every 2025 bill, then the full version history of those with tribal interests, so you can see where consultation language was added or stripped. Expect an hour or more. The results appear under **2025 session** in the app and in the review queue.

### 3. AIGPI review (Supabase, free tier)
1. Create a project at supabase.com.
2. **SQL Editor → New query:** paste `supabase/schema.sql`, add your reviewers' emails on the last lines, and run it.
3. **Authentication → URL Configuration:** set Site URL to your app URL.
4. **Project Settings → API:** copy the Project URL and the `anon` public key into `config.yaml` under `review:`. Both are safe to publish; the database rules only let listed reviewers write.
5. Commit. Reviewers open the app, choose **Reviewer sign in**, and click the emailed link.

To add a reviewer later: Supabase **Table Editor → reviewers → Insert row**.

### 4. Phones
Install **ntfy** and subscribe to your topic; open the app URL and **Add to Home Screen**. In `config.yaml`, set `push.enabled: true` and `push.app_url`.

## On your own computer
```
python -m pip install -r requirements.txt
python billwatch.py sync            # pull 2027 drafts and text
python billwatch.py serve           # open the app at http://localhost:8000
python billwatch.py report          # every flagged item to a CSV for Excel
```
Other commands: `backfill --session 2`, `ingest-csv FILE.csv`, `reviews`, `screen`, `digest`, `feed`, `run`.

## Tuning
Edit term lists in `config.yaml` and commit; the workflow re-scores everything. The MCA list under `amended_law_areas` is a starter set for AIGPI to verify and extend. Err toward catching too much: a missed bill costs more than an extra item in the queue.

## Known limits
- Bill Explorer's data service is public but undocumented. If the Legislature changes or blocks it, the run log says so; `legmt.py` is the one file to update, and the CSV export keeps working meanwhile.
- Screening reads words, not intent. It nominates; AIGPI decides.
- Scanned PDFs without a text layer can't be read and will screen on title only.
- When you edit `docs/index.html`, change the `CACHE` name in `docs/sw.js` so phones load the new version.

## Tests
`python tests/test_offline.py` runs the whole pipeline against a stand-in Bill Explorer (needs `reportlab`).
