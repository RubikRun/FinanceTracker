# Finance Tracker

A small, cross-platform (Windows + macOS) desktop app for tracking Boris and
Milena's household income and spending, built with Python and Qt (PySide6).
Data lives in a single hand-editable JSON file — no database.

## What it does

- Switch between **Boris**, **Milena**, and **Boris & Milena** with tabs at the top.
- In a personal tab, add **income** and **spending** (auto-assigned to that person).
- In **Boris & Milena**, view both people's records together (totals combined; no adding).
- Pick a **month** to view (the dropdown only lists months that have records for the
  active tab).
- See, for the selected month:
  - a **Spending** table (date, category, amount, note; plus **Who** in the combined view),
  - an **Income** table (date, amount, note; plus **Who** in the combined view),
  - totals: **Earned**, **Spent**, and the **Gap** (earned − spent; green when
    positive, red when negative).
- Open a **Spending chart** for a month: a pie chart by category, with Boris's
  and Milena's amounts under each category. The chart can change person and
  month on its own, without changing the main window.

Amounts are shown in euro (€). Dates are shown as `DD/MM/YYYY` and stored as
`YYYY-MM-DD` in the JSON file.

## Project layout

```
FinanceTracker/
├── finance/              # UI-independent core API (no Qt here)
│   ├── config.py         #   categories, currency, date formats, file path
│   ├── crypto.py         #   snapshot envelope; not used by the GUI yet
│   ├── profile.py        #   per-user config directory; cloud mode stays off
│   ├── google_auth.py    #   Google sign-in; refresh token in the OS keyring
│   ├── drive_folder.py   #   folder link parsing and shortcut resolution
│   ├── google_setup.py   #   `python -m finance.google_setup`; uploads nothing
│   ├── drive_adapter.py  #   Drive folder list/download/upload/trash
│   ├── drive_check.py    #   `python -m finance.drive_check`; synthetic files only
│   ├── models.py         #   SpendingRecord, IncomeRecord
│   └── store.py          #   FinanceStore: load/save JSON + all queries
├── ui/                   # Qt (PySide6) UI — a thin client over `finance`
│   ├── add_dialog.py     #   modal add/edit dialog
│   ├── main_window.py    #   main window: period selector, summary, tables
│   └── spending_chart_dialog.py  # category pie chart and legend
├── main.py               # launches the GUI
├── cli.py                # tiny command-line client (proves the API is reusable)
├── lib_requirements.txt  # Python dependencies (PySide6, cryptography, Google API, keyring)
├── requirements-dev.txt  # development dependencies (pytest)
├── tests/                # finance-core tests; synthetic data only
└── finance_data.json     # created on first save, next to the program; not in Git
```

The UI and the CLI both talk only to `finance.FinanceStore`, never to the JSON
file directly. That keeps the data logic in one place and makes it easy to
script custom queries or build other front-ends later.

## Setup

You need **Python 3.9+**.

It's recommended to use a virtual environment.

### Windows (PowerShell)

```powershell
cd C:\dev\FinanceTracker
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r lib_requirements.txt
```

### macOS (Terminal)

```bash
cd /path/to/FinanceTracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r lib_requirements.txt
```

## Run the app

```bash
python main.py
```

## Run the tests

From the project root, with the virtual environment activated:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests exercise the finance core, snapshot encryption, local config paths, Google sign-in, and the Drive adapter with fake credentials and a fake Drive service. They do not read or write `finance_data.json`, and they do not contact Google.

## Google sign-in setup

This prepares a Google sign-in and remembers one shared Drive folder. It does **not** upload finance data, and the FinanceTracker window still uses the local JSON file. Cloud sync is not on. Do the sign-in checklist below for **both** Google accounts before the shared-folder check that follows it.

Use a dry-run profile so this experiment does not become the production config. Set this in the same terminal session before the setup command, including when you run it a second time.

Windows PowerShell:

```powershell
$env:FINANCETRACKER_CONFIG_DIR = "$env:LOCALAPPDATA\FinanceTracker-dry-run"
```

macOS Terminal:

```bash
export FINANCETRACKER_CONFIG_DIR="$HOME/Library/Application Support/FinanceTracker-dry-run"
```

Do not point that variable at the FinanceTracker source directory.

### 1. Google Cloud project

