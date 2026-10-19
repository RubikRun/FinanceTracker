# FinanceTracker: Secure Sync for the CLI — Step-by-Step Implementation Plan

## Before starting

This plan follows `FinanceTracker_SecureSync_ImplementationPlan.md` (the "GUI sync plan"), which adds encrypted Google Drive sync to the GUI only. **Do not start this plan until the GUI sync plan is fully implemented and accepted.**

This plan was written before the sync code existed. References such as "GUI sync plan, Section 4" point to that document as it was written. Step 0 below re-checks every assumption here against the actual code, and the plan must be adjusted wherever the implementation differs.

## Instructions to the coding agent

Implement this plan **one numbered step at a time, in order**. At the end of every step, report using the format in Section 7 of the GUI sync plan, then **stop and wait for review** before starting the next step.

Make the smallest cohesive change that completes the current step, and keep both the GUI and the CLI runnable after each step. Source code is the authority for current behavior; this document is the authority for the new CLI requirements; the GUI sync plan remains the authority for sync, lock, encryption, and backup rules.

Never use the real finance data for tests, demos, or fixtures. Use synthetic records, temporary directories, and a dedicated test Drive folder. Do not print, log, commit, or paste real transaction data.

## 1. Goal and requirements

After the GUI sync plan, `cli.py` refuses to run once cloud mode is configured (GUI sync plan, Step 8). This plan makes the CLI work with synced data again, through the same session layer as the GUI.

- **Same data operations as the GUI.** The CLI must support every data operation the GUI supports (Section 2), with the same validation rules and the same results.
- **Convenience does not need to match.** Each command is a separate run that asks for the passphrase every time. The CLI uses terminal prompts instead of dialogs and text output instead of a chart. There is no interactive or long-running CLI mode.
- **No second implementation.** The CLI calls the same config, OAuth, passphrase, lock, snapshot, retention, and recovery services as the GUI. Do not duplicate encryption, Drive, lock, or snapshot logic in `cli.py`. Logic the CLI needs that currently lives only in GUI code moves into the `finance` core or the session layer, and the GUI is switched to use it too.
- **Security rules from the GUI sync plan apply unchanged.** Ask for the passphrase without echo, and never accept it as a command-line argument or environment variable. Never write decrypted finance JSON to disk. Printing the user's own records to their terminal is intended output, like the GUI's tables, but logs and error messages must not contain transaction data.
- **Each run** signs in by reusing the stored refresh token (or runs the browser sign-in if needed), asks for the passphrase, performs one operation, and exits.
- **Read-only commands** load the newest valid snapshot without taking the lock, so they work while the GUI is open on either computer. If Drive is unreachable, they fall back to the local encrypted copy and print a clear "Offline: data from generation N, <time>" header first.
- **Mutating commands** acquire the lock, load the newest valid snapshot, apply the change, save and verify exactly one snapshot, and release the lock in a `finally` block (including on Ctrl+C).
  - If the Drive lock is held by another session, or another FinanceTracker process is running on this computer (GUI sync plan, Section 4, item 7), fail with a clear message naming the holder. This applies even if the GUI on this computer is paused.
  - Stale-lock recovery, same-computer crash takeover, and the missing/duplicate lock file cases work as in the GUI, with a terminal prompt showing the same information and asking for the same confirmation.
  - The session's heartbeat keeps running while the CLI waits at a prompt, so a slow answer doesn't let the lock go stale.
  - If a save fails, offer Retry or Discard before exiting. On Discard, nothing is saved and the exit code is non-zero. Never report a change as saved unless it was verified.
  - If the save is refused because the newest generation changed, say so and exit non-zero without saving.
- **Confirmations match the GUI.** Delete shows the record type, owner, amount, date, and (for spending) category, and asks y/N; `--yes` may skip this one prompt. Restore, migration, new-dataset creation, and stale-lock takeover always ask, with no skip flag. Creating a dataset or migrating asks for the passphrase twice.
- **Exit codes.** Exit with a non-zero code on any failure.
- **Shared validation** (moved into the `finance` core, used by both GUI and CLI):
  - the amount must be a number greater than 0, and both `14.20` and `14,20` are accepted;
  - a new spending record, or an income record converted to spending, needs a category from `CATEGORIES`;
  - editing a spending record may keep its existing category even if it is no longer in `CATEGORIES`, as the GUI allows today.

  This deliberately makes the CLI stricter than today, since it currently accepts zero or negative amounts and any category name.

## 2. Parity table

Suggested command names; names and argument styles may be adapted, but every row must be covered. If Step 0 finds that the GUI gained or lost a data operation, update this table first.

