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

1. Client fills in their details (subject, full name, company, email, phone, deadline, delivery address - all required) and drags files onto the page, then hits submit.
2. Files are saved to the server and the client immediately sees a "received" confirmation.
3. In the background, each file is analysed:
   - **PDF** - page count + physical size of every page (in mm), compared against A0-A6, US Letter/Legal, Tabloid, the roller-banner size bands, and the 210/297 square formats (see "Notable sizes" below).
   - **JPG/PNG** - pixel dimensions converted to an estimated print size using the image's DPI metadata; if no DPI tag exists (common with phone photos/scans), 300 DPI is assumed and this is flagged.
   - **PPTX** - slide count + the deck's slide size, compared against standard 4:3/16:9/A4-slide formats.
   - **XLSX** - for each sheet: if a print/paper size was explicitly set in the file, that's used (reliable); if not, the physical size of the used cell range is *estimated* from column widths/row heights and flagged for a manual check, since Excel doesn't reliably store a true "page size" otherwise.
   - **ZIP** - unpacked recursively (including nested zips and subfolders) and every file inside is analysed and listed individually against the archive it came from.
4. A report (HTML email body + CSV attachment) is generated listing every item, its size, whether it's flagged, and the client's contact details, and emailed to you. Small original files are attached too; anything too large for email is left on the server for you to retrieve.
5. Submission folders (uploads + their reports) are automatically deleted after 30 days.

### Notable (recognised, non-flagged) sizes

On top of the standard page sizes, a few business-specific sizes are recognised by name rather than being flagged as "non-standard":

- **Roller banners**: matched by a size range rather than one exact measurement, orientation-agnostic (either side can be the "short" one) -
  - 800-850mm x 2000-2200mm -> labelled **"800 RB"**
  - 1000-1150mm x 2000-2200mm -> labelled **"1000 RB"**
  - 1200-1300mm x 2000-2200mm -> labelled **"1200 RB"**
  - 1500-1600mm x 2000-2200mm -> labelled **"1500 RB"**
  - 2000-2200mm x 2000-2200mm -> labelled **"2000 RB"**
- **Squares**: 210mm x 210mm -> **"210 square"**, 297mm x 297mm -> **"297 square"**.
- **US Legal pages are grouped and labelled as "A4"** in reports (the measured dimensions are unchanged - only the display label/grouping changes, since these are treated the same for quoting).

All of this lives in `paper_sizes.py` (`ROLLER_BANNER_BANDS`, `SQUARE_SIZES`, `LABEL_ALIASES`) if you need to add more later.

### "I just want to count my pages"

Ticking this box on the form skips the normal quote pipeline entirely: nothing is emailed to you and nothing is uploaded to WeTransfer/TransferNow. Instead, the files are analysed on the spot, a report opens directly in the client's browser (a new tab), and the files are deleted from the server immediately afterwards rather than being kept for 30 days. Subject, full name, company, email and phone are still required in this mode; deadline and delivery address are not (since nothing is actually being sent to you to action).

### Reference numbers

Every report/email now shows a short reference number in the format **DDMMYY-NNNNN** (e.g. `160926-00001`), instead of the long technical submission ID. It's shown to you in the report heading and email subject, and to the client too, in their on-screen confirmation message once their files are received.

This number is generated from a simple counter (`submission_counter.txt`, created automatically next to the app) that just keeps going up - it does **not** reset daily, so the next submission after `160926-00001` is `160926-00002`, `160926-00003`, and so on, whatever the date.

One honest caveat: that counter file lives on Render's free-tier disk, which is **not guaranteed to persist** - a redeploy, or the app spinning back up after the free tier puts it to sleep from inactivity, can occasionally reset it back to 1. If that happens, the numbers just start again from `160926-00001` at some point - it's a cosmetic reset only. It can't cause any files or reports to be lost, because the reference number is never used to name or locate anything on the server (the actual storage folder for each submission still uses its own separate, always-unique internal ID behind the scenes). If this ever bothers you in practice, the fix is a paid Render instance with a persistent disk - let me know and I can help set that up.

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
   To send to more than one inbox, separate them with a comma in that same variable, e.g.
   `REPORT_RECIPIENT="kaye@sandyboy.co.uk,sales@iprintmanage.com"` (spaces around each address are fine too) -
   every address in the list gets the report.
