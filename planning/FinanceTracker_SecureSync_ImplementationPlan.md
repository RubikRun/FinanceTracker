# FinanceTracker: Secure Google Drive Sync — Step-by-Step Implementation Plan

## Instructions to the coding agent

Implement this plan **one numbered step at a time, in order**. Do not skip ahead or combine multiple steps just because they appear related. **Steps 4 and 5 are a go/no-go gate:** do not start Step 6 or any later step until both Google accounts have authorized the unverified desktop app and the Step 5 shared-test-folder checks have passed.

At the end of every step:

1. Summarize the files and behavior changed.
2. List the exact automated test commands you ran and their results. Do not report tests as passing if they were not run.
3. Give me a short manual test checklist for that step, including the expected result for each test and any prerequisites.
4. State what is not yet implemented and any decisions or blockers that need my input.
5. **Stop and wait for me to review/test the step before starting the next one.**

Make the smallest cohesive change that completes the current step. Keep the app runnable after each step whenever practical. If the repository differs from this plan or from `AGENTS.md` (the current handoff document; there is no `FinanceTracker.md` in the repo), inspect the actual source code and adapt carefully; source code is the authority for current behavior, and this document is the authority for the new agreed requirements.

Never use the real `finance_data.json` for destructive experiments, tests, demos, screenshots, or generated fixtures. Do not print, log, commit, or paste real transaction data into test output or documentation. Use synthetic finance records and temporary directories.

---

## 1. Goal and agreed requirements

Add secure, practical cross-device syncing to the existing FinanceTracker desktop app so it can be used on a Windows 11 PC and a MacBook. Both people use the same encrypted finance dataset stored in a Google Drive folder that is shared with both Google accounts.

The existing app is a Python/PySide6 GUI with a small CLI and a UI-independent `finance` package centered on `FinanceStore`. The data model is JSON schema version 2. The GUI currently saves after successful add/edit/delete operations, and `FinanceStore.save()` uses a temporary file and atomic replace for local saves. There is currently no sync, encryption, backup history, lock, or automated test suite. Keep the current financial functionality and schema; this is a persistence/security change, not a redesign of the finance app.

The implementation must meet these requirements:

- **Google Drive API directly**, rather than relying on a locally synced Google Drive folder. This lets the app know when an upload has completed and avoids syncing a half-written file.
- **Encrypt financial data before it leaves the app.** Google Drive must contain only encrypted finance snapshots, not readable finance JSON.
- Use **AES-256-GCM** for authenticated encryption and **scrypt** with a fresh random salt to derive a key from a passphrase.
- Ask for the encryption passphrase whenever a FinanceTracker session starts. Do not save the finance passphrase in a config file, source file, Google Drive, or OS keychain. This is the safer and simpler first version; convenience features such as remembering the passphrase are out of scope. When a passphrase is first chosen (new dataset or migration), require it to be typed twice.
- Each person signs into Google through OAuth on their own computer **with their own Google account**. One partner owns a dedicated Drive folder and shares it with the other as Editor. Store only the OAuth refresh token in the operating system credential store (Windows Credential Manager / macOS Keychain, through an appropriate Python keyring library). Do not casually create a plaintext `token.json` containing refresh tokens. If secure credential storage is unavailable, fail clearly or require reauthentication rather than silently falling back to a plaintext token file.
- Use the full `https://www.googleapis.com/auth/drive` scope. This is a deliberate, agreed decision: with two separate Google accounts, the narrower `drive.file` scope cannot work (see Section 3). Compensate in code by restricting every operation to the configured folder.
- Keep a **tiered set of valid, verified encrypted snapshots** in the configured Drive folder: the 20 most recent, plus the newest valid snapshot of each of the last 30 days, and never fewer than 5 known-valid snapshots. Define these numbers as constants in one place. A new snapshot is created for each successful add/edit/delete operation. Never remove an older valid snapshot until the new snapshot has uploaded and passed verification. Old snapshots are moved to Drive trash, never permanently deleted.
- Continue saving after every successful add, edit, or delete. On normal close, flush any pending unsaved changes; do not create an unnecessary duplicate snapshot if there have been no changes since the last successful save.
- Use a **separate lock file** in Drive, not a `#BLOCKED` line or any other marker inside the encrypted data file. The lock file is created once during setup and afterwards only updated, never deleted, because only a file's owner can trash or delete it (see Sections 3 and 4). The users agree that attempted opens on different machines will be separated by at least one minute. Design a simple lock around that workflow rather than adding a server-side locking service.
- A lock must have a unique session token, owner/machine label, UTC timestamps, a heartbeat while the app is running, stale-lock recovery, and token checks before saving/releasing it. Never silently assume a lock belongs to the current session.
- When the other person holds the lock, offer **read-only viewing** of the latest data instead of refusing to open. Release the lock automatically after a period of inactivity when there are no unsaved changes, so an app left open does not block the other person.
- Number every snapshot with an increasing **generation** and refuse to save if the newest snapshot in Drive is not the one this session's data is based on. This is a second safety net behind the lock.
- If a cloud save fails, never claim that the change was saved. Make unsaved status obvious, retain the in-memory change so it can be retried, and provide a clear retry/discard/keep-open path. Do not let a session with unconfirmed lock ownership continue making changes.
- Keep a local **encrypted** copy of the last few verified snapshots on each computer. Use it for read-only viewing when Drive is unreachable and as a second recovery source. Never edit from it while offline.
- Add an understandable recovery path to restore one of the previous valid snapshots, from Drive or from the local encrypted copy.
- Bring the GUI under the new persistence/lock workflow. The GUI must never write the legacy plaintext JSON after sync is enabled.
- **This plan covers the GUI only.** Bringing the CLI under the same workflow is a separate plan, `planning/FinanceTracker_SecureSync_CLI_ImplementationPlan.md`, to be done after this one. Until then, `cli.py` must refuse to run once cloud mode is configured (Step 8), so it can never read or write the legacy plaintext file behind the GUI's back. Keep the session layer UI-independent so the CLI plan can reuse it.
- Preserve the current finance schema and behavior. Do not leak transaction data into logs, errors, filenames, tests, or OAuth metadata.
- Support Windows and macOS. Update setup documentation, including the actual dependency-file name (`lib_requirements.txt`; the current README reportedly refers to a nonexistent `requirements.txt`).

### Deliberate first-version limitations

- This is a two-person, sequential-use app, not a concurrent collaborative editor.
- The lock is designed around the agreed one-minute separation between attempted opens. It is not a distributed lock service and cannot mathematically eliminate races if that usage assumption is violated.
- The finance passphrase is shared by the two people and must be communicated privately. A forgotten passphrase means the data and every backup are permanently unreadable; there is no recovery. Changing the passphrase is not supported in this version.
- Encryption protects the Drive contents and backups; it does not protect data from malware or someone who can use an already-unlocked computer and app session.
- The full `drive` scope means each computer's stored Google token could technically read that person's whole Drive. The app limits itself to the configured folder in code, but the token itself is not limited. Because the app is not verified by Google, both people see an "unverified app" warning when they first authorize it.
- Do not add a hosted backend, database, multi-user account system, or simultaneous-edit conflict resolution.

### Implementation decisions already closed

These were left open or under-specified in an earlier draft. They are now **requirements**, not details for the coding agent to invent. Do not reopen them during implementation.