| GUI data operation | CLI command |
| --- | --- |
| First-time Google sign-in and folder setup | `setup` |
| Create a new empty dataset | `create-dataset` |
| Migrate the legacy plaintext file | `migrate [PATH]` |
| Add spending or income for a person | `add-spending USER AMOUNT CATEGORY [DATE] [--note]`, `add-income USER AMOUNT [DATE] [--note]` (existing) |
| Edit a record, including converting between spending and income | `edit ID [--type spending\|income] [--amount] [--category] [--date] [--note]`; omitted fields keep their current values; ID, owner, and recording date are preserved |
| Delete a record | `delete ID [--yes]` |
| Months that have data, per person or both | `months [--user]` |
| Records for a month, per person or both | `list YEAR MONTH [--user]` (existing; sort by date then ID, as the GUI does) |
| Earned, spent, and gap for a month | `summary YEAR MONTH [--user]` (existing) |
| Spending chart: category totals, percentages, and the Boris/Milena split | `categories YEAR MONTH [--user]` (text table) |
| Status: signed-in account, folder, newest generation, who holds the lock | `status` |
| Stale-lock recovery and same-computer takeover | prompted by any mutating command |
| List backups, including invalid files and conflict markers | `backups` |
| Restore a backup from Drive or the local encrypted copy | `restore SNAPSHOT [--from-local]` |
| Offline read-only viewing | automatic fallback in read-only commands |

## 3. Implementation steps

## Step 0 — Preflight against the implemented sync code

**Tasks**

- Inspect the implemented session layer, lock manager, snapshot repository, recovery operations, local encrypted copy, configuration/test-configuration mechanism, the GUI's data operations, the current `cli.py`, and the temporary CLI block.
- Compare them with this plan. Report material differences, for example a different session API, operations that ended up inside GUI code, a changed lock or retention rule, or GUI data operations missing from the parity table.
- List the GUI-only logic that must move into the core or session layer. Expected candidates: input validation, the record details shown before a delete, the category breakdown with percentages and per-person split, and recovery decisions.
- Propose any changes to this plan before changing code. Do not change product behavior in this step.

**Test and expected result**

- The full automated suite from the GUI sync plan passes as a baseline.

**Manual test for me**

- None beyond reviewing the report and any proposed plan changes.

## Step 1 — Move shared rules into the core

**Tasks**

- Move the validation rules from Section 1 into the `finance` core and switch the GUI's add/edit dialog to use them, with unchanged GUI behavior.
- Move any other GUI-only logic identified in Step 0 into the core or session layer, and switch the GUI to use it.
- Apply the shared validation to the CLI's existing add commands too (they still run against the local file when cloud mode is not configured).
- Update the usage examples in `cli.py`; one still uses the old `Food` category.

**Test and expected result**

- Core validation tests cover the amount rules (comma decimal, zero, negative, non-numeric) and the category rules (new spending, income-to-spending conversion, legacy category kept on edit).
- GUI tests confirm the add/edit dialog, delete confirmation, and chart behave as before.
- The full suite passes.

**Manual test for me**

- In the GUI, try adding `0`, `-5`, `abc`, and `14,20`, and edit a record that has a category no longer in the list. Expected result: the same behavior as before this step.

## Step 2 — Read-only commands through the session layer

**Tasks**

- Add the CLI's session startup: sign-in reuse, passphrase prompt without echo, and loading the newest valid snapshot without the lock, with the offline fallback and header.
- Implement or convert `summary`, `list`, `months`, `categories`, and `status` (Section 2).
- Lift the temporary CLI block for these read-only commands only. Mutating commands stay blocked with the existing message.

**Test and expected result**

- Fake-Drive tests cover success, wrong passphrase, Drive unavailable (offline fallback with header), and no valid snapshot for each read-only command.
- Tests confirm no read-only command takes or modifies the lock.
- Output tests confirm `summary` and `categories` report the same totals, percentages, and per-person split as the GUI's summary and chart for the same synthetic data, and `list` sorts by date then ID.
- Mutating commands are still refused in cloud mode.

**Manual test for me**

- Against a synthetic test folder, check each expected result:
  1. With the GUI open on the other computer, `summary`, `list`, `months`, `categories`, and `status` work and match what the GUI shows; `status` names the lock holder.
  2. With the network disconnected, `summary` prints the offline header.
  3. `add-spending` is still refused with the temporary message.

## Step 3 — Mutating commands: add, edit, delete

**Tasks**

