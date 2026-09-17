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

Ticking this box on the form skips the normal quote pipeline entirely: nothing is emailed to you and nothing is uploaded to WeTransfer/TransferNow. Instead, the files are analysed and a report opens directly in the client's browser (a new tab); the files are deleted straight afterwards rather than being kept. Subject, full name, company, email and phone are still required in this mode; deadline and delivery address are not (since nothing is actually being sent to you to action).

### Reference numbers

Every report/email now shows a short reference number in the format **DDMMYY-NNNNN** (e.g. `160926-00001`), instead of the long technical submission ID. It's shown to you in the report heading and email subject, and to the client too, in their on-screen confirmation message once their files are received.

This number is generated from a simple counter (`submission_counter.txt`, created automatically next to the app) that just keeps going up - it does **not** reset daily, so the next submission after `160926-00001` is `160926-00002`, `160926-00003`, and so on, whatever the date.

Render wipes the server's disk on every redeploy, so once Cloudflare R2 is set up (see "Large files: Cloudflare R2"), a copy of the counter is also kept in R2 and numbering carries on after a redeploy instead of restarting at 00001. Without R2 it can still restart occasionally; that's cosmetic only, since reference numbers are never used to name or find files.

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

## Large files: Cloudflare R2

Because of the Cloudflare limit above, big uploads can't come straight to this app. Instead, the client's browser sends each file directly to **Cloudflare R2** (online file storage), and this app then pulls the files down in the background to count pages, sends them on via TransferNow/WeTransfer, and emails you the report. R2 is only a stopover: files are deleted from it once they've safely reached you.

(An earlier version used Google Drive for this. Google blocks bursts of requests from shared cloud-server addresses like Render's as "automated queries", which broke jobs with hundreds of files, so it was replaced. The Google settings in Render - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` - are no longer used and can be deleted.)

**Until R2 is set up, uploads fall back automatically to the direct route** (fine for smaller files; big ones still hit the Cloudflare limit).

### One-time setup (about 15 minutes)

1. **Create a Cloudflare account** at https://dash.cloudflare.com/sign-up (free). You don't need to move your website or domain to Cloudflare for this.
2. **Turn on R2**: in the Cloudflare dashboard go to **Storage & databases -> R2 -> Overview** and complete the sign-up/checkout. It may ask for a card even though this portal's usage should stay inside the free monthly allowance (10 GB of storage; files only sit there briefly, and downloads are free).
3. **Create a bucket** (a storage area): click **Create bucket**, name it `portal-uploads`, leave location on Automatic, and create it.
4. **Allow uploads from your portal's web page (CORS)**: open the bucket -> **Settings** -> **CORS Policy** -> **Add CORS policy**, and paste exactly this (swap in your portal's address if it ever changes):
   ```json
   [
     {
       "AllowedOrigins": ["https://client-portal-2.onrender.com"],
       "AllowedMethods": ["PUT"],
       "AllowedHeaders": ["Content-Type"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```
   Save. Without this, browsers refuse to send the files.
5. **Create an access key**: back on the **R2 Overview** page, under **Account Details**, click **Manage** next to **API Tokens** -> **Create User API token**. Give it a name ("client portal"), choose permission **Object Read & Write**, and restrict it to the `portal-uploads` bucket. Create it, then copy the **Access Key ID** and **Secret Access Key** straight away - the secret is only shown once. The same Account Details box shows your **Account ID**.
6. **Add these in Render -> Environment**, then save (it redeploys):
   ```
   R2_ACCOUNT_ID=<your Account ID>
   R2_ACCESS_KEY_ID=<Access Key ID>
   R2_SECRET_ACCESS_KEY=<Secret Access Key>
   R2_BUCKET=portal-uploads
   ```
   Keep `GDRIVE_ADMIN_KEY` - it's now the password for the admin pages below (or add `ADMIN_KEY` with a new password instead).
7. **Check it**: visit `https://client-portal-2.onrender.com/admin/storage-check?key=<your admin password>`. It saves, reads back, lists and deletes a small test file and shows each step's result; the page should start with `"all_ok": true`.

### What happens to files

