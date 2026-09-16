# Client File Upload & Quoting Portal

A drag-and-drop portal for clients to upload job files (PDF, JPG, ZIP, PPTX, XLSX).
Every submission is analysed automatically in the background - page/slide/sheet
counts, physical page sizes, and flags for anything unusual (not A4/A3/A2/etc) -
and a report is emailed to you so you can quote without opening every file
yourself.

This has been built and tested end-to-end in this session (see the test run
against the sample files in `samples/`). It is **not yet live on the internet**
- see "Going live" below for what that last step involves.

## What it does

1. Client drags files onto the page and hits submit.
2. Files are saved to the server and the client immediately sees a "received" confirmation.
3. In the background, each file is analysed:
   - **PDF** - page count + physical size of every page (in mm), compared against A0-A6, US Letter/Legal, Tabloid.
   - **JPG/PNG** - pixel dimensions converted to an estimated print size using the image's DPI metadata; if no DPI tag exists (common with phone photos/scans), 300 DPI is assumed and this is flagged.
   - **PPTX** - slide count + the deck's slide size, compared against standard 4:3/16:9/A4-slide formats.
   - **XLSX** - for each sheet: if a print/paper size was explicitly set in the file, that's used (reliable); if not, the physical size of the used cell range is *estimated* from column widths/row heights and flagged for a manual check, since Excel doesn't reliably store a true "page size" otherwise.
   - **ZIP** - unpacked recursively (including nested zips and subfolders) and every file inside is analysed and listed individually against the archive it came from.
4. A report (HTML email body + CSV attachment) is generated listing every item, its size, and whether it's flagged, and emailed to you. Small original files are attached too; anything too large for email is left on the server for you to retrieve.
5. Submission folders (uploads + their reports) are automatically deleted after 30 days.

## Known limitations (be aware of these before quoting off the report alone)

- **JPG/PNG size is an estimate** unless the file carries DPI metadata. A scanned or professionally exported file usually has this; a phone photo often doesn't.
- **Excel page size is only reliable when the print setup was explicitly configured** in the file. Otherwise the report gives a rough estimate from column/row dimensions, not a true print size - it's flagged specifically so you know to eyeball it.
- **PowerPoint has one slide size per file** (not per-slide) - PowerPoint itself doesn't allow mixed slide sizes in one deck, so this isn't a limitation of the tool.
- File-type detection is by extension. A file with a wrong or missing extension will be skipped and listed as unsupported.

## Test it yourself first (5 minutes, on your own computer)

1. Unzip `client_portal.zip` somewhere on your computer.
2. Open Terminal (Mac) and `cd` into the unzipped folder.
3. Make sure Python 3 is installed: `python3 --version`. If that fails, install Python from python.org first.
4. Install the dependencies:
   ```bash
   pip3 install -r requirements.txt
   ```
5. Start the app:
   ```bash
   python3 app.py
   ```
6. Open **http://localhost:5000** in your browser. Drag a few files from the `samples/` folder onto the page and submit.
7. You'll see the "thanks, received" message immediately. The analysis happens a second or two later in the background.
8. Since email isn't configured yet, open the `reports/` folder that appears - you'll find an `.html` file for your submission. Open it in a browser to see exactly what the emailed report will look like (this is the same file that gets emailed once SMTP is set up - see below).
9. When you're done testing, go back to Terminal and press `Ctrl+C` to stop the server.

If you want to test the *real* email as well before deploying, set the three
environment variables from "Turning on real email delivery" below in the same
Terminal window before running `python3 app.py`, then submit again - you
should get an actual email this time instead of a saved file.

## Running it locally

```bash
pip3 install -r requirements.txt
python3 app.py
```

Then open http://localhost:5000 - drag files in and submit. Without email
credentials configured (see below), the report is saved to `reports/` as an
HTML file instead of being sent, so you can see exactly what it looks like.

## Turning on real email delivery

The portal is a standalone program - it can't use Claude's Gmail connection
from this chat (that only exists inside this conversation). It needs its own
way to send mail, and there are two supported methods - use whichever fits
where you're running it.

### Method A: Brevo (use this on Render, or anywhere hosted)

Render's **free** web services block outbound traffic on the old SMTP ports
(25/465/587) entirely, as an anti-abuse measure - so a normal Gmail SMTP
login will always fail there with "Network is unreachable", no matter how
correct the credentials are. Brevo sends email over a normal HTTPS request
instead, which isn't blocked, and its free plan (300 emails/day, forever, no
credit card) is far more than this needs.

1. Sign up free at https://www.brevo.com (no credit card needed).
2. Verify a sender address: in Brevo, go to **Senders, Domains & Dedicated IPs → Senders → Add a sender**, enter the email address you want reports to appear FROM (e.g. your own Gmail, or `quotes@sandyboy.co.uk`), and click the confirmation link Brevo emails to that address.
3. Get an API key: go to **Settings → API Keys → Generate a new API key**, name it "client portal", and copy it.
4. Set these as environment variables (in Render: Environment tab → Add variable; locally: `export` before running):
   ```bash
   export BREVO_API_KEY="xkeysib-xxxxxxxxxxxxxxxx"
   export EMAIL_FROM="the address you verified in step 2"
   export REPORT_RECIPIENT="kaye@sandyboy.co.uk"
   ```