- Implement `add-spending`, `add-income`, `edit`, and `delete` through the session layer with the lock behavior from Section 1: release in `finally` (including Ctrl+C), refusal when the lock is held or another local FinanceTracker process is running, stale-lock and crash-takeover prompts, missing/duplicate lock handling, heartbeat during prompts, Retry/Discard on save failure, and refusal on a changed generation.
- `edit` keeps the record's ID, owner, and recording date, and keeps unspecified fields unchanged. Converting to spending requires a category.
- `delete` shows the same details as the GUI and asks y/N unless `--yes` is given.
- Lift the temporary CLI block for these commands.

**Test and expected result**

- Fake-Drive tests cover, for each mutating command:
  - success with exactly one verified snapshot and the lock `free` afterwards;
  - the lock held by another session, and another FinanceTracker process running locally;
  - the stale-lock prompt accepted and declined, and same-computer crash takeover;
  - save failure with Retry and with Discard, and a refused save after a generation change;
  - the lock released after failure and after Ctrl+C;
  - a slow answer at a prompt not letting the lock go stale (fake clock).
- `edit` tests cover ID/owner/recording-date preservation, type conversion in both directions, and the category requirement. Declining the delete confirmation leaves data unchanged.

**Manual test for me**

- Against a synthetic test folder, check each expected result:
  1. With the GUI open, `add-spending` is refused with a message naming the lock holder.
  2. With the GUI closed on both computers, `add-spending`, `edit` (including converting income to spending), and `delete` each create exactly one verified encrypted snapshot, and the lock shows `free` afterwards. Reopening the GUI shows the same data.
  3. `add-spending` with an amount of `0` or an unknown category is refused with the same rule the GUI applies.
  4. With the network disconnected, `add-income` is refused.
  5. Pressing Ctrl+C at the delete confirmation leaves the data unchanged and the lock `free`.

## Step 4 — Setup, dataset creation, migration, backups, and restore

**Tasks**

- Implement `setup`, `create-dataset`, `migrate [PATH]`, `backups`, and `restore SNAPSHOT [--from-local]` using the same session operations and rules as the GUI, including the GUI sync plan's Section 5 migration safeguards, double passphrase entry, and confirmations.
- `backups` shows the same safe metadata as the GUI recovery view and lists invalid files separately.
- Remove the temporary CLI block code entirely.

**Test and expected result**

- Fake-Drive tests cover each command's success and failure paths, matching the GUI tests for the same operations: migration leaves the source untouched on every failure and creates the dated backup first; restore creates a new verified snapshot and respects retention; a failed restore leaves all valid backups intact.
- A test confirms the temporary block no longer exists and every command goes through the session layer.

**Manual test for me**

- Using a separate test configuration (`FINANCETRACKER_CONFIG_DIR` as defined in the GUI sync plan, so this never touches the production profile, keyring slot, or live finance folder), check each expected result:
  1. `setup` completes sign-in and folder confirmation.
  2. `migrate` imports a synthetic copy of a finance file into an empty test folder.
  3. `backups` lists the snapshots the GUI recovery view shows.
  4. `restore` makes an older snapshot current as a new verified snapshot.
  5. Restoring with `--from-local` into a new empty test folder works.

## Step 5 — Parity check, documentation, and acceptance

**Tasks**

- Add a parity test that lists the expected subcommands from Section 2 and fails if any is missing from the CLI parser.
- Update the README with every CLI command, the passphrase prompt on every run, lock behavior, and offline behavior. Remove the note that the CLI is unavailable with sync.
- Run the full automated suite.
- Perform manual tests on Windows 11 and macOS with both Google accounts against the same test folder.

**Acceptance checklist**

- [ ] Every row of the parity table works from the CLI on both computers, with the same validation rules and results as the GUI.
- [ ] Read-only commands work while the GUI is open on either computer and never touch the lock.
- [ ] Mutating commands are refused while the lock is held or another FinanceTracker process runs on the same computer.
- [ ] Each mutating command creates exactly one verified encrypted snapshot, and the lock is `free` afterwards, including after a failure or Ctrl+C.
- [ ] The passphrase is never accepted as an argument or environment variable, and no command writes decrypted JSON to disk.
- [ ] A code search confirms the CLI and GUI share the same session layer, `cli.py` contains no encryption/Drive/lock logic of its own, and the temporary block is gone.

**Manual test for me**

The agent must provide a concise runbook of exact CLI commands and a checklist with expected outcomes. Separate tests that the agent actually ran from tests that require my Windows PC or the MacBook. Do not claim cross-platform success without actually testing both systems.
