# Montana Bill Watch

Tracks Montana LC draft requests and introduced bills. It screens each one for possible effects on tribal lands, tribal nations, and nearby areas, and writes a digest of new activity for review before alerts go to caucus members.

## What it does

1. **LC drafts.** It diffs each Bill Explorer CSV export against the last one and records new requests and status changes. When a draft moves into a status where text is likely viewable, the alert says so.
2. **Introduced bills.** It pulls from the LegiScan API, fetching only bills whose `change_hash` moved. It also downloads each new text version as a PDF, extracts the text, and catches new floor actions and scheduled hearings.
3. **Screening.** Terms are matched in three tiers set in `config.yaml`:
   - *direct*: tribes, reservations, ICWA, compacts, and similar
   - *geographic*: tribal nations and counties overlapping or bordering reservations
   - *indirect*: topics like water, data centers, mining, hunting, and jurisdiction

   LC drafts are screened on their title. Once a draft becomes a bill, the LC and the bill are re-screened on the full text, and the alert notes when a draft moves up a tier.
4. **Alerts.** Push notifications go out via ntfy, with high priority for direct hits and hearings, and an optional email digest.
5. **Mobile app.** A home-screen web app in `docs/` lists flagged items with tier filters, search, upcoming hearings, "new since last visit" markers, snippets, and PDF links. It works offline with the last feed.
6. **Digest.** The digest is grouped by tier, with matched terms, text snippets, status, hearings, and links. It's written as accessible HTML plus plain text, and can optionally be emailed.

## Phone setup (GitHub Actions + Pages + ntfy)

No server to maintain. GitHub runs the collector every 4 hours, publishes the app, and sends push alerts.

1. **Create a GitHub repository** and upload everything in this folder, including the hidden `.github` folder and `data/billwatch.db`. The database already holds a baseline of the 2027 draft list, so the first run won't flood you with alerts.
2. **Add secrets** under Settings → Secrets and variables → Actions:
   - `LEGISCAN_API_KEY`
   - `BILLWATCH_NTFY_TOPIC`: a long random name such as `mt-billwatch-7f3k9q2x`. Anyone who knows it can subscribe.
   - optionally `BILLWATCH_SMTP_PASSWORD` and `BILLWATCH_NTFY_TOKEN`
3. **Turn on Pages** under Settings → Pages: deploy from branch `main`, folder `/docs`. Your app URL will be `https://<user>.github.io/<repo>/`.
4. **Edit `config.yaml`:** set `push.enabled: true` and `push.app_url` to that URL, so tapping an alert opens the app.
5. **On each phone:**
   - Install the ntfy app and subscribe to your topic.
   - Open the app URL and choose Add to Home Screen (Share menu in Safari, the ⋮ menu in Chrome).
6. **Run it now:** Actions tab → Bill Watch → Run workflow.

**New draft lists:** upload a fresh Bill Explorer CSV into `data/inbox/` (the GitHub mobile app or website works). That triggers a run right away.

**Privacy note:** GitHub Pages sites are public, even from a private repo on most plans. The data is public legislative information, but if the caucus would rather keep its flagged list private, host the `docs` folder somewhere with a login instead.

## Local setup

```bash
pip install -r requirements.txt
python billwatch.py init
```

Get a free LegiScan API key at https://legiscan.com/legiscan, then set it as an environment variable. Don't put it in the config file.

```powershell
setx LEGISCAN_API_KEY "your-key"            # Windows (open a new terminal afterward)
```

## First run (baseline)

Load the current draft list without generating hundreds of alerts:

```bash
python billwatch.py ingest-csv Filtered_Bills_2026-11-07.csv --no-alerts
python billwatch.py report --out flagged_now.csv     # everything currently flagged
```

## Daily use

1. Export the LC list from Bill Explorer as a CSV.
2. Drop it into `data/inbox/`.
3. Run:

```bash
python billwatch.py run            # inbox CSVs + LegiScan + digest
python billwatch.py run --send     # same, and email it (email.enabled: true in config)
```

Digests land in `data/digests/`. PDFs land in `data/texts/`. The app feed is written to `docs/feed.json`, and `python billwatch.py feed` rebuilds it on its own.

**Scheduling on Windows.** In Task Scheduler, create a task that runs `python C:\path\to\billwatch.py run --send` every 4 hours.

## Web interface on your computer

```bash
python billwatch.py serve
```

This opens the app at http://localhost:8000 in your browser. It reads straight from `billwatch.db`, so Refresh always shows the latest data. Press Ctrl+C in the terminal to stop it. Only your own computer can reach it.

## Tuning

Edit the term lists in `config.yaml`, then run `python billwatch.py screen` to re-score everything. Err toward catching too much, since a missed bill costs more than an extra alert.

## Known limits

- **LC draft text** isn't fetched automatically yet. Drafts are screened on title until they become bills. A future step is pulling draft text from Bill Explorer's own backend (find its endpoints in the browser dev tools Network tab).
- **The CSV export is manual.** Automating it is the same future step.
- **Keyword screening is a first pass.** Titles like "Generally revise…" hide content. The planned next layer is an LLM read of full text that answers "could this affect tribal lands, members, jurisdiction, water, or resources?" and explains why.
- **Flags are leads, not conclusions.** A person should review the digest before alerting caucus members.
