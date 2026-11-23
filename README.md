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

**Publishes** everything to the app (public, per your choice): views for Needs attention, Tribal interests, AIGPI tracking, Review queue (reviewers only), and All Pathways, plus a session picker (2027 and any past years you load), Pathways filter, and search. The numbers on the view buttons count what that view would show with your current Pathways and search filters.

## Setup

### 1. GitHub (runs everything, free)
1. Create a repository and upload this folder, including the hidden `.github` folder and `data/billwatch.db`.
2. **Settings → Actions → General → Workflow permissions:** Read and write.
3. **Settings → Pages:** deploy from branch `main`, folder `/docs`. The app is at `https://<user>.github.io/<repo>/`.
4. **Settings → Secrets and variables → Actions:** add `BILLWATCH_NTFY_TOPIC` (a long random name) for push alerts. `LEGISCAN_API_KEY` is optional now.
5. **Actions → Bill Watch → Run workflow → run.** The first run loads all current drafts as a baseline (no alert flood) and fetches posted text, up to 600 items per run; the rest come in on the next runs.

### 2. Past sessions (1999 to 2025)
**Actions → Bill Watch → Run workflow**, choose **backfill**, and type the year (any regular session: 2025, 2023, 2021 ... 1999). It reads the final text of every bill that year, then the full version history of those with tribal interests, so you can see where consultation language was added or stripped. Each year shows up in the app's session picker and in the review queue.

A year can take an hour or two. Each run handles up to 1,500 bills; if the log ends with "bills still to do," run the same year again and it picks up where it stopped. To keep the database small, text is kept only for bills with tribal interests. Sessions before 2025 come from the Legislature's archive, so requester names there are as the archive lists them. Special sessions: `python billwatch.py backfill --session 20172` (2017 special).

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
Other commands: `backfill --year 2023`, `ingest-csv FILE.csv`, `reviews`, `screen`, `digest`, `feed`, `run`.

## Tuning
Edit term lists in `config.yaml` and commit; the workflow re-scores everything. The MCA list under `amended_law_areas` is a starter set for AIGPI to verify and extend. Err toward catching too much: a missed bill costs more than an extra item in the queue.

## Known limits
- Bill Explorer's data service is public but undocumented. If the Legislature changes or blocks it, the run log says so; `legmt.py` is the one file to update, and the CSV export keeps working meanwhile.
- Screening reads words, not intent. It nominates; AIGPI decides.
- Scanned PDFs without a text layer can't be read and will screen on title only.
- When you edit `docs/index.html`, change the `CACHE` name in `docs/sw.js` so phones load the new version.

## Tests
`python tests/test_offline.py` runs the whole pipeline against a stand-in Bill Explorer (needs `reportlab`).