5. Restart/redeploy. Submit a test file and check the inbox.

### Method B: Gmail SMTP (use this only when running locally, or on a paid instance)

This works when SMTP ports aren't blocked - your own computer, or a paid
Render plan - but will NOT work on Render's free tier (see above).

```bash
export SMTP_USERNAME="quotes@sandyboy.co.uk"      # the Gmail address to send FROM
export SMTP_PASSWORD="xxxx xxxx xxxx xxxx"         # a Gmail "App Password" - see below
export REPORT_RECIPIENT="kaye@sandyboy.co.uk"      # where reports get sent TO
python3 app.py
```

**Getting a Gmail App Password** (takes about 2 minutes):
1. The Gmail account you send FROM must have 2-Step Verification turned on (Google Account → Security).
2. Go to https://myaccount.google.com/apppasswords, sign in, create a new app password (name it "quote portal"), and copy the 16-character code.
3. Use that code as `SMTP_PASSWORD` above (not your normal Gmail password).

If both `BREVO_API_KEY`+`EMAIL_FROM` and `SMTP_USERNAME`+`SMTP_PASSWORD` are
set, Brevo is used first; SMTP is only a fallback. If neither is set, reports
are saved to the `reports/` folder instead of being emailed, so uploads still
work while you're getting one of these set up.

## Going live on Render (free), step by step

Once you're happy with local testing, this puts the portal on the internet
with a real link, for free, in about 10-15 minutes. The code is already set
up for this (`render.yaml`, `gunicorn` in requirements.txt) - these steps are
all clicking through websites, no coding.

**1. Put the code on GitHub** (Render deploys from a git repo)
   - If you don't have a GitHub account, create one free at github.com.
   - Click the "+" in the top right → "New repository". Name it e.g. `client-portal`, keep it Private, click "Create repository".
   - On the new repo's page, use "uploading an existing file" and drag in every file and folder from the unzipped `client_portal` folder (or, if you're comfortable with git: `git init && git add . && git commit -m "initial" && git remote add origin <your repo url> && git push -u origin main`).

**2. Create a Render account**
   - Go to render.com → Sign up (you can sign up with your GitHub account, which also connects the two for you).

**3. Create the web service**
   - In the Render dashboard, click "New +" → "Web Service".
   - Choose "Build and deploy from a Git repository" and select the `client-portal` repo you just created.
   - Render should detect `render.yaml` and pre-fill everything (name, Python runtime, build command `pip install -r requirements.txt`, start command `gunicorn app:app`, free plan). If it doesn't auto-detect, enter those values manually.

**4. Add your email credentials**
   - Still on the setup screen (or afterwards under the service's "Environment" tab), add:
     - `SMTP_USERNAME` → the Gmail address you're sending from
     - `SMTP_PASSWORD` → the Gmail App Password from the section above
     - `REPORT_RECIPIENT` is already set to kaye@sandyboy.co.uk via `render.yaml` - change it there if needed.

**5. Deploy**
   - Click "Create Web Service". Render will build and start it - takes a couple of minutes the first time. Watch the logs; when it says the service is live, you're done.
   - You'll get a free URL like `https://client-portal-xxxx.onrender.com` - open it to confirm the upload page loads, and try submitting a test file to confirm the email arrives.

**6. (Optional) Use your own domain**
   - Under the service's "Settings" → "Custom Domain", add something like `quotes.sandyboy.co.uk` and follow Render's instructions to point your domain's DNS at it. HTTPS is handled automatically.

A couple of things worth knowing about the free tier: it spins the service
down after 15 minutes of no traffic and takes 30-60 seconds to wake up on the
next visit (a paid instance, ~$7/month, stays always-on if that delay ever
becomes a problem), and its disk isn't guaranteed to survive a redeploy - not
a concern for the emailed report itself, but don't rely on the `uploads/`
folder as permanent storage without adding a proper file store (S3,
Cloudflare R2) later if you need to keep originals long-term.

None of this needs new code - the app is already written to be deployed as-is.

## Project files

```
app.py          - Flask web server: upload endpoint, storage, cleanup, kicks off analysis+email
analyzer.py     - the core file-analysis logic (PDF/JPG/PPTX/XLSX/ZIP)
paper_sizes.py  - reference tables of standard page/slide sizes + matching logic
report.py       - builds the HTML email body and CSV attachment from analysis results
emailer.py      - sends the report by SMTP (falls back to saving locally if not configured)
templates/index.html - the client-facing drag & drop page
make_samples.py - generates the test files used to validate the analyzer (samples/)
samples/        - sample test files (mixed standard + unusual sizes)
uploads/        - where submitted files land (auto-deleted after 30 days)
reports/        - fallback location for reports if email isn't configured / fails
render.yaml     - one-click deployment config for Render.com (see "Going live" below)
.gitignore      - keeps uploads/reports/logs out of git when you push this to GitHub
```

## Adjusting things later

- **Retention period**: change `RETENTION_DAYS` in `app.py`.
- **Upload size cap**: change `MAX_CONTENT_LENGTH` in `app.py` (currently 500MB).
- **Size tolerance** (how close to A4 counts as "A4"): change `TOLERANCE_MM` in `paper_sizes.py`.
- **Accepted file types**: `SUPPORTED_EXTENSIONS` in `analyzer.py` and the `accept=` list in `templates/index.html`.