- **Quote request:** the client sees "received" as soon as their upload finishes. In the background the app downloads the files, counts pages, creates the TransferNow link and emails you. The copy in R2 is deleted only once that link exists.
- **Just count my pages:** the page checks back every couple of seconds ("Fetching your files (12 of 433)...", "Counting pages...") and shows the report when it's ready. The files are deleted from R2 straight after.
- **If something goes wrong** (TransferNow down, a download failing, the server restarting mid-job): the files are kept in R2 and flagged, and you get an email. Open **`https://client-portal-2.onrender.com/admin/attention`** (it asks for your admin password) to download the files, **Try processing again**, or **Delete from storage** once you're done.
- **Interrupted jobs** (e.g. by a redeploy) are restarted automatically within about 3 hours; after 3 failed attempts they're flagged for attention instead.
- **Abandoned uploads** (client closed the tab part-way) are deleted after 24 hours. Flagged jobs are never deleted automatically.
- **Progress:** while a quote job runs, the log and the attention page show what it's doing, e.g. "Counting pages (120 of 433)" or "Sending to TransferNow (200 of 433)", updated about every 30 seconds (the attention page refreshes itself every minute). Files go to TransferNow 4 at a time (`TRANSFERNOW_PARALLEL_FILES` changes that).

### What the client sees while uploading

Before they can submit, the client has to tick "I've checked my file list and I can see all of my files present" (it unticks itself if the list changes). A file that looks wrong gets a yellow warning in the list: a ZIP under 1 MB (usually one that was still being created or downloaded), an empty file, or an unfinished download (`.crdownload`, `.part`...). For "just count my pages", ticking the box also shows a note to allow pop-ups, and the finished page always has an **Open my report** button in case the new tab was blocked.

"Preparing your upload..." then "Uploading - 2 of 5 files done - 120 MB of 480 MB (25%)" with a filling bar, then "Upload complete - finishing up...". Up to 3 files upload at once; a file that fails part-way is retried automatically a few times before giving up. If they try to close the tab mid-upload, the browser asks them to confirm.

## What's in the report

- **Plans banner** (quote requests): "PLANS: PRINTED TO SCALE" or "PLANS: A3 FOLDED", from the tick box on the upload page. It's also on the end of the email subject ("- TO SCALE" / "- A3 FOLDED").
- **Totals, together:** pages/items, estimated sheets of paper, **tabs** (one per folder, at every level, including the outer folder the client dragged in, and a ZIP counts as a folder), **dividers** (one per document: every file, and every file inside a ZIP), and files.
- **Spreadsheets - check these:** every Excel file with its folder, worksheets, estimated pages, sizes, and whether the print size is set in the file or estimated.
- **Quantity by size**, with non-standard sizes broken down.
- **Folders:** a diagram of the folder structure with documents and pages in each folder (including subfolders). In the page-count report each folder has a "show files" toggle; the email shows folders only.
- **Full breakdown** of every page.
- In the **page-count report** (opens in the browser) each section can be folded away; the full breakdown starts folded. Emails can't fold, so the quote email shows everything.
- The CSV has the same totals, spreadsheet and folder sections.
- Folder details come from the upload page, which sends each file's real folder path. Reports from before this change can't be recounted for tabs.
- **Spreadsheet sizes:** only cells with something in them count towards a sheet's estimated size (formatting on empty cells is ignored). A sheet that still comes out bigger than 2.5 m on a side is listed as "check manually" instead of given a size.

## Submission history ("My submissions") and the admin list

Needs Cloudflare R2 (above) and email (Brevo) to be set up - nothing else to configure.

- **Clients don't need an account.** After a quote request they get a short confirmation email with their reference number and a **View my submissions** button. That opens a private page listing everything sent from that email address: reference, date, quote or page count, files, pages, quantity by size, and their copy of the report. The link lasts 30 days. Any time after that, the **My submissions** link at the top of the upload page lets them enter their email and get a fresh link by email. (A link is only ever emailed, never shown on screen, so nobody can look up someone else's submissions by typing their address.)
- **Only submissions from when this went live** are listed - earlier ones weren't recorded.
- **Each email address has its own history**, so a typo in the address files that submission under the typo. Capital letters don't matter.
- **The files themselves aren't kept** - just a few KB of details per submission, plus a copy of the report (tens of KB). A thousand submissions is roughly 50-200 MB, well inside R2's free 10 GB.
- **Your list of everything:** `https://client-portal-2.onrender.com/admin/submissions` (same admin password). Newest first, with search (reference, email, company, name, subject, phone), a quotes/page-counts filter, status, the report and the TransferNow link. It links to and from the attention page.
- **Optional email settings** (Render -> Environment): `EMAIL_FROM_NAME` is the sender name clients see (e.g. `iPrintManage`; default "Client Portal"), and `EMAIL_REPLY_TO` is where their replies go (e.g. `sales@iprintmanage.com`).
- **Good to know:** the private links are signed with `APP_SECRET` if you set one, otherwise with the R2 secret key. If you ever change whichever one is in use, links already emailed stop working (clients just request a new one). Setting `APP_SECRET` to a long random value avoids that.

