# Finance Tracker

A Python and Qt (PySide6) desktop app for Boris and Milena's household income and spending on Windows and macOS. The person tabs filter records and assign ownership to new entries. Secure Sync uses each person's Google account for Drive access and one shared finance passphrase for encryption.

## Secure Sync rollout

`master` is the local-file version. The desktop sync implementation is on `secure-sync`. The branch reached Step 11 of the [GUI plan](planning/FinanceTracker_SecureSync_ImplementationPlan.md). The remaining acceptance work is to verify the shared folder and the full synthetic workflow on Windows and macOS with both Google accounts, then import the real file once on Windows and open the same dataset on the Mac.

1. Use a separate checkout, a dedicated test Drive folder and a dry-run profile on each computer. Follow [Google sign-in setup](#google-sign-in-setup) and the [shared-folder check](#shared-folder-check).
2. Check [snapshot history](#snapshot-history-check) and [lock behavior](#lock-check) with synthetic data. Use the [synthetic session check](#synthetic-session-check) for a complete rehearsal.
3. Follow [real migration](#real-migration) with a fresh production Drive folder after the rehearsal passes.

The [CLI sync plan](planning/FinanceTracker_SecureSync_CLI_ImplementationPlan.md) starts after GUI acceptance. Use the desktop app for the rollout.

## What it does

- Switch between **Boris**, **Milena**, and **Boris & Milena** with tabs at the top.
- In a personal tab, add **income** and **spending** (auto-assigned to that person).
- In **Boris & Milena**, view both people's records together (totals combined; no adding).
- Pick a **month** to view (the dropdown only lists months that have records for the active tab).
- See, for the selected month:
  - a **Spending** table (date, category, amount, note; plus **Who** in the combined view),
  - an **Income** table (date, amount, note; plus **Who** in the combined view),
  - totals: **Earned**, **Spent**, and the **Gap** (earned − spent; green when positive, red when negative).
- Open a **Spending chart** for a month: a pie chart by category, with Boris's and Milena's amounts under each category. The chart can change person and month on its own, without changing the main window.
- Once a Drive folder is in use, each add, edit, and delete uploads one new encrypted snapshot. **Backups** lists older snapshots and can restore one. The status line shows whether the window is saved, saving, read-only, paused, or offline.

Amounts are shown in euro (€). Dates are shown as `DD/MM/YYYY` and stored as `YYYY-MM-DD`.

## Project layout

```
FinanceTracker/
├── finance/              # UI-independent core (no Qt here)
│   ├── config.py         #   categories, currency, date formats, local file path
│   ├── crypto.py         #   AES-256-GCM snapshot envelope
│   ├── profile.py        #   per-user config directory and cloud-mode flag
│   ├── google_auth.py    #   Google sign-in; refresh token in the OS keyring
│   ├── drive_folder.py   #   folder link parsing and shortcut resolution
│   ├── google_setup.py   #   `python -m finance.google_setup`; uploads nothing
│   ├── drive_adapter.py  #   one Drive folder: list, download, upload, trash
│   ├── drive_check.py    #   `python -m finance.drive_check`; synthetic files only
│   ├── snapshots.py      #   encrypted snapshots, generations, retention
│   ├── snapshot_check.py #   `python -m finance.snapshot_check`; synthetic snapshots only
│   ├── lock.py           #   Drive lock, heartbeat, idle release
│   ├── lock_check.py     #   `python -m finance.lock_check`; dry-run folder only
│   ├── session.py        #   passphrase, lock, snapshots, migration, restore
│   ├── session_check.py  #   `python -m finance.session_check`; dry-run profile only
│   ├── models.py         #   SpendingRecord, IncomeRecord
│   └── store.py          #   FinanceStore: records, queries, in-memory JSON
├── ui/                   # Qt widgets. Cloud dialogs call finance.session only
│   ├── add_dialog.py
│   ├── main_window.py
│   ├── spending_chart_dialog.py
│   ├── recovery_dialog.py
│   └── cloud_*.py        #   startup, prompts, status, and the session host
├── main.py               # launches the GUI
├── cli.py                # local-file commands; refuses to run once cloud mode is on
├── lib_requirements.txt  # runtime dependencies
├── tests/                # synthetic data only; no network
└── finance_data.json     # local JSON, created on first local save; not in Git
```

The window and the CLI do not edit the JSON file themselves. They call the `finance` package. After cloud mode is on, the window saves through `finance.session` and `FinanceStore.save()` refuses to write `finance_data.json`.

## Setup

Use **Python 3.14** in a virtual environment. The Windows automated suite was run with Python 3.14.3.

### Windows (PowerShell)

```powershell
cd C:\dev\FinanceTracker
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r lib_requirements.txt
```

### macOS (Terminal)

```bash
cd /path/to/FinanceTracker
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r lib_requirements.txt
```

Runtime libraries are PySide6, `cryptography`, the Google API client, `keyring`, and `platformdirs`. They are listed in `lib_requirements.txt`. There is no `requirements.txt`.

## Run the app

```bash
python main.py
```

On Windows, `run.bat` does the same thing with the virtual environment's Python. The current directory must be the project root.

Which data the window opens depends on the profile for this process:

| Profile | Where it lives | What `python main.py` does |
| --- | --- | --- |
| Production | `%LOCALAPPDATA%\FinanceTracker` on Windows, `~/Library/Application Support/FinanceTracker` on macOS | Local JSON, until `google_setup` has saved a folder id here |
| Dry-run | whatever `FINANCETRACKER_CONFIG_DIR` points at | The folder saved in that directory. Cloud mode in a dry-run profile does not affect production |

Leave `FINANCETRACKER_CONFIG_DIR` unset for normal use and for the real migration. Set it only for the test-folder commands below. The dry-run keyring name is `FinanceTracker-profile-` plus a hash of that directory. The production name is `FinanceTracker`. A dry-run cannot write the production Google credential.

With cloud mode off and no Drive folder ID, the window loads and saves `finance_data.json` beside the program. With a saved folder ID, it enters the cloud startup flow and asks who is using the computer and for the finance passphrase. The profile variable does not change the local JSON path. Use a separate checkout for dry runs so the real JSON is out of reach of that checkout's import and local-mode flows.

## Run the tests

From the project root, with the virtual environment activated:

```bash
python -m pip install "pytest>=8.4"
python -m pytest
```

The tests use synthetic records, temporary directories, and stand-ins for Drive and the keyring. They do not read `finance_data.json`, and they do not contact Google. There is no separate linter or CI configuration in this repository.

## Google sign-in setup

This prepares a Google sign-in and remembers one shared Drive folder. It does **not** upload finance data, and it does **not** turn cloud mode on. Do the sign-in checklist below for **both** Google accounts before the shared-folder check that follows it.

Use a dry-run profile so this experiment does not become the production config. Set this in the same terminal session before the setup command, including when you run it a second time. Unset it before a normal launch or the real migration.

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
8. Open **Audience**, select **Publish app** and confirm the status is **In production**. Google allows limited personal-use apps to skip verification. Both users can expect an unverified-app warning. [Google's personal-use verification guidance](https://support.google.com/cloud/answer/13464323)

For this scope, an External app in **Testing** gets refresh tokens that expire after seven days. **In production** removes that fixed testing limit. Tokens can still expire or be revoked. [Google's refresh-token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration)

### Why this is the full Drive scope

The app must discover snapshots that either account adds to the shared folder. Google's `drive.file` grants per-file access, so this folder-link workflow uses the full `drive` scope. The token can reach the account's Drive. The adapter restricts app operations to the confirmed folder and its matching snapshot and lock files. [Google Drive scope reference](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)

Google Advanced Protection or a Workspace policy can block authorization. Both accounts need to authorize successfully for the shared-folder checks. [Google App Audience guidance](https://support.google.com/cloud/answer/15549945)

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
- In Google Cloud, confirm user type **External** and publishing status **In production**.
- More than seven days after authorization, verify saved sign-in on both computers. Run the setup command if cloud mode is still off, or relaunch the synced GUI if it is already on. If Google requests consent, check the publishing status and whether the token was revoked.

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

`ping` reads the folder and Drive's clock. Online it prints the folder name and Drive time in UTC. Offline it should report the failure. The Drive HTTP requests have a 30-second timeout per request.

`create` always reads the new snapshot back with `files.get`. If the folder listing is slow, it also says the listing had not caught up and the snapshot is still treated as saved. Either sentence is a successful read-back.

If sign-in, sharing, listing, content updates, or read-back by file id fails for either account, stop. Do not continue to snapshot history, the lock, or the finance window on top of that.

## Snapshot history check

Run this only after the shared-folder check passed for both Google accounts, and only after that test folder is empty again. It uploads synthetic encrypted snapshots. It does not read `finance_data.json`, it does not ask for a real finance passphrase, and it does not turn cloud sync on.

Use the same dry-run profile and the same empty test folder. From the project directory, with the virtual environment active and `FINANCETRACKER_CONFIG_DIR` still set:

```powershell
.\.venv\Scripts\python.exe -m finance.snapshot_check fill
```

```bash
python -m finance.snapshot_check extend
python -m finance.snapshot_check open
python -m finance.snapshot_check clean
```

`fill` prints the folder name and asks you to type that name. Type it only when it is the empty test folder. It then saves 22 synthetic snapshots from this Google account. The passphrase it prints, `snapshot-check-passphrase`, is only for these test files. Do not reuse it for real finance data.

Expected result on the account that ran `fill`:

1. The folder keeps 20 encrypted snapshots. The two oldest were moved to that account's Drive trash. They were not permanently deleted.
2. Opening each remaining snapshot succeeds. The command prints `Opened generation` for each one.
3. The files in Drive do not contain the text `synthetic-snapshot-check-marker`. That text is inside the encrypted document only.
4. The local folder printed in the output contains only encrypted snapshot files, at most 10 of them. `config.json` still has `"cloud_mode": false`.

On the other Google account, with its own dry-run profile pointed at the same folder, run `extend`. Expected result: it saves two more snapshots, and every snapshot created by the first account is still in the folder. A line saying a file was trashed should name only a file this second account created. `not owner — skipped` means the other account's file was left in place.

`open` on either account decrypts every snapshot still in the folder and checks the local encrypted copies.

When the check is finished, run `clean` on each account. Each account moves only its own snapshots to its Drive trash. Leave the test folder empty. Cloud mode in this dry-run profile is still off. Unset `FINANCETRACKER_CONFIG_DIR` before opening the everyday app.

## Lock check

Run this only after the shared-folder check passed for both Google accounts. Use the empty test folder, or leave synthetic snapshots in it. This check does not read `finance_data.json`, does not turn cloud sync on, and does not delete the lock file. Snapshot files stay where they are.

A lock left by `python -m finance.drive_check` is marker text, not a FinanceTracker lock. Move that file to the owner's Drive trash in the Drive website before this check, or the next command will report that it cannot read the lock and will leave it unchanged.

Use the same dry-run profile. From the project directory, with the virtual environment active and `FINANCETRACKER_CONFIG_DIR` still set:

```powershell
.\.venv\Scripts\python.exe -m finance.lock_check hold
```

```bash
python -m finance.lock_check open
python -m finance.lock_check plant
python -m finance.lock_check idle
```

`hold` prints the folder name and asks you to type that name. Type it only when it is the test folder. It then takes the lock for Boris and heartbeats about every 30 seconds. Press Enter to release it.

Expected results:

1. Start `hold` in one terminal. While it is still running, start `open` in a second terminal with the same dry-run profile. `open` prints `Mode: read_only` and says another window on this computer is open. It does not change the lock. Press Enter in the first terminal. `open` again: it prints `Mode: editable`, and Enter releases the lock.
2. Stop `hold` from Task Manager instead of pressing Enter, then run `open`. It prints `Mode: takeover` and says this computer already holds the lock. Type `TAKE OVER`. The new window takes the lock. Enter releases it.
3. `plant` writes a stale lock held by Boris on "Other computer". `open` prints `Mode: stale` and does not take it. Type `RECOVER` only when you mean to take that lock. `python -m finance.lock_check plant --fresh` writes a current heartbeat instead. `open` then prints `Mode: read_only` and leaves the file unchanged.
4. `idle` takes the lock and asks you not to type. After about 10 minutes it prints `Mode: paused` and `The lock is free.` Heartbeats during that wait keep the lock from looking abandoned. `config.json` still has `"cloud_mode": false`.

The finance window, with the dry-run variable unset, still saves the local JSON file. Cloud mode in this dry-run profile is still off.

## Encrypted sync

Do this only after both Google accounts have passed the sign-in, shared-folder, snapshot, and lock checks above. Keep using the **test** folder and a **dry-run** profile until the synthetic session check passes on both computers. The real finance file stays where it is until the real-migration section.

### Passphrase

Both people enter the same finance passphrase on every launch. The app keeps it for the session. When creating or importing a dataset, choose the new passphrase and enter it twice. Restoring an encrypted copy uses its existing passphrase.

A forgotten passphrase makes the data and every backup permanently unreadable. There is no recovery. Store it in a password manager. Changing the passphrase is not supported.

### What a save does

Each successful add, edit, or delete uploads one new encrypted file, named like `finance_snapshot_g000123_<UTC time>_<random>.enc`. The generation number increases by one. The window says **Saving…** while that upload is checked, and **Connected — saved** only after the downloaded bytes match what was encrypted. Closing a window that is already saved does not upload another copy.

If a save cannot be verified, the change stays on screen as unsaved and the previous good snapshot stays in Drive. Retry from the status line while you still hold the lock. If Drive received the upload before verification was interrupted, reopening can show that generation. Check the existing dataset and Backups before entering the change again. When a newer generation blocks a stale save, the app offers to keep the unsaved change visible or reload the Drive copy and discard it. It does not merge changes.

### Lock

One permanent `finance_lock.json` lives in the Drive folder. It is updated in place and is not deleted. Only one computer edits at a time.

- While the other person holds the lock, this window opens **read-only** and can still show totals, tables, and the chart. Add, edit, and delete are hidden.
- After about 10 minutes with no typing or clicking, and with nothing unsaved, the lock is released and the window shows **Paused**. Click **Resume editing**. If the other computer saved in the meantime, the window reloads that data before an edit dialog opens.
- A heartbeat updates the lock about every 30 seconds, using Drive's clock.
- After a crash, the same computer can take its own lock back on the next start.
- A lock from the other computer is stale after about 3 minutes without a heartbeat. The window explains that and waits for an explicit confirmation before taking it. Check that the other app is closed first.
- A laptop that slept while holding the lock cannot save until it has checked the lock again.

### Offline and errors

With no network, the window can open the newest encrypted copy stored on this computer. The status names the generation and the time, and editing stays disabled. That copy is never edited and never replaces a newer Drive snapshot on its own.

A wrong passphrase does not create an empty dataset and does not change Drive. If the newest snapshot cannot be opened, the window says it was skipped and opens the newest older snapshot that can. It does not overwrite Drive because a read failed.

### Backups and restore

Retention keeps the 20 newest known-valid snapshots plus the newest known-valid snapshot from each of the last 30 UTC days. Pruning runs only after a verified save and preserves at least five known-valid snapshots when pruning is possible. Each Google account moves only its own eligible files to Drive trash. Extra files can remain if the current profile has yet to verify them or they belong to the other account. Known invalid files stay in place and appear under **Cannot be restored**.

This computer also keeps the 10 newest encrypted snapshots in the profile directory (`encrypted_snapshots`). Those files are ciphertext.

**Backups** lists generation, time, source, and record counts. Drive entries use the owner's Google account label where available. Snapshot headers expose counts, generation, time, and next ID. Transaction details are encrypted. A readable header alone does not verify a snapshot. Restore verifies the selected data and, if it differs from the current data, uploads it as a new generation. Restore requires the editing lock and a clean window.

### Command line

`cli.py` reads and writes the checkout's local JSON when cloud mode is off for the active profile, even immediately after `google_setup` saves a folder ID. Use the GUI during migration and for synced records.

```bash
python cli.py add-spending Boris 14.20 Groceries 2026-06-11 --note "toilet paper"
python cli.py add-income Milena 3560 2026-06-03
python cli.py summary 2026 6
python cli.py summary 2026 6 --user Boris
python cli.py list 2026 6 --user Milena
```

Once cloud mode is on, every `cli.py` command exits nonzero with an explanation before it opens `finance_data.json`. The `python -m finance.*_check` commands are separate test utilities.

## Local JSON, before sync

`finance_data.json` is created next to the program the first time a local-mode window saves. It is plain JSON. Example:

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

IDs are one shared sequence across income and spending. Every record has a `user` of `Boris` or `Milena`. The path is `default_data_path()` in `finance/config.py`.

Each local save replaces the whole file. Git ignores this file, so switching branches in the same checkout leaves it in place and another checkout starts without it. Migration preserves the source and creates a dated plaintext backup. Keep both in a protected local location outside Drive while you confirm the cloud data and Backups.

## Synthetic session check

Use the separate test checkout and the confirmed dry-run profile on each computer. Start with a test folder that has zero snapshots. A valid, free lock left by `lock_check` can be reused. Use fresh profiles and a fresh test folder if a previous rehearsal already enabled cloud mode.

On Windows, create a fictional October dataset in the **test checkout**. These commands use the active virtual environment. The first command opens the file in exclusive-create mode, so it fails if a file already exists.

```powershell
python -c "from pathlib import Path; Path('finance_data.json').open('x', encoding='utf-8').write('{}')"
```

After that command succeeds, add the synthetic records and launch the GUI.

```powershell
python cli.py add-income Milena 100 2026-10-01 --note "Synthetic income"
python cli.py add-spending Boris 14.20 Groceries 2026-10-02 --note "Synthetic spending"
python main.py
```

In the window, enter the test passphrase at the initial unlock prompt. Choose **Import the local finance file**, confirm the passphrase twice and pick a local backup directory. Expect a dated backup, unchanged source, generation 1 and one permanent lock. The combined October totals should be earned €100, spent €14.20 and gap €85.80.

On the Mac, activate its virtual environment in the test checkout, use its separate dry-run profile, confirm the same folder via `python -m finance.google_setup` and run `python main.py`. Use Milena's account and the same test passphrase. Cancel setup if the app offers to create an empty dataset.

### Acceptance checklist

- Wait at least a minute between opening the apps on the two computers. Open the Mac while the Windows app still holds the lock. The Mac shows the same data and names Boris as the lock holder. Close the Windows app, then use **Try again** on the Mac to take the lock.
- From the test window, add, edit and delete a fictional record. Check **Backups** after each save and confirm the generation advances once. Verify the month tabs, totals, tables and chart. Reopen on the other computer and compare.
- Restore a version containing different data. It becomes the next generation and the previously current snapshot remains available. Inspect the test folder for encrypted snapshots and one lock. For corruption recovery, use only disposable synthetic files and check fallback to a good copy.
- Reopen with a wrong passphrase and check that the folder is unchanged. Then reopen with the correct one. Disconnect the network and relaunch after an online open has populated the cache. Expect read-only data with the saved generation and time.
- On both machines, check clean close, crash recovery, idle release and resume after the other machine saves. After sleep, confirm the old window cannot save until it verifies lock ownership again. For failed or stale saves, verify the change stays visibly unsaved and existing snapshots remain usable. The automated suite exercises these fault paths with fake Drive.
- In each synced dry-run profile, `python cli.py summary 2026 10` must exit nonzero and leave the test checkout's JSON untouched.

`finance.session_check` is available for a terminal-only synthetic migration or open check. It accepts an explicit synthetic source with `migrate --source` and refuses the checkout's default `finance_data.json` as that argument. The GUI rehearsal above tests the same import path used for the real rollout.

## Real migration

Do this only after the synthetic check has succeeded on **both** the Windows PC and the Mac. The real file is imported once, from Windows. The Mac then opens the same cloud dataset.

1. In the Drive website, create a new folder that is **not** the test folder. Share it with the other Google account as **Editor**.
2. On Windows, close every terminal that still has `FINANCETRACKER_CONFIG_DIR` set. Open a new one, activate the virtual environment, and confirm the variable is empty:

```powershell
Remove-Item Env:FINANCETRACKER_CONFIG_DIR -ErrorAction SilentlyContinue
echo $env:FINANCETRACKER_CONFIG_DIR
```

The echo should print nothing.

3. In the Windows checkout that contains the authoritative `finance_data.json`, run the reviewed `secure-sync` version. Copy the same desktop client JSON to the production profile. `python -m finance.google_setup` prints the exact path when the client file is missing. Run setup as Boris, paste the production folder link and confirm its name and owner.
4. Run `python main.py`. Confirm the folder, select Boris and enter the passphrase you plan to use at the initial unlock prompt. Choose **Import the local finance file**, enter the new passphrase twice and select a private local backup folder outside Drive and the FinanceTracker profile. Keep the dated plaintext backup while you check both computers and **Backups**.
5. Expected after a successful import: cloud mode is on, the original `finance_data.json` is unchanged, Drive contains `finance_lock.json` and `finance_snapshot_g000001_….enc`, and the window shows the same months and totals as before. Open **Backups** and confirm generation 1 is listed. Do not paste amounts or notes into a chat or a commit message.
6. On the Mac, use the same reviewed version with `FINANCETRACKER_CONFIG_DIR` unset (`unset FINANCETRACKER_CONFIG_DIR`). Copy the same client JSON to the production path printed by `python -m finance.google_setup`. Run setup as Milena and paste the same production folder link.
7. Run `python main.py` on the Mac. Enter the same passphrase. Expected: the Windows data opens. The empty-dataset question should not appear. If it does, cancel and check the folder id and the Editor share.
8. Use the two computers one after another, at least a minute apart. Add a record on one, close the window, and open the other. The new record should be there. While one window is open and not paused, the other should be read-only.

If verification is interrupted, keep the original and backup. Drive may already have received the encrypted upload. Reopen the same folder with the same passphrase and check whether generation 1 opens. If Drive is still empty, retry the import. An existing valid lock file is reused.

## Changing categories, currency or people

Edit `finance/config.py`:

- `CATEGORIES` — the fixed list shown in the spending category dropdown.
- `CURRENCY_SYMBOL` — the symbol shown next to amounts.
- `USERS` — the two people (currently Boris and Milena). Renaming a person requires changing every stored record as well. The encrypted snapshots keep the old spelling until they are migrated by hand.