1. **Lock state lives in `finance_lock.json` file content**, not in `appProperties`. `appProperties` are capped at 124 bytes per key+value and `files.list` can return stale property values after a successful update. After setup, cache the lock file's Drive ID in local config and read/write the lock with `files.get` / `files.update` by that ID, never by searching on name. Heartbeats will create Drive revisions; that is acceptable. Never `files.delete` the lock.
2. **Verify uploads with `files.get` by ID, not by listing.** After `files.create`, the returned file ID is the source of truth. Download and verify that ID. `files.list` is eventually consistent: a just-created snapshot can be missing from a parent-folder list for several seconds. For “newest generation,” list with retries/backoff and `trashed = false`; if the list is missing a file this session just created, union in that known ID. Always request `fields=` explicitly (`id,name,parents,owners,capabilities,createdTime,md5Checksum,trashed`). Default list fields omit almost everything this plan needs.
3. **Isolate real data from development.** Honor `FINANCETRACKER_CONFIG_DIR` as an override for the per-user config/data directory. When it is set, use a distinct keyring service name derived from that profile so tests and dry-runs never touch the production Google folder, production keyring slot, or real `finance_data.json`. Default production paths must ignore the override when the variable is unset. Never run agent integration tests against the live finance folder.
4. **Verify canonical JSON bytes, not re-parsed records.** Serialize once to canonical UTF-8 bytes (stable key order; same `indent` / `ensure_ascii` as today's saver), encrypt those bytes, decrypt, and compare **byte-for-byte** to the bytes intended to be saved. Then parse into `FinanceStore`. Non-integral amounts still go through the existing `float()` JSON representation; do not change the public schema. Object equality after parse is not an acceptable verification.
5. **Recovery metadata lives in the authenticated envelope header.** Put `generation`, `created_at_utc`, spending count, income count, and `next_id` in the envelope AAD/authenticated header. Drive `owners` supplies “which account saved it.” Do not put amounts, categories, notes, or other transaction details in the header, filenames, or Drive metadata. Do not decrypt every retained snapshot just to show the recovery list.
6. **Prune only already-cached known-valid files owned by this account**, and only after the new snapshot is verified. Never block a save on verifying 20–50 old files. Extra uncached files stay until a later save on this computer; leftover backups are harmless.
7. **Folder paste must resolve shortcuts.** Shared-folder links sometimes yield `application/vnd.google-apps.shortcut`. Setup must resolve `shortcutDetails.targetId`, then show name and owner and confirm. Before every update or trash, re-fetch metadata and require: not trashed, parent is the configured folder, name matches `finance_snapshot_g*.enc` or `finance_lock.json`.
8. **Do not enable cloud mode until both the permanent lock file and a verified generation-1 snapshot exist.** Setup is two Drive writes, not one transaction. Retry must be idempotent: reuse an existing lock file, do not create a second. A half-configured folder (lock without snapshot, or snapshot without lock) must not flip local config into cloud mode.
9. **Restore is blocked while the session is dirty** (or the user must explicitly discard unsaved changes first). If the user closes while a save is in flight, wait/cancel/confirm; do not release the lock from under an in-flight upload. After a generation-mismatch refusal, keep the unsaved record visible and offer reload (discard) versus keep-and-retry after they inspect; do not leave “lose it” as the only action.
10. **OAuth consent screen User type must be External** (two personal Gmail accounts). Both computers must use the **same** OAuth desktop client JSON. Google Advanced Protection or a Workspace-managed account can still block unverified apps; if authorization is blocked, stop and report it rather than continuing later steps.

**Go/no-go gate.** Steps 4 and 5 (unverified Drive-scope OAuth plus a shared empty test folder, exercised by both Google accounts) are the first real go/no-go. Do not start Step 6 or any later step until that gate passes. If either account cannot authorize or cannot see the other account's test files, stop and change the storage approach; do not push snapshots, lock, session, or GUI work on top of an unproven Drive path.

## 2. Architecture to implement

Keep finance calculations and the JSON schema in the existing `finance` package. Add a clear persistence/session layer rather than mixing Google API calls and encryption into Qt widgets or finance models.

A suitable conceptual flow is:

```text
                 main.py / GUI
                       |
                       v
       Finance session (UI-independent)
       (passphrase, lock, dirty state,
        load/save/recovery decisions)
                       |
            +----------+-----------+
            |                      |
            v                      v
   FinanceStore / JSON        Cloud snapshot store
   serialization in memory    Google Drive API
            |                      |
            +----------+-----------+
                       v
                Encrypt / decrypt
                AES-256-GCM + scrypt
                       |
                       v
          Shared Drive folder contains:
          - encrypted snapshots, numbered by generation
          - one permanent lock file
          - optional harmless metadata/manifest only

          Each computer also keeps (user data dir, or FINANCETRACKER_CONFIG_DIR):
          - last few encrypted snapshots (offline view / recovery)
          - nonsecret config, machine ID, cached lock file ID, verified-snapshot cache
```

Adapt names and boundaries to the existing code, but keep these responsibilities separate:

- `FinanceStore` remains UI-independent and owns finance records and queries.
- Serialization/deserialization must be possible **in memory**. Routine cloud operations must not write decrypted finance JSON to a persistent local file.
- An encryption module accepts plaintext bytes and a passphrase and returns an authenticated encrypted envelope; the inverse verifies authentication before returning plaintext bytes.
- A Drive adapter owns OAuth/API operations: list, download, upload, trash, and lock-file reads/updates. It should be replaceable by a fake adapter in tests.
- A snapshot repository owns generation ordering, verification, the tiered retention policy, and the local encrypted copy.
- A session/controller coordinates startup, lock ownership, read-only/editable/offline modes, store loading, saves, dirty state, idle lock release, backup restoration, and shutdown.
- Qt dialogs/status indicators display results and errors but do not implement cryptography or call raw Drive endpoints directly.

**Threading.** Google's Python client is built on `httplib2`, which is not thread-safe, and its default request timeout is 60 seconds. Run every Drive call (and the scrypt key derivation) on **one dedicated background worker** that executes jobs one at a time, so calls never share an HTTP object across threads and never block the Qt event loop. Give every request an explicit, short timeout (for example 20–30 seconds). Keep the worker in the UI-independent layer (for example a single-thread executor); the GUI receives results on the Qt thread. While a save is in progress, the GUI shows "Saving…" and does not start another mutation.

The agent may choose exact module/class names after inspecting the repository. Avoid introducing an elaborate framework or database.

## 3. Security and file-format requirements

### Encryption

- Use the maintained `cryptography` package.
- Use AES-256-GCM with a 32-byte key, a fresh cryptographically random nonce for every encryption, and a fresh random salt for every snapshot.
- Derive the key from the user-entered passphrase with `cryptography`'s scrypt KDF and explicit documented parameters. Choose parameters that are practical on both target computers and cover them with tests.
- Include a file-format version, KDF parameters/salt, nonce, and authenticated ciphertext in a documented envelope. Use authenticated associated data for stable context/version fields where appropriate.
- Do not reuse a nonce with the same key. Do not invent custom cryptographic primitives.
- Decryption must fail closed on a wrong passphrase, tampering, truncation, unknown format version, or malformed envelope. It must not return partial or untrusted plaintext.
- The KDF parameters are read from the file header before the file is authenticated. Reject any parameters above fixed maximums (memory and CPU cost) before deriving a key, so a tampered file cannot make the app allocate gigabytes or hang.
- The envelope's authenticated header also carries the snapshot generation number, creation time, spending count, income count, and `next_id` supplied by the caller (Section 1, closed decision 5), so they cannot be altered without detection. The finance JSON schema itself stays unchanged.
- The outer file may expose only minimal non-sensitive format metadata. It must not expose transaction values, category names, notes, owner details, or the JSON document. Record counts and `next_id` in the authenticated header are the only finance-adjacent figures allowed, so the recovery list does not have to decrypt every snapshot.
- Never log the passphrase, derived key, decrypted JSON, or transaction contents. Error messages may say that authentication/decryption failed, but must not dump payload bytes.

### Local storage and OAuth credentials

- Put nonsecret per-user application configuration under a platform-appropriate user configuration directory, not beside source code. Configuration may contain a Drive folder ID, the cached lock file ID, and other nonsecret settings, but no finance data or encryption passphrase.
- Honor `FINANCETRACKER_CONFIG_DIR` as described in Section 1 (closed decision 3). When set, all config, local encrypted copies, process lock, known-valid cache, and the keyring service name must stay inside that profile. Tests and developer dry-runs must set this variable and must never use the live finance folder.
- Persist OAuth credentials through the OS credential store. Store only the refresh token (the client ID/secret come from the client configuration file). Windows Credential Manager limits an entry to 2560 bytes, about 1280 characters through `keyring`, so a full serialized credentials object can fail to save. On Windows, `keyring` may raise a non-`KeyringError` exception for this; catch it and report a clear error. The OAuth client configuration file and any local app settings must not be committed to Git. Add appropriate ignore rules and document the per-machine setup.
- Each computer generates a random machine ID once and stores it in the local config. Use it, not the hostname, to recognize "this computer" in the lock.

### Google Drive scope and account model

The agreed model is two separate Google accounts sharing one folder that is owned by one of them. That model has hard constraints that the implementation must respect:

- **`drive.file` cannot work here.** Its access is granted per user: the other person's app cannot see files created by this person's app, even inside a shared folder. Selecting the folder through Google Picker grants access to the folder itself, not to files in it or files added later. Because every save creates a new file, this cannot be made to work. Use the full `drive` scope and document why in the setup docs.
- **Only a file's owner can trash or delete it** in a regular (non-Workspace) shared folder; an Editor can read and update content but not remove it. Every file is owned by whichever account created it. Therefore:
  - The lock is one permanent file that is updated in place and never deleted during normal operation (Section 4).
  - Pruning only trashes snapshots owned by the current account (check `capabilities.canTrash`). Snapshots owned by the other account that are due for pruning are left in place; that account's app trashes them on its next save. Leftover old snapshots are harmless extra backups.
- **Never permanently delete files in normal operation.** `files.delete` bypasses Drive trash. Use `files.update` with `trashed=true` instead, so pruned snapshots stay recoverable from the owner's trash for 30 days.
- **OAuth publishing status must be "In production".** While a Google Cloud OAuth app is in "Testing" status, refresh tokens expire after 7 days, which would force both people to sign in again every week. Publish the app without submitting it for verification. Because `drive` is a restricted scope, Google shows an "unverified app" warning during authorization and caps the app at 100 users; both are acceptable for two people. The consent screen **User type must be External** (two personal Gmail accounts; Internal is Workspace-only). Both computers must use the **same** OAuth desktop client JSON. Document the exact unverified-app warning clicks (including “Advanced → Go to … (unsafe)” on macOS/Safari and Chrome). Google Advanced Protection or a Workspace-managed account can still block unverified apps. If publishing or authorizing an unverified app with this scope turns out to be blocked for either account, stop and report it; do not continue past Step 5. The fallback, only if that gate fails, is Testing status with weekly re-authorization, or a later storage-approach change.
- **Code-enforced folder restriction.** Because the token can reach the whole Drive, restrict app operations to the configured FinanceTracker folder in one central place in the Drive adapter. Before every update or trash, re-fetch the file's metadata by ID and confirm that it is not trashed, its parent is the configured folder, and its name matches `finance_snapshot_g*.enc` or `finance_lock.json`. Never search for or modify unrelated files, and never touch items outside this folder.
- **Folder setup.** The folder owner creates a dedicated folder in the Drive web UI and shares it with the other account as Editor. On each computer, the user pastes the folder link or ID during setup. If the ID is a Drive shortcut (`application/vnd.google-apps.shortcut`), resolve `shortcutDetails.targetId` and use the target folder. The app shows the folder's name and owner and asks for confirmation before using it.

### Snapshot names, generations, and ordering

- Give every snapshot a unique filename that includes a zero-padded generation number, for example `finance_snapshot_g000123_<UTC-timestamp>_<random-id>.enc`. Do not put finance values or transaction notes in filenames.
- The generation increases by exactly one per save. The same number is stored in the snapshot's authenticated header; a file whose filename generation and authenticated generation disagree is invalid.
- Order snapshots by generation, not by anyone's local clock. Use server `createdTime` and the random ID only to break ties.
- Each session remembers the generation its in-memory data is based on (its **base generation**). Before uploading, determine the newest snapshot generation (list with `trashed = false`, explicit `fields`, and retries; union in any snapshot ID this session just created) and confirm it still equals the base generation. If it doesn't, another session has saved: refuse to save, keep the unsaved change in memory, and tell the user that the data changed on the other computer. Offer reload (discard unsaved) versus keep-and-retry after they inspect; do not leave “lose it” as the only action.
- After a successful `files.create`, verify **that returned file ID** with `files.get` and a media download of that ID. Do not treat a subsequent folder list as proof that the upload exists or that it is the only new file. Then list (with retries) to look for a conflict: if another snapshot with the same new generation exists (only possible if the lock assumptions failed), keep both, use the one with the earlier server `createdTime` as current, and warn the user clearly that a conflicting save was detected and the other version is available in the restore list. Do not silently discard either. If the list is still missing the file just created, treat the known ID as present rather than as a failed save.
- A new save creates a new file. Do not overwrite the only known-good snapshot.
- A snapshot counts as verified only after it is uploaded, downloaded again **by file ID**, successfully decrypted, parsed, schema-validated, and compared **byte-for-byte** to the canonical JSON bytes that were encrypted (Section 1, closed decisions 2 and 4). Then parse those bytes into `FinanceStore`.
- Keep any operational metadata minimal. Never store the passphrase, derived key, OAuth tokens, or decrypted JSON in Drive metadata.

### Retention

- Keep the 20 most recent valid snapshots plus the newest valid snapshot of each of the last 30 UTC days. Everything else is a pruning candidate. Snapshot files are small (tens of KB), so storage is not a concern; the point is that backups must cover more than the last few clicks. Entering a dozen receipts must not wipe out yesterday's state.
- Prune only after the new snapshot has been verified and at least 5 of the snapshots that will remain are **known-valid**. A save may prune only files this account owns that are **already** in the local `(fileId, md5Checksum)` cache as known-valid. Never block a save on verifying the whole history (first use of a new computer would take tens of seconds). Extra uncached files stay until a later save; leftover backups are harmless. A corrupt/unreadable file never counts as a good backup. Never remove the last known-good snapshot to clean up after a failed upload.
- Prune by moving to trash, and only files owned by the current account (see the account model above).
- Files that fail decryption or validation are never trashed automatically. List them in the recovery view so the user can investigate.
- **Known-valid cache.** Checking validity means downloading and decrypting a snapshot, which costs a deliberately slow scrypt derivation. Doing that for every retained snapshot on every save would make each save take many seconds. Record each successfully verified snapshot as `(Drive file ID, md5Checksum)` in a local nonsecret cache. A snapshot whose server `md5Checksum` still matches its cached entry is known-valid without downloading it again. Snapshots are never modified after upload, so a changed checksum means the file must be re-verified before it counts. The recovery list must use the authenticated header (generation, time, record counts, `next_id`) plus Drive `owners`; it must not decrypt every file to populate those columns.

### Local encrypted copy

- After every verified save, and after loading at startup, write the encrypted snapshot bytes (never decrypted JSON) to a local folder in the platform user-data directory, atomically. Keep the most recent 10.
- The local copy is used only for read-only viewing when Drive is unreachable and as an extra source in the recovery view. It is never edited, and it never replaces newer Drive data automatically. In offline view, show clearly which generation and time the data is from.

## 4. Lock behavior

Use one permanent Drive item, for example `finance_lock.json`, created once when the dataset is first set up. Because only a file's owner can delete it and the two people use different Google accounts, the lock file is **never deleted during normal operation**. Acquiring and releasing the lock means updating its state in place. Its state has fields similar to:

```json
{
  "format_version": 1,
  "state": "held or free",
  "session_token": "random unguessable token",
  "owner_label": "Boris or Milena",
  "machine_id": "random per-computer ID",
  "machine_label": "non-sensitive local label",
  "acquired_at_utc": "ISO-8601 timestamp",
  "heartbeat_at_utc": "ISO-8601 timestamp"
}
```

The exact fields can differ, but do not place finance data in the lock. **The state lives in the lock file's content**, not in `appProperties` (Section 1, closed decision 1). Content updates create a new Drive revision on every heartbeat; Drive prunes old revisions automatically, and that is acceptable. After setup, cache the lock file's Drive ID in local config. Acquire, heartbeat, and release must `files.get` / `files.update` that ID. The Step 5 real-Drive test must confirm that the other account's app reads the updated file content right away.

Put the timing values in one place and document them: heartbeat about every 30 seconds, stale after about 3 minutes, acquisition confirmation delay about 5 seconds, idle release after about 10 minutes.

**Time.** Lock freshness must not depend on the two computers' clocks agreeing. Base "now" on Drive server time, for example from the HTTP `Date` header of a Drive response, both when writing heartbeats and when judging staleness.

Required behavior:

1. At startup, authenticate to Drive and read the lock before allowing any edits. Reading the lock and loading data for read-only viewing do not require holding it.
2. If the lock is held and fresh by another computer, do not overwrite it. Offer read-only viewing and show who holds it, on which machine, and when its heartbeat was last observed. Offer "Try again" to attempt editing later.
3. If the lock is free, write `held` with a new cryptographically random session token. Wait the confirmation delay, re-read, and proceed only if it still contains this session's token. If it doesn't, another computer won the race: back off to read-only and say who holds it.
4. If more than one lock file exists, fail closed and explain that the owner of the extra file must remove it in the Drive web interface; do not guess which one is authoritative. If the lock file is missing while snapshots exist, explain this and recreate it only after the user confirms, then re-list to check for duplicates.
5. Heartbeat about every 30 seconds while editing: re-read the lock first, and update it only if it still contains this session's token. If it contains a different token, ownership is lost: never overwrite it; disable editing (item 9).
6. Do not take over a stale lock without telling the user. Explain that the other app must be confirmed closed, then offer a manual stale-lock recovery action. Before taking over, re-read it and confirm it still has the same token and heartbeat that the user reviewed; then acquire as in item 3.
7. **Crash on the same computer.** Each running FinanceTracker process holds a local OS-level file lock in the user-data directory, and the machine ID plus current session token are recorded locally while the Drive lock is held. If the Drive lock shows this computer's machine ID and the local file lock is free (the previous process is gone), offer to take the lock over immediately instead of waiting for it to go stale. If the local file lock is held, another FinanceTracker process on this computer (for example a second window) is running; that process must stay read-only.
8. **Sleep and long gaps.** If the time since the last successful heartbeat exceeds the stale threshold (for example after the computer slept), treat ownership as unverified until a fresh read confirms the token. Never save based on a confirmation from before the gap. Test this on both operating systems, since some clocks pause during sleep.
9. If ownership cannot be verified or the heartbeat cannot be maintained beyond the safe threshold, disable add/edit/delete and explain that the app can no longer confirm exclusive access. Do not continue silently in offline-edit mode.
10. Before every save, confirm that the lock contains this session's token and the last successful heartbeat is within the threshold, and confirm the base generation (Section 3).
11. **Idle release.** After about 10 minutes without user input, and only when there are no unsaved changes, release the lock and switch to a read-only "paused" state. The next attempt to add, edit, or delete (or a "Resume editing" action) re-acquires it. If the newest generation in Drive differs from the base generation, reload first and tell the user the data was updated from the other computer. If re-acquiring fails, stay read-only and say who holds the lock.
12. To release (on close or idle), re-read the lock and set it to `free` only if it still contains this session's token. Never modify a lock that belongs to another session.
13. If a save fails while the session still owns the lock, keep the changed in-memory state marked unsaved and allow retry. Keep the lock while retrying. On close, offer retry, discard the unsaved changes and reload/return to the last committed state, or cancel closing. Do not release the lock while dirty changes remain unless the user explicitly chooses to discard them.

The one-minute separation agreement makes this simple lock acceptable for this private app. Drive has no atomic "write only if unchanged" operation, so a near-simultaneous race is possible in theory; the confirmation re-read, the per-heartbeat token check, and the generation check together make it detectable rather than silent. Do not build a web service/Apps Script lock server unless real testing shows the agreed design is insufficient and you explain why before expanding scope.

## 5. One-time migration from the existing plaintext file

Only the Windows laptop has real finance data today; the MacBook has none. Migration therefore happens once, from the Windows laptop. The MacBook joins the existing cloud dataset afterwards. No merging of two plaintext files is needed.

Migration must be explicit and recoverable:

- Never overwrite or delete the existing plaintext `finance_data.json` as part of migration.
- On first synchronized startup, first check Drive. If valid encrypted snapshots exist, load the latest valid one; do not import a stale local plaintext file over it. If a legacy local file is also present on that computer, show a one-time notice that it was found and is not used, without reading its contents into the cloud.
- If no encrypted snapshots exist but a legacy local JSON exists, offer an explicit “Import existing local FinanceTracker data into encrypted Google Drive” flow. Show only safe metadata such as record counts, not transaction details.
- Ask for the new passphrase twice and refuse to continue if the two entries differ. Before uploading, warn that a forgotten passphrase makes the data and all backups unreadable, and recommend storing it in a password manager.
- Before uploading, create a dated local backup of the original plaintext file in a user-selected or clearly documented non-synced location. Make clear that this backup is still plaintext and must be protected accordingly. Do not put this backup in the shared Drive folder.
- Load and validate the original using the existing schema rules, serialize to canonical JSON bytes, encrypt in memory, upload a first snapshot, and verify it by `files.get` of that ID, decrypt, and byte-for-byte comparison.
- **Do not write local “cloud mode enabled” until both the permanent lock file and the verified generation-1 snapshot exist.** If anything fails, leave the original file untouched, leave local config out of cloud mode, and report the failure clearly. Retry must reuse an existing lock file rather than creating a second one. If a previous attempt left a lock without a snapshot (or a snapshot without a lock), explain that and finish setup idempotently after the user confirms; then re-list to check for duplicate lock files.
- After successful migration, routine GUI operation must use the cloud session layer and must not save decrypted JSON beside the source code or into the old path, and the CLI is blocked (Section 1). Do not automatically destroy the user's original backup.
- If there are no remote snapshots and no local plaintext file, do not silently initialize an empty cloud dataset. Ask the user to explicitly create a new empty dataset or cancel setup, to prevent a wrong folder ID from overwriting their data. On the MacBook this prompt should never be needed; if it appears there, the folder or sharing is almost certainly misconfigured, and the message should say so. If a local encrypted copy exists on this computer, also offer to restore it into the empty folder (Step 10).
- The first snapshot is generation 1, and the permanent lock file is created as part of the same setup. Cloud mode is enabled only after both exist and the snapshot has been verified (closed decision 8).

## 6. Implementation steps

## Step 0 — Repository preflight and safe baseline

**Tasks**

- Inspect the actual repository, branch/status, `main.py`, `cli.py`, `finance/config.py`, `finance/store.py`, GUI mutation flows, current startup/shutdown behavior, `.gitignore`, README, and `lib_requirements.txt`.
- Compare the current code to `AGENTS.md`; note material differences before changing code.
- Create/use a dedicated feature branch, and ensure no user finance data, OAuth secrets, or local credential files are staged.
- Run the current app with a disposable copy or synthetic dataset and record the baseline behaviors relevant to add/edit/delete, monthly totals, chart, load/save.
- Do not change product behavior in this step.

**Test and expected result**

- Existing launch/setup command is documented and works in the current development environment, or a clear pre-existing failure is recorded.
- A synthetic add/edit/delete/save/reload smoke test behaves as before.
- Git status has no finance-data or credential files staged.

**Manual test for me**

- The agent should give the commands to launch the app against a disposable dataset and ask me to confirm the main UI still behaves normally before proceeding.

## Step 1 — Add automated tests for current finance behavior

**Tasks**

- Introduce `pytest` (and `pytest-qt` only if needed for useful Qt tests) as development dependencies, without unnecessarily changing runtime dependencies yet.
- Add synthetic fixtures and tests for current model/store serialization, add/edit/delete, IDs, and core totals/queries. Test local atomic persistence using a temporary directory.
- Capture important compatibility invariants: current schema version, record ownership, dates, Decimal precision, and no ID reuse on delete.
- Use no real `finance_data.json` values.

**Test and expected result**

- A documented test command (normally `python -m pytest`) passes.
- Tests prove the existing local store behavior before persistence is refactored.
- At least one malformed JSON/schema test demonstrates a clear controlled failure rather than a traceback containing sensitive payload data.

**Manual test for me**

- None should be needed beyond reviewing the test report, unless the agent changes normal app behavior (it should not).

## Step 2 — Separate JSON serialization from persistent file I/O

**Tasks**

- Refactor the finance persistence boundary so the same `FinanceStore` model can load and serialize its JSON document in memory, without requiring decrypted data to live in a local file.
- Preserve the existing local `load(path)`/`save()` behavior. The GUI uses it until cloud mode is integrated. The CLI keeps using it until the separate CLI plan, but only while cloud mode is not configured (Step 8).
- Keep the current JSON schema and existing app-facing finance operations stable. Avoid rewriting unrelated UI or query logic.
- Make serialization deterministic enough for round-trip comparison tests, while preserving the app's existing numeric/date semantics. Produce canonical UTF-8 bytes with stable key order and the same `indent` / `ensure_ascii` as today's saver, so later snapshot verification can compare ciphertext plaintext **byte-for-byte** (Section 1, closed decision 4). Do not change the public schema, including the existing `float()` representation of non-integral amounts.

**Test and expected result**

- Existing Step 1 tests still pass.
- New tests cover in-memory document -> canonical bytes -> store -> canonical bytes round trips with decimals, dates, both users, income, spending, notes, IDs, and next ID. Serializing the same store twice produces identical bytes.
- Local `load(path)`/`save()` remains compatible with existing schema-v2 files.
- A test confirms no temporary persistent plaintext file is needed by the in-memory serialization path.

**Manual test for me**

- Launch the normal app and verify browsing, totals, chart, and a synthetic add/edit/delete still work exactly as before.

## Step 3 — Implement and test encryption independently

**Tasks**

- Add `cryptography` to `lib_requirements.txt`.
- Create a small encryption module with a versioned envelope, AES-256-GCM, a random salt per snapshot, and scrypt-derived 32-byte keys.
- The envelope's authenticated header carries the generation number, creation time, spending count, income count, and `next_id` supplied by the caller (Section 3 and closed decision 5).
- Enforce maximum KDF parameters on decrypt before deriving a key.
- Choose scrypt parameters that take well under a second on both computers; key derivation will run on the background worker, not the GUI thread.
- Keep crypto functions independent of Qt, Google Drive, and `FinanceStore`.
- Document the chosen KDF parameters and envelope format in code comments or developer docs.
- Treat all decryption errors as failures; no “best effort” partial decrypt and no automatic creation of a blank store when decryption fails.

**Test and expected result**

- Round-trip encryption/decryption reproduces the exact test plaintext and the authenticated generation, creation time, record counts, and `next_id`.
- Encrypting the same plaintext twice produces different envelopes due to random salt/nonce.
- A wrong passphrase, changed ciphertext byte, changed authenticated header (including the generation), truncated file, and unsupported envelope version all fail safely.
- A header with KDF parameters above the maximums is rejected without attempting key derivation.
- A test confirms plaintext markers from synthetic JSON do not appear in the encrypted bytes.
- `python -m pytest` passes. No network or GUI is needed for these tests.

**Manual test for me**

- The agent should explain how the crypto tests establish correctness and note explicitly that they do not yet mean Google Drive sync is active.

## Step 4 — Add per-user configuration and secure Google OAuth credentials

This step starts the **go/no-go gate** with Step 5. Confirm Python 3.14 compatibility of `google-api-python-client`, `google-auth-oauthlib`, `cryptography`, `keyring`, and `platformdirs` here before building later steps on them. Do not start Step 6 until both this step and Step 5 pass with both Google accounts against a shared empty test folder.

**Tasks**

- Add a platform-appropriate configuration location in the UI-independent layer (for example using `platformdirs`). Do not keep app config beside source code.
- Honor `FINANCETRACKER_CONFIG_DIR`. When set, config, local encrypted-copy path, process lock, known-valid cache, and keyring service name must all use that profile. Default production paths apply only when the variable is unset. Tests must set the override.
- Add the required Google API dependencies to `lib_requirements.txt`, using versions compatible with the supported Python/PySide6 environments (including the current Python 3.14 development environment). Use Google's supported Drive API v3 Python libraries.
- Add first-run setup for the OAuth desktop-app credential configuration and the dedicated shared Drive folder. Document how to:
  - enable the Drive API;
  - create desktop OAuth credentials (the **same** client configuration file is copied to both computers; a second client ID is not allowed);
  - configure the consent screen with **User type External**;
  - **publish the app to "In production"** so refresh tokens don't expire after 7 days;
  - what the "unverified app" warning looks like and the exact clicks to proceed, including “Advanced → Go to … (unsafe)”;
  - that Google Advanced Protection or a Workspace-managed account can block unverified apps;
  - create the folder in one account and share it with the other account as Editor.
- Use the full `drive` scope as decided in Section 3, and explain in the setup docs why `drive.file` cannot work with two accounts. Do not build a Google Picker flow.
- Folder setup: the user pastes the folder link or ID; if it is a shortcut, resolve `shortcutDetails.targetId`; the app confirms it can access the folder and shows its name and owner for confirmation before saving the ID to local config.
- Generate and store the per-computer machine ID in local config. After the lock file exists (later steps), also cache its Drive file ID here.
- Store only the refresh token in the OS credential store with `keyring` or a tested platform-equivalent implementation. Do not write it to Drive or to an unprotected token file. Do not store the finance passphrase here. Handle the Windows credential size limit and backend-specific exceptions described in Section 3.
- Do not include client credential JSON, tokens, the folder ID if treated as local configuration, or personal transaction data in source control.

**Test and expected result**

- Unit tests mock OAuth and credential storage; they verify expired credentials are refreshed, missing credentials trigger interactive setup, revoked/expired refresh tokens lead back to interactive sign-in with a clear message, and unavailable secure storage fails safely.
- A test confirms that what is written to the credential store fits within the Windows limit.
- Config tests prove paths resolve to appropriate user directories on Windows/macOS and do not point at the project/source directory by default. When `FINANCETRACKER_CONFIG_DIR` is set, config, cache, local encrypted copy, process lock, and keyring service name all resolve under that directory and do not touch the production profile.
- Dependencies install cleanly in the current environment.

**Manual test for me**

- The agent should give me a setup checklist for the Google Cloud project (including User type External, publishing to "In production", and copying the same client JSON to both computers) and a first-time sign-in test. Expected result: the app opens Google authorization in a browser, the unverified-app warning appears and can be passed on both Windows and macOS, each partner can authorize their own account, and repeat launches reuse the OS-stored refresh token without putting a plaintext token file in the repository or shared Drive. No finance data should be uploaded yet.
- Confirm in the Google Cloud console that the publishing status is "In production" and User type is External. The agent should remind me to re-check sign-in after more than 7 days, since that is when a wrong setting would show up.
- If either account cannot complete authorization, **stop**. Do not start Step 5's real-Drive tests against a broken OAuth setup, and do not start Step 6.

## Step 5 — Implement the Google Drive API adapter

Together with Step 4, this is the **go/no-go gate**. Do not start Step 6 (snapshots, retention, lock manager, session, GUI, restore, or real-data migration) until the manual tests below pass with **both** Google accounts against a shared empty **test** folder, using `FINANCETRACKER_CONFIG_DIR` so nothing touches the live finance folder.

**Tasks**

- Build a Drive adapter behind a small interface, injected into the session/repository layer so unit tests can use a fake implementation.
- Run all adapter calls through the single background worker described in Section 2, with explicit per-request timeouts.
- Support listing direct child items of the configured folder (query must include `trashed = false` and an explicit `fields=` of `id,name,parents,owners,capabilities,createdTime,md5Checksum,trashed`), downloading a file **by ID**, uploading a new uniquely named encrypted snapshot, reading/updating the permanent lock file **by cached ID** (file content, not `appProperties`), and moving a specific file to trash by ID. Do not expose permanent deletion in normal operation.
- After `files.create`, treat the returned file ID as source of truth. Verify or re-fetch with `files.get` of that ID. Folder lists used for generation/conflict checks must retry with backoff and union in any ID this session just created.
- Resolve shortcut folder IDs during setup (`shortcutDetails.targetId`).
- Expose Drive server time (for example from the response `Date` header) for lock timing.
- Restrict every list/update/trash operation to direct children of the configured folder whose names match the app's naming pattern, using the central guard from Section 3. Before every update or trash, re-fetch metadata by ID and require: not trashed, parent is the configured folder, name matches `finance_snapshot_g*.enc` or `finance_lock.json`. Do not touch unrelated Drive content.
- Trashing a file owned by the other account must be reported as "not owner — skipped", not as an error that blocks saving.
- Handle pagination, retries for transient errors, authorization revocation, rate limits, missing folder access, unavailable network, and API errors with useful safe error messages.
- Never treat a failed or timed-out upload as successful. When success is uncertain, `files.get` the created file ID rather than blindly overwriting or trashing anything, and rather than trusting a single immediate `files.list`.
- Avoid logging raw API objects if they could include auth material or file contents.

**Test and expected result**

- Unit tests with a fake Drive service cover create/list/download/update/trash, pagination, API errors, request timeouts, upload timeout/uncertain completion, missing permissions, trash refused for non-owner, and folder filtering.
- The fake Drive models ownership: files belong to the account that created them, and only the owner can trash them.
- Tests assert that an unrelated file outside the configured folder is never touched, and that the adapter never calls permanent delete.
- A test confirms that Drive calls never run on more than one thread at a time.
- A fake-Drive test simulates delayed `files.list` visibility after create: `files.get` of the new ID succeeds, and generation/conflict logic unions that ID rather than treating the save as failed.
- No actual finance snapshot is uploaded in this step.

**Manual test for me**

- Using an empty, dedicated **test** folder shared between both Google accounts, and a `FINANCETRACKER_CONFIG_DIR` that is not the production profile, run a developer-only integration test/script against harmless synthetic test files. Do not add a permanent production UI just for diagnostics unless it has continuing user value. The agent must not use the real finance file or the live finance Drive folder for this test. Run it with both accounts, one per computer or one after the other, and check each expected result:
  1. Account A creates a file. Account B can list it, download it by ID, and update its content.
  2. Account B's attempt to trash A's file is refused and reported as "not owner — skipped". Account A can trash it, and it appears in A's Drive trash.
  3. A lock-style file whose **content** is updated by one account shows the new values to the other account immediately when read **by file ID**.
  4. Nothing outside the test folder is touched.
  5. Disconnecting the network during a call produces a clear error within the timeout instead of hanging.
  6. Immediately after create, `files.get` of the new ID succeeds even if a parent-folder `files.list` is still catching up.

**Go/no-go.** If all of the above pass for both accounts, Step 6 may start. If OAuth, sharing, listing, content updates, or get-by-ID verification fail for either account, **stop and wait**. Do not implement snapshots, lock, session, GUI, or restore on top of an unproven Drive path. Report what failed and the options (Testing-status weekly re-auth, or a later storage-approach change).

## Step 6 — Build encrypted snapshots, generations, and tiered retention

Start this step only after the Step 4/5 go/no-go gate has passed.

**Tasks**

- Create a snapshot repository that serializes the in-memory store to canonical JSON bytes, encrypts those bytes (authenticated header includes generation, creation time, record counts, and `next_id`), uploads a new file, then `files.get`s that ID, downloads, decrypts, and compares **byte-for-byte** to the intended bytes before parsing.
- Implement generation numbering, the base-generation check before upload, and the conflicting-save check after upload (Section 3), using list-with-retry plus union of known created IDs. A generation-mismatch refusal keeps the unsaved change and offers reload-discard versus keep-and-retry.
- On startup, examine snapshots newest-first by generation and load the newest snapshot that decrypts and passes schema validation. A corrupt newest item must not prevent recovery from an older valid snapshot. Report skipped corrupt files. Do not decrypt the whole history just to build a list; use authenticated headers plus the known-valid cache.
- Implement tiered retention (Section 3): 20 most recent plus one per day for 30 days, never fewer than 5 known-valid remaining.
  - Only prune after a new version is verified.
  - Prune by trashing, and only files owned by the current account that are **already** in the known-valid cache. Never block a save on verifying uncached history.
  - A failed upload or failed verification must leave all previous good versions intact.
  - Do not count an invalid file as a good backup, and never trash it automatically.
- Implement the local known-valid cache keyed by file ID and `md5Checksum`, so each snapshot is downloaded and decrypted at most once per computer unless its checksum changes.
- Write the local encrypted copy after each verified save and after startup load; keep the most recent 10.
- Do not silently create a blank database if files exist but none can be decrypted or validated. Present a clear choice to retry the passphrase, inspect/recover, or cancel.
- Avoid creating a duplicate snapshot when the current document hasn't changed since the last successful save.

**Test and expected result**

- Fake-Drive tests verify first save, successive saves, reload, and clean behavior when fewer than the retention numbers exist.
- Fault-injection tests simulate failed upload, corrupt uploaded bytes, failed read-back, failed decryption, bad JSON, and trash failure. Previous known-good snapshots always survive a failed save.
- Test 25 successful same-day snapshots and assert that the 20 newest valid ones remain. With a fake clock spanning several days, assert that the newest snapshot of each day within 30 days is also kept.
- Ownership tests: snapshots owned by the other account are never trashed by this account and are pruned later by their owner. Leftover snapshots don't break retention.
- Generation tests: a save is refused when the newest Drive generation differs from the base generation, and the unsaved change remains in memory with reload-discard versus keep-and-retry. Two snapshots with the same generation trigger the conflict warning, and both are kept. A list that omits a just-created file ID still treats that ID as present (fake Drive can delay list visibility).
- Byte-comparison tests: verification compares canonical JSON bytes, not re-parsed record objects.
- A filename/header generation mismatch makes a snapshot invalid.
- Known-valid cache tests: a cached snapshot with an unchanged checksum is not downloaded again; a changed checksum forces re-verification. A save with fewer than 5 cached known-valid files does not decrypt the rest of the folder in order to prune; uncached extras are left in place.
- Test a corrupt newest snapshot and assert that the app selects the next newest valid version without trashing it.
- The local encrypted copy contains only encrypted bytes and is capped at 10 files.

**Manual test for me**

- On an empty test Drive folder, the agent provides a developer script that creates synthetic snapshots (never the real data). Check each expected result:
  1. After 22 saves from one account, 20 valid encrypted snapshots remain, and the two oldest are in that account's Drive trash.
  2. After a few saves from the second account, the second account's app does not trash the first account's files.
  3. Each remaining snapshot opens/decrypts through the app.
  4. Files in Drive do not reveal synthetic transaction text.
  5. The local encrypted copy folder contains only encrypted files.

## Step 7 — Implement the separate lock and stale-lock recovery

Start this step only after the Step 4/5 go/no-go gate has passed.

**Tasks**

- Add a lock manager using the permanent Drive lock file **content** (Section 4). Cache the lock file ID in local config and read/write by that ID. Do not use `appProperties`. Do not alter the encrypted snapshot format to indicate “blocked”, and never delete the lock file in normal operation.
- Implement acquisition with the confirmation re-read, duplicate/missing lock detection, periodic heartbeat with read-before-write token check, pre-save ownership checks, safe release (set to `free`), and user-confirmed stale-lock recovery as specified above.
- Base all lock timing on Drive server time.
- Implement the local OS-level process lock and same-computer crash takeover (Section 4, item 7). Use a small cross-platform approach that works on Windows and macOS without Qt in the core layer.
- Implement sleep/long-gap detection (Section 4, item 8) and the idle-release/re-acquire logic (item 11) in the UI-independent layer; the GUI only reports user activity and shows the state.
- Use a cryptographically random per-session token. A lock holding another session's token must never be modified during heartbeat or release.
- Keep the heartbeat timer in an appropriate session/controller layer; don't bury sync logic in widgets.
- Use clear lock errors: active session (with read-only option), stale lock requiring confirmation, duplicate/missing lock file, network unavailable, or lock ownership lost.
- If lock ownership can't be verified, disable add/edit/delete and require the user to re-establish a valid session.

**Test and expected result**

- Unit tests cover:
  - successful acquire/release, and losing the acquisition race (the confirmation re-read shows another token);
  - a fresh lock blocking editing but allowing read-only, and stale-lock confirmation;
  - a lock that changed between review and recovery, and duplicate or missing lock files;
  - heartbeat update, and a heartbeat that finds another token (ownership lost, nothing overwritten);
  - saving with a matching token, and refusal to save with a different token or a stale last heartbeat;
  - refusal to modify another session's lock on release;
  - same-computer crash takeover when the local process lock is free, and refusal when it is held;
  - a long-gap/sleep forcing re-verification;
  - idle release only when clean, and re-acquire with reload when the generation changed.
- Fake-clock tests cover timestamp/expiry behavior, including the two computers' local clocks disagreeing by several minutes; tests do not sleep for real minutes.
- Lock failures never overwrite or trash valid finance snapshots.

**Manual test for me**

- Start one app session against a test folder, then try opening a second session. Expected result: the second session offers read-only viewing and reports that the lock is held. Close the first session and retry; the second can then acquire the lock.
- Kill the app process (Task Manager / Activity Monitor) and restart it on the same computer. Expected result: it offers to take over its own lock immediately.
- Simulate an abandoned lock from another machine ID and confirm the app requires explicit confirmation before taking it over.
- Leave a session idle past the idle-release time. Expected result: it switches to read-only "paused" and the lock shows `free`.
- Test the two real machines later in Step 11.

## Step 8 — Add the shared session/controller and one-time migration

Start this step only after the Step 4/5 go/no-go gate has passed.

**Tasks**

- Integrate the passphrase prompt, OAuth/Drive, lock manager, snapshot repository, local encrypted copy, and in-memory `FinanceStore` behind a session/controller used by the GUI. Keep it UI-independent, so the later CLI plan can reuse it unchanged.
- Startup order must be explicit and safe:
  1. config;
  2. OAuth;
  3. passphrase;
  4. load the newest valid snapshot read-only (this also validates the passphrase, and no lock is held while the user retries a wrong one);
  5. try to acquire the lock;
  6. if acquired, re-check the newest generation and reload if it changed, then allow edits; if not acquired, stay read-only.

  If Drive is unreachable, offer read-only viewing from the local encrypted copy. The exact order may be adjusted for usability, but no finance mutation may be possible without confirmed lock ownership and a loaded valid store.
- The session has explicit modes: **editable** (lock held), **read-only, other person editing**, **paused** (lock released after idle), **offline read-only** (local encrypted copy), and **access lost**. Each mode states whether mutations are allowed; only editable allows them.
- Handle initial data setup and plaintext migration according to Section 5 above, including the double passphrase entry, creating the permanent lock file, caching its Drive ID, and the one-time notice when a legacy file exists alongside cloud data. Inspect the current `default_data_path()` before changing it: the existing legacy file is documented as living beside the source code. Migration must explicitly find/accept that legacy path (and any currently configured path) as a read-only import source. Once cloud mode is enabled, the legacy path must never again be a save destination. Do not write local cloud-mode config until both the lock file and a verified generation-1 snapshot exist; retries must reuse an existing lock file.
- Track the last successfully committed document, its generation (the base generation), and a dirty/unsaved state. Ensure all mutations eventually call the same `save()` path.
- Avoid exposing a local path that the GUI can accidentally use to save plaintext after cloud mode is configured.
- **Temporary CLI block.** At the very start of `cli.py`, before it loads any data, check whether cloud mode is configured on this computer. If it is, print a short message saying the CLI does not support synced data yet, and exit with a non-zero code without reading or writing the legacy file. Without cloud mode, the CLI keeps working on the local file exactly as today. This is the only CLI change in this plan; the separate CLI plan removes the block.
- Keep the session controller UI-independent where practical; inject fake collaborators for tests.

**Test and expected result**

- Session tests cover:
  - successful cloud startup, a wrong passphrase (no lock held during retry), and no access to the folder;
  - a lock already held (read-only mode offered), no valid snapshot, and a valid older snapshot with a corrupt newest file;
  - Drive unreachable (offline read-only from the local copy, mutations refused);
  - a generation changed between load and lock acquisition (reload);
  - first-time explicit empty-dataset creation, migration success/failure, and a mismatched double passphrase entry;
  - idle pause and resume;
  - shutdown with a clean/dirty session.
- Migration tests verify source plaintext is untouched on every failure, that a dated local backup is created before upload, that cloud mode is not enabled until lock + verified generation-1 snapshot both exist, and that a retry reuses an existing lock file instead of creating a duplicate.
- A successful migration verifies an encrypted remote snapshot can be downloaded/decrypted into the same schema-v2 document as the source.
- A code search confirms normal cloud-mode saves no longer write to the legacy data path.
- CLI block tests: with cloud mode configured, every `cli.py` command exits non-zero and leaves the legacy file untouched (including not creating one); without cloud mode, the existing commands behave as before.

**Manual test for me**

- First use only a copy of the current finance file with synthetic data, under `FINANCETRACKER_CONFIG_DIR`, against the test Drive folder. Expected result: the app explains migration, asks for the passphrase twice, makes a local plaintext backup outside the shared folder, writes an encrypted verified cloud snapshot (generation 1) and the lock file, and only then enables cloud mode and reloads equivalent data. With a deliberately wrong folder ID or failed upload, migration fails without modifying the source and without enabling cloud mode. The real finance file should not be migrated until this passes.
- Disconnect the network and start the app. Expected result: it offers read-only viewing from the local encrypted copy, clearly labeled with the data's generation/time, and editing is disabled.
- After the synthetic migration, run `python cli.py summary 2026 10`. Expected result: it refuses with a message that the CLI doesn't support synced data yet, and the local test file is unchanged.

## Step 9 — Integrate the GUI: startup, autosave, errors, and shutdown

Start this step only after the Step 4/5 go/no-go gate has passed.

**Tasks**

- Make the main window use the shared session/controller rather than loading `FinanceStore` directly from `default_data_path()`.
- Prompt for the encryption passphrase on every launch. Avoid storing it in settings, logs, crash reports, or clipboard helpers.
- Add a clear sync/save status indicator, for example “Connected — saved” / “Saving…” / “Changes not saved” / “Read-only — Milena is editing on MacBook” / “Paused — click to resume editing” / “Offline — read-only, data from <time>” / “Access lost — editing disabled”. Don't overdecorate the existing UI.
- In every read-only mode, hide or disable Add/Edit/Delete, and offer the matching action ("Try again", "Resume editing", or "Reconnect"). Browsing, totals, and the chart keep working.
- Report user activity (keyboard/mouse in the app) to the session for idle release. When the user tries to edit while paused, re-acquire first; if the data changed on the other computer, reload and tell the user before opening the dialog.
- Saves run on the background worker. While a save is in progress, show “Saving…”, keep the window responsive, and don't allow another mutation to start.
- After each successful add/edit/delete, write and verify a new encrypted snapshot before reporting the change as saved. If cloud save fails while lock ownership remains confirmed, keep the change in memory as dirty, display a clear warning, and provide an explicit retry path. If lock ownership is lost/unverifiable, disable further mutations. If the save is refused because the data changed on the other computer, explain that, keep the unsaved change visible, and offer reload (discard) versus keep-and-retry after they inspect.
- Ensure UI error paths don't show success notifications or close dialogs as if the cloud save succeeded. The UI may keep a changed record visible while unsaved, but must make that state conspicuous and must never imply it is safely synced.
- On close: if a save is in flight, wait/cancel/confirm; do not release the lock from under an in-flight upload. If clean, release the lock safely and exit. If dirty, offer Retry Save, Discard Unsaved Changes (restoring the last committed state for shutdown), or Cancel. Do not release the lock if close is cancelled or a dirty save is still pending.
- Handle the quit paths that bypass a normal window close: Cmd+Q / the app menu on macOS, and Windows shutdown/logoff. Make a best-effort lock release (for example on `aboutToQuit`) when clean. Where the OS doesn't allow a prompt, accept that the lock is left behind; the same-computer takeover from Step 7 then recovers it on the next start.
- Don't create a second redundant snapshot on close if all changes already saved successfully.
- Preserve existing add/edit/delete confirmations, chart functionality, month behavior, and financial calculations.

**Test and expected result**

- Automated tests or Qt-level tests cover status changes for every mode, save success, save failure, a save refused due to a generation change, lock loss disabling edits, idle pause/resume, and dirty-close choices where practical.
- Existing GUI smoke tests and core tests still pass.
- Each add/edit/delete produces exactly one new verified snapshot; a no-op close doesn't add a duplicate.
- A simulated Drive outage does not claim success and does not destroy the prior snapshot.

**Manual test for me**

- Against a test dataset and Drive folder, add a synthetic transaction, edit it, and delete it. Each successful action should create one verified new snapshot, the window should stay responsive while saving, and the UI should indicate it is saved.
- Simulate network failure during save. Expected result: a visible unsaved warning and a working retry/cancel path. Confirm that closing cannot silently discard pending changes.
- Quit with Cmd+Q on macOS (and close normally on Windows) with no unsaved changes. Expected result: the lock is released, or is recovered immediately on the next start on the same computer.

## Step 10 — Add backup browsing and restore

Start this step only after the Step 4/5 go/no-go gate has passed.

**Tasks**

- Add a recovery view in the GUI that lists valid snapshots from Drive and from the local encrypted copy. Put the listing and restore operations in the session layer, not in the dialog, so the later CLI plan can reuse them. Show generation, timestamp, which computer/account saved it, and safe metadata such as record counts, using the authenticated header and Drive `owners` without decrypting every file and without exposing transaction notes/details. Mark any snapshot involved in a detected conflicting save.
- List files that failed validation separately, so the user can see them, but do not offer them for restore.
- Allow the user to select a previous valid snapshot and restore it safely. Prefer creating a **new latest snapshot whose contents equal the selected older version** (with the next generation number), rather than trashing/overwriting other snapshots. This retains a clear audit/recovery trail and still respects the retention policy.
- Restoring from the local encrypted copy works the same way: it is uploaded as a new snapshot, which also covers the case where the Drive folder's contents were lost.
- Require confirmation before restoring. Verify selected snapshot decrypts, parses, and matches the expected schema before use.
- Refuse restore while the session has unsaved changes, or require an explicit discard first. Never let restore bypass lock ownership. Keep a copy of the currently committed latest snapshot until the restore snapshot is verified.
- If the newest snapshot is corrupt, report that it was skipped and allow loading/recovering the newest valid backup. Do not automatically overwrite cloud data just because a read failed.

**Test and expected result**

- Tests cover listing newest-first without decrypting every snapshot for counts, filtering invalid snapshots, refusing restore while dirty, restoring an older version, restoring from the local encrypted copy, failed restore upload, and retention after restore.
- A failed restore must leave the current latest valid version and all previously valid backups intact.

**Manual test for me**

- Create several synthetic snapshots, restore an older one, and verify that the selected data becomes current through a newly verified encrypted snapshot. Confirm that the prior latest version remains available as a backup unless it naturally falls outside the retention policy after verification.
- Point the app at a new, empty test folder on a computer that has a local encrypted copy. Expected result: the startup screen offers to restore from the local copy alongside creating an empty dataset. Restoring creates the lock file and a new verified snapshot with the data.

## Step 11 — Cross-platform integration, migration of real data, documentation, and acceptance

Start this step only after the Step 4/5 go/no-go gate has passed. Real-data migration is last, and only after synthetic end-to-end tests on both platforms.

**Tasks**

- Update README and setup documentation for Windows and macOS, covering:
  - actual dependencies (`lib_requirements.txt`);
  - Google OAuth setup, including User type External, the same client JSON on both computers, publishing to "In production", the unverified-app warning clicks, and Advanced Protection / Workspace caveats;
  - why the full `drive` scope is used;
  - shared-folder preparation/permissions and file ownership (each account owns, and prunes, its own snapshots);
  - secure credential storage;
  - encryption passphrase usage, including that a forgotten passphrase is unrecoverable and that changing it is not supported;
  - first-time migration;
  - backups and retention (including Drive trash and the local encrypted copy);
  - lock behavior (read-only while the other person edits, idle release, same-computer crash takeover, stale-lock recovery);
  - offline/error behavior;
  - recovery;
  - that the CLI is unavailable once sync is set up, until the separate CLI plan is implemented.
- Correct README references to the dependency file (`lib_requirements.txt`) if still incorrect.
- Ensure `.gitignore` excludes local app secrets/config, credential JSON, OAuth tokens if any legacy files are created during setup, test artifacts, decrypted dumps, and local finance files. Do not ignore useful source templates or test fixtures.
- Run the full automated suite and static/code-quality checks already used by the repository. Add no unneeded framework or CI setup unless it is a small natural fit.
- Perform manual integration tests on Windows 11 and macOS. Test both partner Google accounts against the same dedicated shared folder.
- Only after the synthetic end-to-end migration is verified on both platforms, guide the user through migration of the real plaintext dataset. Preserve the dated plaintext backup until the user confirms cloud data and recovery work; clearly warn that it is plaintext and should be stored safely.
- Inspect the shared Drive folder and confirm it contains only encrypted data snapshots and the non-sensitive lock/metadata files. Never paste or print real transactions into the report.

**End-to-end acceptance checklist**

- [ ] Existing finance behavior still works: person tabs, month selection, add/edit/delete, totals, tables, and spending chart.
- [ ] The same latest data appears when opening on Windows and then on macOS, sequentially.
- [ ] Each successful add/edit/delete creates a verified encrypted snapshot with the next generation number.
- [ ] Retention keeps the 20 newest valid snapshots plus one per day for 30 days. Each account trashes only its own snapshots, and nothing is permanently deleted.
- [ ] A wrong passphrase does not create an empty dataset or alter files.
- [ ] Corrupted newest data falls back to a prior valid version and can be restored.
- [ ] A failed upload leaves prior good data and backups intact, and the UI clearly indicates unsaved state.
- [ ] The app will not edit when another session holds the lock or lock ownership can't be confirmed, but offers read-only viewing.
- [ ] A save based on outdated data (newer generation in Drive) is refused, and the unsaved change is not lost silently.
- [ ] An idle session releases the lock automatically, and resuming reloads newer data from the other computer.
- [ ] After a crash, the same computer can take back its own lock immediately; abandoned locks from the other computer can be recovered only with clear warning/confirmation after checking the other app is closed.
- [ ] A laptop that slept while holding the lock cannot save until ownership is re-verified.
- [ ] With no network, the app opens read-only from the local encrypted copy and refuses edits.
- [ ] Normal cloud-mode operation does not write plaintext finance JSON to the source directory or a Drive folder.
- [ ] OAuth refresh tokens are stored in the local OS credential store and never in shared Drive/source control. Sign-in still works more than 7 days after authorization.
- [ ] The window stays responsive during saves and network problems.
- [ ] Once cloud mode is configured, `cli.py` refuses to run with a clear message and never reads or writes the legacy plaintext file.
- [ ] Setup and recovery instructions are understandable and tested on both operating systems.

**Manual test for me**

The agent must provide a concise final runbook of exact commands and a user-performable checklist, with expected outcomes. Separate tests that the agent actually ran from tests that require my Windows PC or the MacBook. Do not claim cross-platform success without actually testing both systems.

---

## 7. Required reporting format after every step

Use this exact structure in the agent's progress report:

1. **Step completed:** number and title.
2. **Changes made:** files/modules changed and brief explanation.
3. **Automated tests:** exact command(s), pass/fail summary, and failures not fixed.
4. **What I should test now:** numbered actions I can perform, with expected result for each.
5. **Known limitations / decision needed:** be explicit; write “None” if there are none.
6. **Stop point:** state that the agent is waiting for my review before proceeding.

If a step reveals a major design or security problem, stop at that step and explain the problem and options. Do not silently change the agreed architecture. After Step 5, if the shared-folder OAuth/Drive tests fail for either Google account, treat that as a gate failure: do not start Step 6.

## 8. Useful official references

- Google Drive API Python quickstart: <https://developers.google.com/workspace/drive/api/quickstart/python>
- Google Drive API authorization scopes and least-privilege guidance: <https://developers.google.com/workspace/drive/api/guides/api-specific-auth>
- Google Drive API file search and folder queries: <https://developers.google.com/workspace/drive/api/guides/search-files>
- Google Drive API v3 reference: <https://developers.google.com/workspace/drive/api/reference/rest/v3>
- Python `cryptography` documentation: <https://cryptography.io/en/latest/>
- Google Drive `files` resource (ownership, `trashed`, `capabilities`, `md5Checksum`): <https://developers.google.com/workspace/drive/api/reference/rest/v3/files>
- OAuth consent screen publishing status and the 7-day Testing token expiry: <https://support.google.com/cloud/answer/10311615>
- OAuth app states (unverified production apps, user cap): <https://developers.google.com/identity/protocols/oauth2/production-readiness/overview>
- google-api-python-client thread safety: <https://googleapis.github.io/google-api-python-client/docs/thread_safety.html>
- Drive custom file properties (`properties` vs `appProperties`, 124-byte limit): <https://developers.google.com/workspace/drive/api/guides/properties>
- Restricted-scope verification and the personal-use exception: <https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification>
- Drive shortcuts (`shortcutDetails.targetId`): <https://developers.google.com/workspace/drive/api/guides/manage-shortcuts>