## Desktop Page Counter (for your own use)

`Page Counter.zip` is a separate download: the same page counting and size checks, running entirely on your own computer. Nothing is uploaded and there's no size limit. Unzip it somewhere permanent (e.g. Documents), double-click **Page Counter.command** (Mac) or **Page Counter.bat** (Windows), and it opens in your browser. The first run takes a minute or two to set itself up. Full instructions are in its "READ ME FIRST.txt". Its source files are `desktop_app.py`, `templates/desktop.html` and the `desktop/` folder in this project, and it reuses `analyzer.py`, `paper_sizes.py` and `report.py`, so size rules stay identical to the website.

## Project files

```
app.py          - Flask web server: both upload routes, storage, cleanup, kicks off analysis+email
r2_storage.py   - Cloudflare R2 storage for large uploads (see "Large files: Cloudflare R2")
history.py      - submission records for "My submissions" and the admin list
structure.py    - documents (dividers), folders (tabs), the folder diagram and spreadsheet list
analyzer.py     - the core file-analysis logic (PDF/JPG/PPTX/XLSX/ZIP)
paper_sizes.py  - reference tables of standard page/slide sizes + matching logic
report.py       - builds the HTML email body and CSV attachment from analysis results
reference_number.py - generates the short DDMMYY-NNNNN reference shown in reports/emails
file_part.py    - streams large file parts to TransferNow/WeTransfer without loading them into memory
emailer.py      - sends the report via Brevo or SMTP (falls back to saving locally if neither configured)
file_transfer.py     - picks WeTransfer or TransferNow for uploading originals, based on which API key is set
wetransfer_upload.py - WeTransfer provider (for your paid account, once reachable)
transfernow_upload.py - TransferNow provider (works now, free 14-day trial then pay-as-you-go)
templates/index.html - the client-facing drag & drop page
templates/my_submissions.html - the client's private submission history page
templates/admin_*.html - admin pages: login, all submissions, uploads needing attention, file downloads
desktop_app.py, templates/desktop.html, desktop/ - the desktop Page Counter
make_samples.py - generates the test files used to validate the analyzer (samples/)
samples/        - sample test files (mixed standard + unusual sizes)
uploads/        - where submitted files land (auto-deleted after 30 days)
reports/        - fallback location for reports if email isn't configured / fails
render.yaml     - one-click deployment config for Render.com (see "Going live" below)
.gitignore      - keeps uploads/reports/logs out of git when you push this to GitHub
```

## Adjusting things later

- **Retention period**: change `RETENTION_DAYS` in `app.py`.
- **Upload size cap**: change `MAX_CONTENT_LENGTH` in `app.py` (currently 5GB) and `MAX_TOTAL_BYTES` in `templates/index.html` to match. This is this app's own cap - it's separate from (and smaller than) the Cloudflare edge limit that large uploads actually run into; see "Large files: Cloudflare R2" above for what actually makes big uploads work reliably.
- **Memory / Render plan**: files are read without loading them whole into memory (PDFs, PowerPoint and Excel are read piece by piece, and parts sent to TransferNow are streamed from disk). A rehearsal job of 406 files / 909 MB, including a 254 MB PDF, a 152 MB PowerPoint and a 150,000-row spreadsheet, peaked at 66 MB, so the 512 MB Starter plan is enough. (Before this, a single large spreadsheet could use 670 MB and crash the server.)
- **Size tolerance** (how close to A4 counts as "A4"): change `TOLERANCE_MM` in `paper_sizes.py`.
- **Notable sizes** (roller banners, squares, Legal->A4 grouping): `ROLLER_BANNER_BANDS`, `SQUARE_SIZES`, `LABEL_ALIASES` in `paper_sizes.py`.
- **Required form fields**: `REQUIRED_FIELDS_ALWAYS` / `REQUIRED_FIELDS_FULL_SUBMISSION` in `app.py`, and the matching inputs in `templates/index.html`.
- **Reference number format**: `next_reference_number()` in `reference_number.py` (currently DDMMYY-NNNNN, e.g. 160926-00001).
- **File types**: clients can upload any file type, and whole folders (dragged in, or via "Choose a whole folder instead"), including subfolders. Files from inside a folder keep their folder path in the name (e.g. `Transport - Drawings - plan.pdf`), and system clutter like `.DS_Store`/`Thumbs.db` is skipped. Page counts/sizes are worked out for the types in `SUPPORTED_EXTENSIONS` in `analyzer.py` (PDF, JPG, PNG, ZIP, PPTX, XLSX); anything else (e.g. `.msg` emails, `.ai`, `.indd`) is still sent to you in the download link and listed in the report as "check it manually".
