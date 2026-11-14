# FinanceTracker

FinanceTracker is a Python/PySide6 desktop app for Boris and Milena's household income and spending on Windows and macOS. Transactions belong to one person, and the combined tab shows household totals. The tabs set a record owner or filter; Google sign-in belongs to the separate sync profile.

## Branches and plans

- `master` at `d16408c` is the local-file baseline. `secure-sync` at `c34c503` implemented the desktop Google Drive sync path and reached Step 11 documentation. A commit labeled Step 11 is evidence of code and documentation, while live acceptance requires results from both computers and both Google accounts.
- [GUI Secure Sync plan](planning/FinanceTracker_SecureSync_ImplementationPlan.md) covers the initial desktop rollout. [README](README.md) is the runbook for current behavior, synthetic acceptance and migration from Windows.
- [CLI Secure Sync plan](planning/FinanceTracker_SecureSync_CLI_ImplementationPlan.md) is follow-up work after GUI acceptance. Start it by rechecking the proposed parity table and current session API. The local CLI has add-spending, add-income, summary and list. With `cloud_mode=true`, it exits before argument parsing or reading the legacy file.

## Code map

- `main.py` selects the startup path using `finance/profile.py`. With no folder and cloud mode off, the GUI opens `default_data_path()` beside the source. A saved Drive folder sends the GUI through `ui/cloud_startup.py` even while cloud mode is off. Saving the folder and enabling cloud mode are distinct operations.
- Keep Qt in `ui/`. `ui/main_window.py` manages person tabs, populated months, totals, tables and record actions. `ui/add_dialog.py` validates form input. `ui/spending_chart_dialog.py` uses person-filtered category totals and a separately computed two-person split. Combined mode can edit and delete existing records when the session permits editing. New records are added from a personal tab.
- `FinanceStore` owns in-memory models, mutations, queries and schema-v2 JSON. `finance/models.py` uses `Decimal` in memory and writes JSON numbers. IDs share one sequence. Edits preserve ID, owner and recording date. Transaction dates drive inclusive period queries. Keep local save/load compatibility and deterministic `to_json_bytes()` / `from_json_bytes()`. The core accepts legacy categories and the existing on-disk schema.
- `finance/session.py` coordinates cloud startup, dirty state, migration, locking, save, offline view and restore. `ui/cloud_open.py`, `ui/cloud_host.py`, `ui/cloud_status.py` and `ui/recovery_dialog.py` handle prompts and presentation. `ui/session_runner.py` serializes long GUI session jobs, and `finance/drive_adapter.py` uses a single Drive worker per connection.
- `finance/crypto.py` uses AES-256-GCM with scrypt and fresh salt and nonce per snapshot. The generation, time, record counts and next ID are visible in the header and authenticated on decryption. `finance/snapshots.py` manages ordering, verification by file ID, conflict detection, retention and local ciphertext copies. Recovery can list metadata before verification; `Verified` status is distinct from a readable header.
- `finance/lock.py` uses one permanent `finance_lock.json`, a profile-scoped OS process lock, Drive time, a 5-second acquisition confirmation, 30-second heartbeats, a 3-minute stale threshold and clean idle release at 10 minutes. Token and generation checks protect sequential use. Drive does not provide an atomic compare-and-set for this lock.
- `finance/google_auth.py` puts refresh tokens in the OS credential store. `finance/google_setup.py` confirms a dedicated Drive folder and saves its ID. The app requests full Drive scope for this folder-based workflow. The adapter confines normal operations to recognized app files in the configured folder.

## Data and test boundaries

- `finance_data.json` is ignored by Git and belongs to the user’s checkout. Switching branches leaves the file in place. `FINANCETRACKER_CONFIG_DIR` changes the config, keyring namespace, caches and process lock only. Local-mode GUI and CLI still use the checkout’s `finance_data.json`.
- Use temporary paths, fake Drive and fake credentials for automated tests. For real-Drive dry runs use a separate checkout, a dedicated test folder and separate profiles for both Google accounts. The `finance.*_check` modules are diagnostic commands for test folders. `finance.session_check` is distinct from the application CLI and refuses the default migration source.
- Production migration runs once from the authoritative Windows JSON. Keep the source and dated plaintext backup outside Drive. Milena joins the verified dataset from the Mac. Cloud saves use only encrypted snapshots and the original remains in place. `FinanceStore.save()` and `cli.py` block the plaintext path once cloud mode is enabled in the active profile.
- Keep real transactions, plaintext copies, credentials and passphrases out of Git, logs, prompts and fixtures.
- Use Python 3.14 for setup with the dependency versions exercised on Windows. Install `lib_requirements.txt`, then run `python -m pip install "pytest>=8.4"` and `python -m pytest`. See the README for platform commands and live checks.

## Follow-up from the handoff

- Test startup with a slow or disconnected network. Save and session jobs use workers, but initial Google authorization and the first folder lookup wait on the Qt thread in `ui/cloud_startup.py`.
- If Drive accepts an upload and the read-back fails, the save remains unsaved in the GUI. A retry can hit the newer-generation guard. Reopening can recover the uploaded copy. A fake-Drive reproduction confirmed this sequence. A future retry improvement should verify the existing upload before attempting a new generation.
- For the future CLI, `FinanceSession.open_with_passphrase()` tries to acquire the editing lock after loading. A read-only CLI command needs a shared session entry point that skips acquisition. Reuse the core recovery and lock APIs, and extract shared input validation from the GUI when implementing parity.
