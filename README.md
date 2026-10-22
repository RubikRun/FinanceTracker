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

The tests exercise the finance core, snapshot encryption, local config paths, and Google sign-in with fake credentials. They do not read or write `finance_data.json`, and they do not contact Google.

## Google sign-in setup

This prepares a Google sign-in and remembers one shared Drive folder. It does **not** upload finance data, and the FinanceTracker window still uses the local JSON file. Cloud sync is a later step. Do not start the later Drive tests until the checklist below has worked for **both** Google accounts.

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

If either Google account cannot get past the unverified-app warning, stop. The shared-folder Drive tests should not start on a sign-in that did not work.

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
(see above). That command does not upload finance data. The window still
saves the local JSON file, and cloud sync is not on yet.

## Changing categories, currency or people

Edit `finance/config.py`:

- `CATEGORIES` — the fixed list shown in the spending category dropdown.
- `CURRENCY_SYMBOL` — the symbol shown next to amounts.
- `USERS` — the two people (currently Boris and Milena).