5. Restart/redeploy. Submit a test file and check the inbox.

### Method B: Gmail SMTP (use this only when running locally, or on a paid instance)

This works when SMTP ports aren't blocked - your own computer, or a paid
Render plan - but will NOT work on Render's free tier (see above).

```bash
export SMTP_USERNAME="quotes@sandyboy.co.uk"      # the Gmail address to send FROM
export SMTP_PASSWORD="xxxx xxxx xxxx xxxx"         # a Gmail "App Password" - see below
export REPORT_RECIPIENT="kaye@sandyboy.co.uk"      # where reports get sent TO - comma-separate for more than one
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

## Getting the original files (a download link in the report)

By default, small submissions get their original files attached directly to
the report email (up to 20MB combined); anything larger is just left on the
server with no easy way to get at it. Setting this up instead uploads every
submission's original files to a transfer service and puts a "Download
original files" link right in the report email - no attachment size limit
to worry about, and one click to get everything.

Two providers are supported - `file_transfer.py` picks between them
automatically based on which API key is set, so **switching later is just
changing an environment variable on Render, no code changes needed.**

### Option A: TransferNow (works right now)

Free for 14 days / 100GB to try properly; after that it's pay-as-you-go
(roughly $0.20/GB/month) rather than free forever - for occasional
quoting-sized batches that'll likely be a very small monthly amount, but
worth knowing going in.

1. Create a free account at https://developers.transfernow.net/en/register (self-serve, no credit card for the trial).
2. Find your API key in their dashboard.
3. Set it as an environment variable (Render: Environment tab → Add variable; locally: `export` before running):
   ```bash
   export TRANSFERNOW_API_KEY="your-api-key-here"
   ```
4. Restart/redeploy and submit a test file - the report email should show a blue "Download original files" button.

### Option B: WeTransfer (for your existing paid account, once reachable)

Since you already have a paid WeTransfer account, this is the intended
long-term home - paid accounts typically get longer file retention than the
free plan's 3-day expiry. Their developer signup portal was returning a 503
error as of building this, so this is ready to switch to whenever it's back:

1. Go to https://developers.wetransfer.com/sign-up (try again in a day or two if it's still down) and get your API key from your paid account.
2. Set `WETRANSFER_API_KEY` the same way as above.
3. That's it - `file_transfer.py` prefers WeTransfer over TransferNow whenever `WETRANSFER_API_KEY` is set, so you can leave `TRANSFERNOW_API_KEY` in place or remove it, either works.

If neither key is set, this is skipped entirely and the app just falls back
to attaching what it can directly to the email, exactly as before.

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
     - `REPORT_RECIPIENT` is already set to kaye@sandyboy.co.uk via `render.yaml` - change it there if needed. To add more recipients, edit that same value directly in the Environment tab to a comma-separated list, e.g. `kaye@sandyboy.co.uk,sales@iprintmanage.com` - no code change needed, just save and it redeploys.

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

## Large uploads keep failing ("Upload failed - please check your connection")

Two different things can cause this, and it's worth knowing both since they were tackled in order:

1. **gunicorn's default 30-second request timeout.** gunicorn (the production server this app runs under on Render) assumes any request that takes longer than that to finish has hung, and kills it - which looks to the browser like a dropped connection. Fixed permanently in `render.yaml`'s `startCommand`, which raises that to 1 hour and runs 2 workers instead of 1 (`gunicorn app:app --workers 2 --timeout 3600`). Worth checking **Settings -> Start Command** in the Render dashboard matches this after deploying - `render.yaml` changes only auto-apply if Blueprint auto-sync is on for your service, otherwise paste it in there by hand.

2. **Render sits behind Cloudflare, which enforces its own hard upload size limit** (commonly around 100MB) completely separately from anything Render or this app controls - a request over that size gets rejected right at the door, before it ever reaches Render's servers, gunicorn, or this app's code. This is the one that was actually causing multi-hundred-MB/GB uploads to fail instantly, with nothing at all showing up in Render's own logs (the request never got that far). No app-level setting fixes this - see the next section for the actual workaround.

## Large files: Google Drive bypass

Because of the Cloudflare limit above, this app can send big files a different way: straight from the client's browser to a Google Drive folder (bypassing Render/Cloudflare entirely for the actual file bytes), and then pull them back down server-side just to run the page-count analysis. WeTransfer/TransferNow (see "Getting the original files" above) is unchanged and still does the actual "here's a link to download the originals" part of the report - Drive is only ever a temporary relay to get past the edge limit, and the Drive copies are deleted again once a submission finishes processing.

**Until this is set up, uploads fall back automatically to the old direct route** (still subject to the Cloudflare limit for big files, but fine for smaller ones) - so there's no rush, and nothing breaks in the meantime.

### One-time setup

This needs a real Google account with actual storage behind it - **not** a bare "service account" (that has zero storage of its own unless backed by a paid Workspace domain-wide delegation setup, which is its own can of worms). Since sandyboy.co.uk is on Google Workspace, authorizing this app against your own Workspace account is the way to go, and - because it's "Internal" to your own organisation - Google won't require the app to go through their public verification review.

1. **Create a Google Cloud project** (free): go to https://console.cloud.google.com, and create a new project (top-left project picker -> New Project). Any name is fine, e.g. "Client Portal".
2. **Enable the Drive API**: in that project, go to **APIs & Services -> Library**, search for "Google Drive API", and click **Enable**.
3. **Configure the OAuth consent screen**: **APIs & Services -> OAuth consent screen**. Choose **Internal** as the User Type (this is what skips Google's verification review - it's only available because your domain is on Workspace). Fill in an app name (e.g. "Client Portal") and your email, save.
4. **Create OAuth credentials**: **APIs & Services -> Credentials -> Create Credentials -> OAuth client ID**. Application type: **Web application**. Under **Authorized redirect URIs**, add:
   ```
   https://client-portal-2.onrender.com/admin/gdrive-callback
   ```
   (swap in your actual Render URL if it's different). Save, then copy the **Client ID** and **Client Secret** it gives you.
5. **Set environment variables** in Render's Environment tab:
   ```
   GOOGLE_CLIENT_ID=<the client ID from step 4>
   GOOGLE_CLIENT_SECRET=<the client secret from step 4>
   GDRIVE_ADMIN_KEY=<make up any random password - this just stops strangers from finding the setup page>
   ```
   Save and let it redeploy.
6. **Authorize the app against your Drive**: once redeployed, visit (in your own browser, signed into your Workspace Google account):
   ```
   https://client-portal-2.onrender.com/admin/gdrive-auth?key=<the GDRIVE_ADMIN_KEY you just set>
   ```
   Google will show its normal consent screen ("Client Portal wants access to...") - approve it. You'll land on a page showing a long **refresh token** value.
7. **Copy that refresh token** into Render's Environment tab as one more variable:
   ```
   GOOGLE_REFRESH_TOKEN=<the value shown on that page>
   ```
   Save. Once this redeploys, large uploads switch over to the Drive bypass automatically - nothing else to change, and no client-facing difference except that big files now actually work.

**That refresh token is a credential** - anyone who has it can act as this Drive connection. Don't paste it anywhere other than Render's Environment tab.

### Storage/cleanup

Files normally only sit in Drive briefly. For a quote request, the client is told "received" as soon as their upload reaches Drive; the app then pulls the files down in the background, analyses them, creates the TransferNow/WeTransfer link and emails you. The Drive copies are deleted only once that download link exists. For "just count my pages", they're deleted as soon as the pages are counted.

**If something goes wrong after the client has been told "received"** (Google can't be reached to pull the files back, or TransferNow/WeTransfer fails), the files are **not** deleted. The folder is renamed **"NEEDS ATTENTION - quote ref …"** in your Google Drive, and the email you get says so. Grab the files from there, then delete the folder yourself once you're done.

Folders from uploads a client started but never finished (closed the tab, lost connection) are removed automatically after 24 hours. "NEEDS ATTENTION" folders are never touched by that cleanup.

### What the client sees while uploading

"Preparing your upload..." (animated bar), then "Uploading - 2 of 5 files done - 120 MB of 480 MB (25%)" with a filling bar, then "Upload complete - finishing up..." (or "counting your pages...") until it's done. Up to 3 files upload at once. If they try to close the tab mid-upload, the browser asks them to confirm.

## Project files

```
app.py          - Flask web server: both upload routes, storage, cleanup, kicks off analysis+email
gdrive_upload.py - Google Drive bypass for large files (see "Large files: Google Drive bypass")
analyzer.py     - the core file-analysis logic (PDF/JPG/PPTX/XLSX/ZIP)
paper_sizes.py  - reference tables of standard page/slide sizes + matching logic
report.py       - builds the HTML email body and CSV attachment from analysis results
reference_number.py - generates the short DDMMYY-NNNNN reference shown in reports/emails
emailer.py      - sends the report via Brevo or SMTP (falls back to saving locally if neither configured)
file_transfer.py     - picks WeTransfer or TransferNow for uploading originals, based on which API key is set
wetransfer_upload.py - WeTransfer provider (for your paid account, once reachable)
transfernow_upload.py - TransferNow provider (works now, free 14-day trial then pay-as-you-go)
templates/index.html - the client-facing drag & drop page
templates/gdrive_success.html - one-time page shown after authorizing Google Drive, displaying the refresh token to copy
make_samples.py - generates the test files used to validate the analyzer (samples/)
samples/        - sample test files (mixed standard + unusual sizes)
uploads/        - where submitted files land (auto-deleted after 30 days)
reports/        - fallback location for reports if email isn't configured / fails
render.yaml     - one-click deployment config for Render.com (see "Going live" below)
.gitignore      - keeps uploads/reports/logs out of git when you push this to GitHub
```

## Adjusting things later

- **Retention period**: change `RETENTION_DAYS` in `app.py`.
- **Upload size cap**: change `MAX_CONTENT_LENGTH` in `app.py` (currently 5GB) and `MAX_TOTAL_BYTES` in `templates/index.html` to match. This is this app's own cap - it's separate from (and smaller than) the Cloudflare edge limit that large uploads actually run into; see "Large files: Google Drive bypass" above for what actually makes big uploads work reliably.
- **Size tolerance** (how close to A4 counts as "A4"): change `TOLERANCE_MM` in `paper_sizes.py`.
- **Notable sizes** (roller banners, squares, Legal->A4 grouping): `ROLLER_BANNER_BANDS`, `SQUARE_SIZES`, `LABEL_ALIASES` in `paper_sizes.py`.
- **Required form fields**: `REQUIRED_FIELDS_ALWAYS` / `REQUIRED_FIELDS_FULL_SUBMISSION` in `app.py`, and the matching inputs in `templates/index.html`.
- **Reference number format**: `next_reference_number()` in `reference_number.py` (currently DDMMYY-NNNNN, e.g. 160926-00001).
- **File types**: clients can upload any file type, and whole folders (dragged in, or via "Choose a whole folder instead"), including subfolders. Files from inside a folder keep their folder path in the name (e.g. `Transport - Drawings - plan.pdf`), and system clutter like `.DS_Store`/`Thumbs.db` is skipped. Page counts/sizes are worked out for the types in `SUPPORTED_EXTENSIONS` in `analyzer.py` (PDF, JPG, PNG, ZIP, PPTX, XLSX); anything else (e.g. `.msg` emails, `.ai`, `.indd`) is still sent to you in the download link and listed in the report as "check it manually".