Both computers use **one** Google Cloud project and **the same** desktop client JSON. Do not create a second client ID.

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create a project (any name is fine).
2. Enable the Google Drive API: [Drive API library page](https://console.cloud.google.com/apis/library/drive.googleapis.com) → Enable.
3. Open [Google Auth platform → Branding / Overview](https://console.cloud.google.com/auth/overview) (older consoles call this **APIs & Services → OAuth consent screen**).
4. User type: **External**. This is required for two personal Gmail accounts. Internal is only for a Google Workspace organization.
5. Fill in the app name, a user support email, and a developer contact email. These are shown on the consent screen.
6. Open Data access / Scopes and add `https://www.googleapis.com/auth/drive`. The description is "See, edit, create, and delete all of your Google Drive files." Do **not** choose the narrower "only the specific Google Drive files you use with this app" scope (`drive.file`).
7. Open [Clients](https://console.cloud.google.com/auth/clients) (older consoles: **APIs & Services → Credentials → Create credentials → OAuth client ID**). Create a client of type **Desktop app**. Download the JSON once.
8. Open [Audience](https://console.cloud.google.com/auth/audience) and **Publish app**. Confirm the publishing status is **In production**. Do not submit the app for Google verification. An unverified production app is allowed for personal use; Google shows a warning and caps it at 100 users, which is fine for two people.

Publishing status **Testing** makes refresh tokens die after 7 days, so both of you would have to sign in again every week. In production they do not. After you finish sign-in, look again after more than 7 days: the setup command should reuse the saved sign-in. If Google asks you to consent again, the app is still in Testing.

### Why this is the full Drive scope

`drive.file` only lets the app see files that this installation created, and only for the Google account that created them. The other person's app cannot see those files, even inside a folder you both share. Choosing the folder in Google's file picker grants access to the folder itself, not to files the other account adds later. Every save will be a new file, so that scope cannot work. The app uses the full Drive scope. Later steps restrict every Drive call in code to the one folder you confirm here. The token itself can still reach the rest of that Google account's Drive; that is the accepted tradeoff.

Google Advanced Protection, or a Workspace-managed account, can block an unverified app and hide the "Advanced" link below. If that happens, stop and say so. Do not continue to the shared-folder Drive tests.

### 2. Shared test folder

1. In the Drive website, with the account that will own the folder: **New → Folder**. Name it something obvious, such as `FinanceTracker test`. Leave it empty.
2. **Share** it with the other Google account as **Editor**.
3. Copy the folder link. Both computers paste that same link.

Use this empty test folder for now. This step only reads the folder's name and owner. It does not add a file.

### 3. Client file and sign-in

Install the new dependencies, then run setup. The first run prints the exact path of the missing client file. Copy the downloaded JSON to that path (the same file on both computers) and run the command again.

Windows PowerShell, from the project directory, with the dry-run variable still set:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r lib_requirements.txt
.\.venv\Scripts\python.exe -m finance.google_setup
```

macOS:

```bash
source .venv/bin/activate
pip install -r lib_requirements.txt
python -m finance.google_setup
```

The command opens a browser. Sign in with **this** computer's Google account (Boris on the Windows PC, Milena on the Mac). Because the app is unverified, Google shows **Google hasn't verified this app**. On Chrome and on Safari:

1. Click **Advanced**. On Safari this is a small link at the lower left of the warning, not a button at the top.
2. Click **Go to … (unsafe)**. The name matches the app name you entered in Google Cloud.
3. Review the Drive permission and click **Continue** or **Allow**.
4. The browser says FinanceTracker sign-in is complete. You can close that tab.

Back in the terminal, paste the folder link. The command prints the folder name, the owner, and the folder id. It saves the id only after you answer `y`. If the link was a shortcut, it follows the shortcut and shows the real folder.

The refresh token is stored in the operating-system credential store (Windows Credential Manager or macOS Keychain), under a name starting with `FinanceTracker-profile-` when the dry-run variable is set. There is no `token.json`. The finance passphrase is not asked for and is not stored.

### 4. What to check

- Run the same command again in the same terminal (the dry-run variable must still be set). Expected: `Reused the saved Google sign-in. No browser window was opened.` Press Enter to keep the folder, then confirm it.
- The dry-run folder has `config.json` with `"cloud_mode": false` and no finance records. It has no `token.json`.
- `git status` in the project does not show the client JSON, a token file, or `config.json`.
- The shared Drive folder has no new file.
- `python main.py` still opens the local app and still saves to `finance_data.json`.
- In Google Cloud, Audience / the consent screen still says user type **External** and publishing status **In production**.
- More than 7 days later, run the setup command again. Expected: it still reuses the saved sign-in. A new consent screen means the app is still in Testing.

If either Google account cannot get past the unverified-app warning, stop. The shared-folder check below should not start on a sign-in that did not work.

## Shared-folder check

This is the check that both Google accounts can use one shared Drive folder. It uploads a few small synthetic files. It does not read the finance file, it does not ask for the finance passphrase, and it does not turn cloud sync on.

Use the same dry-run profile as the sign-in above, still pointed at the empty test folder. Each account needs its own profile: the Windows PC and the Mac each have one, or, on one PC, use a second directory such as `FinanceTracker-dry-run-b` and run `python -m finance.google_setup` there with the other Google account and the same folder link.

The command refuses to run when `FINANCETRACKER_CONFIG_DIR` is unset, and it refuses a profile inside the FinanceTracker source directory.

From the project directory, with the virtual environment active and the dry-run variable still set:

```powershell
.\.venv\Scripts\python.exe -m finance.drive_check create
```

```bash
python -m finance.drive_check partner SNAPSHOT_ID LOCK_ID
python -m finance.drive_check finish SNAPSHOT_ID LOCK_ID
python -m finance.drive_check ping
```

`create` prints the folder name and asks you to type that name. Type it only when it is the empty test folder. If the name is anything else, press Enter without typing it. The command then stops and uploads nothing.

`create` adds `finance_snapshot_g000001_….enc` and `finance_lock.json`. The bytes are markers such as `marker=snapshot-a`. They are not finance records. It prints both file ids.

On the other account, `partner` with those two ids lists the files, downloads the snapshot, changes both files' contents, and tries to trash the snapshot. The trash line should be `not owner — skipped`, and the snapshot should still be in the folder.

Back on the account that ran `create`, `finish` with the same ids reads the updated lock by file id and moves both files to that account's Drive trash. They are not permanently deleted. In the Drive website, the test folder should be empty, both files should be in that account's trash, and nothing outside the test folder should have changed. Leave the folder empty.

`ping` reads the folder and Drive's clock. With the network disconnected first, it should fail within 30 seconds with a message that Drive could not be reached or did not answer. It should not hang. While the network is connected, it prints the folder name and a Drive time in UTC.

`create` always reads the new snapshot back with `files.get`. If the folder listing is slow, it also says the listing had not caught up and the snapshot is still treated as saved. Either sentence is a successful read-back.

If sign-in, sharing, listing, content updates, or read-back by file id fails for either account, stop. Do not continue to snapshot history, the lock, or the finance window on top of that.

## Use the CLI (optional)

The CLI reads/writes the same `finance_data.json`:

```bash
python cli.py add-spending Boris 14.20 Groceries 2026-06-11 --note "toilet paper"
python cli.py add-income Milena 3560 2026-06-03
python cli.py summary 2026 6
python cli.py summary 2026 6 --user Boris
python cli.py list 2026 6 --user Milena
```

## The data file

`finance_data.json` is created next to the program the first time you save a
record. It is plain JSON and safe to edit by hand. Example:

```json
{
  "version": 2,
  "next_id": 3,
  "spending": [
    { "id": 1, "amount": 14.2, "category": "Groceries", "date": "2026-06-11", "date_recorded": "2026-06-11", "user": "Boris", "note": "toilet paper" }
  ],
  "income": [
    { "id": 2, "amount": 3560, "date": "2026-06-03", "date_recorded": "2026-06-13", "user": "Milena", "note": "" }
  ]
}
```

IDs are a single shared sequence across both income and spending. Every record
has a `user` field (`Boris` or `Milena`).

The file path is `default_data_path()` in `finance/config.py`. Keep the app
closed on one computer before opening it on the other: each save replaces the
whole file, so overlapping saves drop whichever copy saves last. Sign-in to a
shared Drive folder can be prepared with `python -m finance.google_setup`
(see above). That command does not upload finance data. `python -m finance.drive_check`
uploads synthetic marker files into the test folder only when you run it.
The window still saves the local JSON file, and cloud sync is not on yet.

## Changing categories, currency or people

Edit `finance/config.py`:

- `CATEGORIES` — the fixed list shown in the spending category dropdown.
- `CURRENCY_SYMBOL` — the symbol shown next to amounts.
- `USERS` — the two people (currently Boris and Milena).
