# FinanceTracker: Product and Technical Context

> Self-contained handoff document for humans and AI assistants that do not have access to the repository.
>
> Repository state documented: product source matches commit `1c63c72a5c075bb22a4b7c1c8d3dffa9450c9ad7` (`In spending chart below each category show how much each person spent`), inspected on 2026-10-13. This handoff file is `AGENTS.md`. The `planning/` directory was added in later documentation commits. The private contents of `finance_data.json` are deliberately not reproduced here.

## 1. Executive summary

FinanceTracker is a small desktop application that Boris and Milena use to track their own household finances. It is not intended to become a public product, hosted service, multi-tenant system, or general-purpose accounting package. The names of the two users and several assumptions about their workflow are intentionally built into the application.

The practical goal is to record income and spending, understand spending habits, and see the gap between income and spending. One partner has comparatively regular salary income and the other has irregular freelance income, so the application records individual ownership while also providing a combined household view.

The application is:

- A local Python desktop program with a Qt GUI implemented using PySide6.
- Intended to run on Windows and macOS.
- Backed by one human-readable JSON file rather than a database.
- Usable by either partner separately or in a combined view.
- Organized around calendar months in the current UI.
- Also accessible through a small command-line client.
- Deliberately simple: there is no server, login, account system, network API, synchronization engine, installer, or database.

The most important architectural rule is that the GUI and CLI do not manipulate JSON directly. They both call the UI-independent `finance` package, primarily `FinanceStore`. This keeps persistence, models, and queries reusable from scripts or future front ends.

The private data model is currently version 2. Every record belongs to exactly one of the two configured people. Spending and income share one global numeric ID sequence.

## 2. Product intent and scope

### 2.1 Intended users

The only intended users are Boris and Milena. The `user` field is a record-owner label, not an authenticated identity. Selecting a person in the UI does not log that person in; it only filters records and controls the owner automatically assigned to a new record.

The product was originally specified for one Windows laptop and one MacBook. Cross-platform behavior therefore matters, but enterprise deployment and broad operating-system support do not.

### 2.2 Problems it solves

FinanceTracker currently answers questions such as:

- How much did Boris earn, spend, and retain in a given month?
- How much did Milena earn, spend, and retain in a given month?
- What were the combined household totals for that month?
- Which spending categories account for the most spending?
- Within a category, how much was spent by each person?
- What individual income and spending entries make up those totals?

The application defines:

```text
gap = total income - total spending
```

A positive or zero gap is shown in green and a negative gap in red. “Gap” is a period cash-flow measure. It is not a bank balance, net worth, accounting profit, savings account balance, or reconciliation against financial institutions.

### 2.3 Current non-goals

These are outside the current implementation:

- Public or third-party use.
- Authentication, authorization, or separate passwords.
- A web application, mobile application, or hosted backend.
- Automatic bank feeds or statement reconciliation.
- Multiple currencies or currency conversion.
- Budgets, recurring transactions, transfers, accounts, assets, liabilities, or taxes.
- Concurrent editing or conflict resolution.
- Yearly and arbitrary custom-period controls in the UI.
- Encryption of the data file.
- General support for any number of users.

Some of these have appeared as possible future ideas, but they should not be assumed to be committed requirements.

## 3. Current user experience

### 3.1 Main window

The GUI starts in Boris mode and has three tabs:

1. `Boris`
2. `Milena`
3. `Boris & Milena`

In a personal tab:

- Only that person’s records are shown.
- The redundant `Who` table column is hidden.
- The Add button is visible and names the person, for example `+ Add for Boris`.
- New records are automatically assigned to the selected person. The Add dialog intentionally has no user picker.

In the combined tab:

- Both people’s records and combined totals are shown.
- The `Who` column is visible in both tables.
- The Add button is hidden and replaced by a message asking the user to switch to a personal tab to add a record.

The combined view is only “view-only” with respect to adding. Existing rows can still be right-clicked and edited or deleted from the combined view. This is an important distinction from a truly read-only mode.

### 3.2 Period selection

The main period selector contains `(year, month)` values displayed as labels such as `September 2026`.

- Only months containing at least one record for the active person/view are listed.
- Months are ordered newest first.
- Switching person tabs attempts to preserve the selected month if that person has records in it; otherwise the newest available month is selected.
- The UI does not currently offer yearly or arbitrary date-range selection.
- An empty store has no selectable month, but Add remains available in a personal tab.

When adding from an existing selected month, the dialog defaults the transaction date to the first day of that selected month. When no month exists, it defaults to today. The user can always change the date. This behavior was added to make retrospective monthly data entry faster.

### 3.3 Summary and tables

For the selected month and person scope, the main window shows:

- `Earned`: sum of matching income.
- `Spent`: sum of matching spending.
- `Gap`: earned minus spent.
- A Spending table with Date, Who, Category, Amount, and Note.
- An Income table with Date, Who, Amount, and Note.

`Who` is hidden in personal views. IDs and `date_recorded` are always hidden.

Rows are:

- Sorted by transaction date and then by ID.
- Read-only in-place; editing happens through a modal dialog.
- Selected by full row.
- Displayed with a deliberately neutral dark-gray selection rather than the operating system’s accent color.
- Backed by their record ID stored as hidden Qt item metadata (`Qt.UserRole`) on the date cell.

Columns can be resized interactively. The Note column stretches to fill remaining width. The two tables sit in a vertical splitter, so their relative heights can be changed.

### 3.4 Adding and editing

The same `AddDialog` handles both adding and editing.

It supports:

- Spending or Income radio buttons.
- A text amount.
- A fixed category dropdown for spending.
- A calendar date widget.
- An optional one-line note.
- Add/Save and Cancel actions.

Amount input accepts both decimal points and decimal commas (`14.20` or `14,20`). The GUI requires a numeric amount greater than zero and marks an invalid amount field in red.

Editing pre-fills all editable values. It can also convert:

- Income to spending, in which case a category must be chosen.
- Spending to income, in which case the old category is discarded.

An edit preserves the original:

- ID.
- User.
- `date_recorded`.

The UI does not provide a way to move an existing record from Boris to Milena or vice versa. That would require a core API change or careful manual JSON editing.

If editing moves a record to a different month, the main view rebuilds the month list and attempts to show the destination month.

### 3.5 Deleting

Right-clicking a table row offers `Delete`. The GUI shows a confirmation containing:

- Record type.
- Owner.
- Amount.
- Date.
- Category for spending.

Deletion is permanent after the next successful save. There is no undo, recycle bin, soft-delete state, or automatic backup. IDs of deleted records are not reused.

### 3.6 Spending chart

The `Spending chart` button opens a modal dialog initially scoped to the same person and month as the main window. Inside the dialog, the person and month can be changed independently without changing the main window.

The chart:

- Groups spending by category.
- Sorts categories by descending total, then alphabetically for ties.
- Displays a pie chart.
- Shows total spending.
- Uses a scrollable custom legend.
- Shows category amount and percentage.
- Shows Boris’s and Milena’s amount below each category.
- Temporarily “explodes” a pie slice while it is hovered.
- Shows `No spending in this period.` instead of an empty chart.

The dialog defaults to 880 by 560 pixels. The current TODO file still asks to make the chart window bigger, even though the most recent chart commit already widened it from 720 to 880 pixels. Treat that TODO as a request for further adjustment or as potentially stale, not as proof that no resizing work has occurred.

Important chart nuance: `spending_by_category(...)` respects the chart’s selected person, but `spending_split_by_category(...)` always computes the Boris/Milena split over both people. Therefore, in a personal chart, the category headline and percentage describe only the selected person while the secondary split line can include the other person as additional context. The split line will not necessarily add up to the personal headline. In the combined chart it does add up.

## 4. Architecture

### 4.1 High-level structure

```text
                        +-------------------------+
                        |       main.py           |
                        | QApplication + window   |
                        +------------+------------+
                                     |
                                     v
+------------------+       +-------------------------+
|     cli.py       |       |       ui/ package       |
| argparse client  |       | PySide6 widgets/charts  |
+--------+---------+       +------------+------------+
         |                              |
         +---------------+--------------+
                         v
               +---------------------+
               |  finance/ package   |
               | models, config,     |
               | FinanceStore/query  |
               +----------+----------+
                          |
                          v
               +---------------------+
               | finance_data.json   |
               | local plain text    |
               +---------------------+
```

There are three conceptual layers:

1. **Core/domain and persistence (`finance/`)** — no Qt dependency; owns models, JSON serialization, mutation, and queries.
2. **Presentation (`ui/`)** — Qt widgets that call the core API.
3. **Entrypoints (`main.py`, `cli.py`)** — construct/load the shared store and expose it through a GUI or command line.

There is no service layer, repository interface, dependency-injection framework, ORM, event bus, or background worker. This is appropriate for the current scale.

### 4.2 Data flow

GUI startup:

```text
main.py
  -> QApplication
  -> FinanceStore.load(default_data_path())
  -> MainWindow(store)
  -> Qt event loop
```

GUI mutation:

```text
user action
  -> AddDialog or confirmation
  -> MainWindow calls FinanceStore mutation
  -> MainWindow calls store.save()
  -> controls are refreshed from the in-memory store
```

CLI mutation:

```text
argparse
  -> FinanceStore.load(default path)
  -> add_* mutation
  -> store.save()
  -> print confirmation
```

Queries do not hit the disk. `FinanceStore.load` reads the entire JSON document once, and all subsequent queries scan in-memory lists.

### 4.3 Why this boundary matters

The initial requirements explicitly asked for data loading, exporting, and queries to be independent of the UI so custom scripts and a CLI would remain easy. The implementation follows that requirement well:

- `finance/` imports no PySide6 modules.
- `ui/` never opens the JSON file.
- `cli.py` demonstrates that the core can be used without Qt.
- External scripts can import public names from `finance/__init__.py`.

This boundary should be preserved in future changes. Business rules and reusable queries belong in `finance`; widget-specific state and presentation belong in `ui`.

## 5. Repository layout and file responsibilities

```text
FinanceTracker/
├── .gitignore
├── AGENTS.md                      # this handoff document
├── README.md
├── cli.py                         # command-line entrypoint
├── finance/
│   ├── __init__.py                # public core exports
│   ├── config.py                  # people, categories, formats, chart colors, data path
│   ├── models.py                  # SpendingRecord and IncomeRecord serialization
│   └── store.py                   # persistence, mutations, and all queries
├── finance_data.json              # private runtime data; ignored by Git
├── lib_requirements.txt           # PySide6 dependency
├── main.py                        # GUI entrypoint
├── planning/
│   ├── FinanceTracker_SecureSync_CLI_ImplementationPlan.md
│   └── FinanceTracker_SecureSync_ImplementationPlan.md
├── prompt0001.txt                 # original product prompt
├── run.bat                        # Windows convenience launcher
├── todo.txt                       # informal feature/task history
├── ui/
│   ├── __init__.py
│   ├── add_dialog.py              # add/edit modal and validation
│   ├── main_window.py             # tabs, month selection, totals, tables, CRUD
│   └── spending_chart_dialog.py   # category pie chart and legend
└── v0.0_requirements.txt          # original detailed v0 requirements
```

There are no automated tests, packaging metadata, lock file, CI configuration, formal migration scripts, or generated installer in the repository.

### 5.1 `main.py`

`main()`:

- Creates a `QApplication`.
- Selects Qt’s cross-platform `Fusion` style.
- Loads the default JSON path.
- Creates and shows `MainWindow`.
- Enters the Qt event loop.

Fusion was selected for consistent Windows/macOS behavior and to avoid an unwanted native Windows accent bar on selected table rows.

### 5.2 `finance/config.py`

This module is the central source of truth for:

- The two user names.
- Combined-view label.
- Category choices.
- Euro display symbol.
- Storage and display date formats.
- Data filename and project-relative path.
- Pie-chart colors.
- Amount formatting.
- Category-to-color selection.

The current categories, in order, are:

```text
Groceries
Eating out
Transport
Rent
Bills / Utilities
Home Stuff
Health
Personal care
Entertainment
Sports
Shopping
Maintenance
Car Stuff
Work Expenses
Hobbies
Gifts
Holiday
Misc
```

Order matters because known categories map by index to the `CHART_COLORS` palette. Unknown categories receive a deterministic fallback based on the sum of their character code points.

`format_amount` renders exactly two decimal places followed by a non-breaking space and `€`, for example `14.20 €`.

The data path is:

```python
Path(__file__).resolve().parent.parent / "finance_data.json"
```

This means the file is kept in the repository/application directory, not in the current working directory or an operating-system user-data folder.

### 5.3 `finance/models.py`

This module defines two plain dataclasses and owns conversion between typed Python values and JSON-friendly dictionaries.

In memory:

- Money is `decimal.Decimal`.
- Dates are `datetime.date`.
- IDs are `int`.
- User, category, and note are `str`.

On disk:

- Dates are ISO strings (`YYYY-MM-DD`).
- Integral amounts are JSON integers.
- Non-integral amounts are JSON numbers produced through `float`.

The module validates that a loaded `user` is one of the configured `USERS`. It does not validate category membership or amount positivity.

### 5.4 `finance/store.py`

`FinanceStore` is the main application API. It owns:

- In-memory spending and income lists.
- The target file path.
- The next global record ID.
- Loading and saving.
- Add, lookup, update/convert, and delete operations.
- Date-range and user filtering.
- Totals, gap, category aggregation, per-person category splits, and available months.

Store mutations modify memory only. Callers are responsible for invoking `save()`. Both current front ends do this after successful mutations.

### 5.5 `cli.py`

This is a deliberately small `argparse` client. It serves two purposes:

- Provides useful non-GUI access.
- Proves that the finance core is reusable without Qt.

It supports add, monthly summary, and monthly list operations. It does not support edit, delete, arbitrary date ranges, or charts.

### 5.6 `ui/add_dialog.py`

`AddDialog` only collects and validates user input. It does not know about `FinanceStore` and does not save anything. On acceptance it exposes `result_data`, which `MainWindow` uses for a store mutation.

This separation keeps widget input handling out of persistence code.

### 5.7 `ui/main_window.py`

`MainWindow` coordinates the principal user workflow:

- Person tabs.
- Month selection.
- Summary labels.
- Data tables.
- Add/edit/delete actions.
- Store saves.
- Launching the chart dialog.

It contains small Qt-specific workarounds:

- `_NoFocusDelegate` removes the current-cell focus rectangle.
- `_period_tuple` normalizes combo-box data because Qt may round-trip a Python tuple as another sequence type.
- Record IDs are stored in `Qt.UserRole`.

### 5.8 `ui/spending_chart_dialog.py`

The chart dialog receives the existing in-memory store rather than reloading disk data. It has independent person/month selectors and rebuilds its chart body when either changes.

It uses `PySide6.QtCharts`:

- `QChart`
- `QChartView`
- `QPieSeries`

The custom legend was chosen instead of Qt’s built-in legend so each category can have a second, muted Boris/Milena breakdown line.

## 6. Technology and runtime

### 6.1 Language and standard library

- Python with modern type annotations.
- The source syntax is compatible with the project’s documented Python 3.9+ target (`list[str]`, `tuple[...]`, and postponed annotation evaluation are used).
- The inspected local environment uses Python 3.14.3.

Important standard-library modules include:

- `argparse` for CLI parsing.
- `calendar` for month boundaries and month names.
- `dataclasses` for records.
- `datetime` for typed dates.
- `decimal` for monetary calculations.
- `json` for storage.
- `pathlib` for paths.
- `tempfile` and `os.replace` for atomic saves.

### 6.2 GUI framework

The only third-party dependency declared by the project is:

```text
PySide6>=6.10
```

PySide6 6.10+ was selected because the dependency comment says older releases do not support Python 3.14. The inspected environment uses PySide6 6.11.1.

QtCharts is imported from the PySide6 distribution; there is no separate charting dependency.

### 6.3 Dependency file

Runtime dependencies are listed in `lib_requirements.txt`. There is no `requirements.txt`. README setup uses the same file:

```bash
pip install -r lib_requirements.txt
```

### 6.4 Setup and launch

Cross-platform setup:

```bash
python -m venv .venv
```

Windows PowerShell activation:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r lib_requirements.txt
python main.py
```

macOS/Linux-shell activation:

```bash
source .venv/bin/activate
pip install -r lib_requirements.txt
python main.py
```

Windows convenience launcher:

```bat
.\.venv\Scripts\python.exe main.py
```

That is the complete content of `run.bat`. It assumes:

- The current working directory is the project root.
- A virtual environment exists at `.venv`.
- Dependencies are already installed.

There is no equivalent macOS launcher, packaged `.app`, Windows executable, or installer.

## 7. Data model

### 7.1 Spending record

```text
SpendingRecord
  id: int
  amount: Decimal
  category: str
  date: date
  date_recorded: date
  user: str                 # exactly "Boris" or "Milena"
  note: str = ""
```

Semantics:

- `date` is when the purchase happened.
- `date_recorded` is when the application record was created.
- `date_recorded` defaults to today, is hidden in the GUI, and survives edits/type conversions.
- `category` is required by the GUI but is not validated by the core.

### 7.2 Income record

```text
IncomeRecord
  id: int
  amount: Decimal
  date: date
  date_recorded: date
  user: str                 # exactly "Boris" or "Milena"
  note: str = ""
```

Semantics mirror spending except that income has no category.

### 7.3 Shared invariants and conventions

Intended invariants:

- IDs are unique across both record types.
- IDs come from one monotonically increasing sequence.
- Deleted IDs are not reused.
- An edit or type conversion preserves ID, user, and recording date.
- Every record has one of the two configured user strings.
- Transaction dates, rather than recording dates, determine periods and totals.
- Date-range endpoints are inclusive.
- `None` as a query user means “both people/no user filter.”

Only some invariants are enforced by the core:

- The store validates the user when adding.
- Models validate users when loading.
- The GUI validates positive amounts.
- The core does **not** enforce positive amounts.
- The core does **not** enforce unique IDs on load.
- The core trusts an on-disk `next_id` even if it is stale or collides.
- The core does **not** validate categories against `CATEGORIES`.
- The core does **not** enforce two decimal places.

### 7.4 JSON schema

A representative, fictional version-2 file is:

```json
{
  "version": 2,
  "next_id": 3,
  "spending": [
    {
      "id": 1,
      "amount": 42.5,
      "category": "Groceries",
      "date": "2026-10-03",
      "date_recorded": "2026-10-05",
      "user": "Boris",
      "note": "weekly shop"
    }
  ],
  "income": [
    {
      "id": 2,
      "amount": 250,
      "date": "2026-10-04",
      "date_recorded": "2026-10-05",
      "user": "Milena",
      "note": "invoice"
    }
  ]
}
```

Top-level fields:

- `version`: written as `2`, but currently not checked or acted upon during load.
- `next_id`: ID to assign to the next new record.
- `spending`: array of spending dictionaries.
- `income`: array of income dictionaries.

If `spending` or `income` is absent, it is treated as an empty array. If `next_id` is absent, it is computed as one greater than the maximum existing ID. If the file itself is absent, an empty in-memory store is created and no file is written until the first save.

At documentation time, a structural-only inspection of the private file found:

- Version 2.
- 216 spending records and 5 income records.
- Both configured users present.
- All record IDs unique.
- `next_id` greater than the current maximum ID.
- All required record keys present.

These counts are only a point-in-time sanity check; they are not application constants. No actual amounts, notes, dates, or transaction descriptions are included in this document.

### 7.5 Numeric precision

The design intentionally uses `Decimal` for in-memory arithmetic:

```python
Decimal(str(value))
```

JSON parsing also uses `parse_float=Decimal`, avoiding a binary-float round trip while reading.

However, non-integral `Decimal` values are converted to `float` before JSON serialization. This keeps the JSON tidy but means storage is not a mathematically lossless arbitrary-precision decimal format. For normal two-decimal currency values, Python’s JSON output will ordinarily preserve the expected decimal text, but a future financial-hardening change should serialize amounts as decimal strings or integer minor units (cents) and migrate the schema.

The application displays two decimal places but does not restrict input to two decimal places.

## 8. Persistence behavior

### 8.1 Loading

`FinanceStore.load(path)`:

1. Returns an empty store if the path does not exist.
2. Parses the complete UTF-8 JSON document.
3. Uses `Decimal` for JSON fractional numbers.
4. Converts each dictionary through the relevant dataclass `from_dict`.
5. Loads `next_id`, or computes it only when absent.

Malformed JSON, invalid dates, missing mandatory fields, unknown users, permission failures, and other errors are not caught. They propagate and can prevent the GUI from opening or make the CLI exit with a traceback.

The loader does not inspect the `version` field. `DATA_VERSION = 2` currently describes what is written, not a functioning migration system.

### 8.2 Saving

`FinanceStore.save()` rewrites the entire data set:

```text
build complete payload
  -> create temp file in destination directory
  -> write indented UTF-8 JSON
  -> atomically replace finance_data.json
  -> clean up leftover temp file if necessary
```

`os.replace` provides an atomic final rename on Windows and macOS when used on the same filesystem. Creating the temporary file in the destination directory is what makes that same-filesystem replacement likely.

This protects against many partial-write/corruption cases, but it is not a backup strategy and does not provide:

- File locking.
- Concurrent-write detection.
- Merge behavior.
- Cloud-sync conflict handling.
- `fsync` durability guarantees.
- Versioned snapshots.

### 8.3 Concurrency and sharing warning

The data file path comes from `default_data_path()` in `finance/config.py`. Pointing two computers at one shared copy of that file is not safe for simultaneous editing.

Each process:

1. Loads a full snapshot.
2. Mutates its own memory.
3. Replaces the full file.

If both laptops open the same synchronized file and save independently, the last writer can overwrite the other person’s newer changes. Cloud providers may also create conflict copies that this application does not understand.

For current use, the safe operational rule is:

- Do not edit on both machines at the same time.
- Let synchronization finish before opening the application elsewhere.
- Keep external/versioned backups.

If reliable concurrent use becomes a real requirement, a shared JSON file is no longer sufficient without adding explicit synchronization, locking, merge/version checks, or moving to a small service/database.

### 8.4 Privacy and security

`finance_data.json` contains sensitive personal financial data in plaintext.

Current protections:

- The file is excluded by `.gitignore`.
- `*.tmp` files are also ignored.
- The application does not transmit telemetry or send data to a server.

Not provided:

- Encryption at rest.
- Authentication.
- Per-user access control.
- Secure secret storage.
- Redaction of notes.
- Automatic backups.
- Audit logging.

Operating-system permissions, device security, disk encryption, and any cloud provider’s security are therefore the real protection boundary.

The repository’s original requirements mention encryption only as a possible future direction, with the acknowledged trade-off that encrypted data would no longer be directly hand-editable.

## 9. Core API reference

The public package exports configuration values, both record classes, `FinanceStore`, and `month_range`.

### 9.1 Date helper

```python
month_range(year: int, month: int) -> tuple[date, date]
```

Returns the first and last date of a calendar month, inclusive. Invalid month/year combinations raise standard library exceptions.

### 9.2 Construction and persistence

```python
FinanceStore(path, spending=None, income=None, next_id=1)
FinanceStore.load(path) -> FinanceStore
store.save() -> None
```

The constructor can be useful in tests or scripts. `load` is the normal production path.

### 9.3 Add operations

```python
store.add_spending(
    amount,
    category: str,
    on_date: date,
    user: str,
    note: str = "",
    recorded_on: date | None = None,
) -> SpendingRecord
```

```python
store.add_income(
    amount,
    on_date: date,
    user: str,
    note: str = "",
    recorded_on: date | None = None,
) -> IncomeRecord
```

Both:

- Validate the user.
- Convert amount with `Decimal(str(amount))`.
- Allocate a shared ID immediately.
- Default `date_recorded` to today.
- Append to an in-memory list.
- Do not save automatically.

Neither enforces positivity.

### 9.4 Lookup, update, and delete

```python
store.get_spending(record_id) -> SpendingRecord | None
store.get_income(record_id) -> IncomeRecord | None
```

Lookups are linear scans and return the first matching record.

```python
store.update_record(
    record_id: int,
    record_type: str,       # "spending" or "income"
    amount,
    on_date: date,
    note: str = "",
    category: str = "",
) -> SpendingRecord | IncomeRecord
```

This can update in place or convert between record types. Unknown record types raise `ValueError`; unknown IDs raise `KeyError`.

When converting type, the old object is removed and a new object is appended to the destination list. The global ID and metadata remain stable, but raw list order changes. User-visible tables sort by date/ID, so the append behavior is normally invisible.

```python
store.delete_record(record_id) -> SpendingRecord | IncomeRecord
```

Deletes from either list and returns the removed object. An unknown ID raises `KeyError`.

### 9.5 Range queries

```python
store.spending_between(start, end, user=None) -> list[SpendingRecord]
store.income_between(start, end, user=None) -> list[IncomeRecord]
```

Both endpoints are inclusive. `user=None` means both people. A non-`None` user is compared exactly but is not separately validated by these query functions.

### 9.6 Totals

```python
store.total_spent(start, end, user=None) -> Decimal
store.total_earned(start, end, user=None) -> Decimal
store.gap(start, end, user=None) -> Decimal
```

Empty sums return `Decimal("0")`.

### 9.7 Category queries

```python
store.spending_by_category(start, end, user=None)
    -> list[tuple[str, Decimal]]
```

- Respects optional user filtering.
- Converts an empty category string to `Uncategorized`.
- Omits categories with no records.
- Sorts by amount descending, then category ascending.

```python
store.spending_split_by_category(start, end)
    -> dict[str, dict[str, Decimal]]
```

- Always includes both users’ matching records.
- Omits a user from a category’s inner dictionary if that user spent nothing.
- The chart supplies display zeros for omitted users.

### 9.8 Available months

```python
store.available_months(user=None) -> list[tuple[int, int]]
```

Collects distinct months from both record lists and returns them newest first. The GUI uses this to ensure period selectors offer only months that contain relevant data.

### 9.9 Example script

```python
from datetime import date
from finance import FinanceStore, default_data_path, month_range

store = FinanceStore.load(default_data_path())
start, end = month_range(2026, 10)

print(store.total_earned(start, end, "Boris"))
print(store.total_spent(start, end, "Boris"))
print(store.gap(start, end, "Boris"))

# Mutations require an explicit save:
record = store.add_spending(
    amount="12.50",
    category="Groceries",
    on_date=date(2026, 10, 13),
    user="Boris",
    note="example",
)
store.save()
```

Any script using the real default path changes the private data file. Tests and experiments should construct a store with a temporary path instead.

## 10. CLI behavior

### 10.1 Commands

Add spending:

```bash
python cli.py add-spending Boris 14.20 Groceries 2026-10-13 --note "example"
```

Categories containing spaces must be shell-quoted:

```bash
python cli.py add-spending Milena 20 "Eating out" 2026-10-13
```

Add income:

```bash
python cli.py add-income Milena 250 2026-10-04 --note "invoice"
```

Combined monthly summary:

```bash
python cli.py summary 2026 10
```

Personal monthly summary:

```bash
python cli.py summary 2026 10 --user Boris
```

List records:

```bash
python cli.py list 2026 10 --user Milena
```

If the optional date is omitted from an add command, it defaults to today.

### 10.2 CLI validation differences

The CLI validates:

- User against the configured two names.
- Explicit dates as ISO `YYYY-MM-DD`.
- Year and month as integers.

Unlike the GUI, it does not validate:

- Amount greater than zero.
- Category against `CATEGORIES`.
- Friendly month bounds before calling `calendar.monthrange`.

Consequences:

- The CLI can create zero or negative records.
- The CLI can create arbitrary/legacy category names.
- Invalid decimal text or dates can produce uncaught exceptions.

This difference is one of the strongest reasons to move invariants such as positive amounts into the core if stronger data integrity becomes important.

## 11. Important design decisions

### 11.1 One JSON file instead of a database

This was an explicit product decision:

- The app is for two known people.
- The data volume is small.
- Hand-editability is valuable.
- Installation and backup should be understandable.
- No database server should be required.

Trade-offs:

- Very simple deployment and inspection.
- Full-file reads/writes and linear queries.
- No concurrency control, transactions across multiple writers, indexing, or schema migration framework.

At the current scale, loading and rewriting a few hundred records is negligible. Complexity, not performance, is the limiting factor.

### 11.2 Decimal in memory

Money uses `Decimal` so summation does not accumulate ordinary binary floating-point artifacts. This is a sound core decision. The remaining weakness is the conversion of non-integral values to JSON floats during save.

### 11.3 Separate transaction date and recording date

The original model distinguishes when money moved from when the record was entered. The current UI reports and filters by transaction date while preserving recording date as hidden metadata. This supports retrospective entry without changing period reports.

### 11.4 Fixed two-person model

The two names are centralized in configuration, but the whole application assumes exactly two people:

- Two constants (`USER_BORIS`, `USER_MILENA`).
- Three fixed tab indexes.
- Three chart person choices.
- A two-name chart split line.
- CLI help text naming both people.

Changing the spelling of either configured user also makes existing records fail to load until their stored `user` values are migrated. Adding a third person is not a configuration-only change.

This is acceptable because the product is explicitly private and fixed-scope.

### 11.5 Combined mode cannot add

New records are created from personal modes so ownership is implicit and accidental misassignment is less likely. Combined mode is for household totals. Edit/delete remain available because existing records already carry an owner.

### 11.6 Only populated months are listed

This keeps navigation compact. It also means an empty future month cannot be selected before its first record exists. To create the first record for a new month, the user opens Add from another month and manually changes the date; after saving, the new month appears and is selected.

### 11.7 Atomic replace on save

Writing a sibling temporary file and replacing the destination reduces the risk of leaving half-written JSON after a crash. This is a valuable durability feature despite the application’s small size.

### 11.8 Source configuration over settings UI

Categories, users, currency, paths, and chart colors are edited in Python source. There is no settings screen or external configuration file. This keeps the app small but means “configuration” changes are developer changes and may require data migration.

## 12. Limitations, risks, and edge cases

### 12.1 Data integrity is front-end dependent

The GUI enforces positive amounts; `FinanceStore`, dataclasses, manual JSON, and CLI do not. Reusable invariants should ideally live in the core rather than only in one client.

There is also no load-time validation for:

- Duplicate IDs.
- `next_id` greater than all used IDs.
- Positive amounts.
- Known categories.
- Unknown top-level fields.
- Schema version compatibility.

### 12.2 Version 1 has no automatic migration

Two-user support changed the file format from version 1 to version 2 by adding mandatory `user` fields. The loader requires `data["user"]`, but it neither checks `version` nor migrates old records. An untouched version-1 file therefore fails to load with a missing-user error. Migration must be performed manually or implemented before loading old data.

### 12.3 Changing users requires data migration

Loading strictly validates user strings against current `USERS`. Renaming `Boris` or `Milena` in `config.py` without changing every stored record will break startup.

### 12.4 Categories are intentionally loose in storage

Changing `CATEGORIES` changes future GUI choices but does not invalidate old categories:

- Existing records with a removed category still load.
- Editing such a record temporarily adds its old category to that dialog.
- Charts assign unknown categories a deterministic fallback color.
- The CLI can create any category.

This is useful for historical continuity but means category cleanup/renaming requires explicit data migration if totals should be merged.

### 12.5 Error handling is minimal

There is no top-level error dialog or recovery mode for:

- Corrupt JSON.
- File permission errors.
- Invalid hand edits.
- Failed saves.
- Unsupported schema versions.

An exception can terminate the application or operation. Before adding complexity elsewhere, robust load/save error reporting and backups would be high-value reliability work.

### 12.6 No autosave beyond explicit actions

Current GUI flows call `save()` immediately after add/edit/delete. The store itself is not automatically persistent. Future callers can accidentally mutate memory and forget to save.

### 12.7 No concurrency or stale-write detection

Two open processes can overwrite each other, even on one machine. There is no “file changed since load” check.

### 12.8 Full-file and linear behavior

- Load: O(number of records).
- Save: O(number of records), rewriting the full JSON.
- Most filters/totals/lookups: O(number of records).

This is entirely reasonable for the current private data size. It should be revisited only if scale or synchronization requirements materially change.

### 12.9 Plaintext privacy

Notes may contain sensitive context in addition to amounts. Git ignore prevents accidental normal commits but does not encrypt local, backup, or cloud copies.

### 12.10 The path assumes a writable application directory

Keeping data beside source is convenient during development. It can fail if the application is later installed under a read-only system directory or packaged in a read-only bundle. A packaged version should use an operating-system user-data directory or an explicit configurable path.

### 12.11 No test suite

There are no automated unit, integration, persistence, migration, or UI tests. The architecture is testable, especially the core, but regressions currently depend on manual discovery.

### 12.12 Documentation drift

Known example:

- `todo.txt` still asks for a larger chart window after a commit already widened it.

Repository documentation should be checked against source before using it as authoritative.

## 13. Git history and evolution

The complete repository history contains 11 commits.

### 13.1 `d538874` — 2026-07-09 — `v0.0`

Created the entire initial application:

- Python/PySide6 GUI.
- UI-independent finance package.
- CLI proof of reuse.
- Version-1 JSON persistence.
- Spending and income dataclasses.
- Monthly tables and totals.
- Add dialog with positive amount validation.
- Atomic JSON save.
- README, original prompt, detailed requirements, TODO, and Git ignore.

The initial design already included several deliberate quality choices:

- `Decimal` for money.
- ISO dates on disk and `DD/MM/YYYY` in the UI.
- One shared ID sequence.
- Hidden record IDs stored in Qt row metadata.
- Fusion style and custom selection painting.
- Interactive columns.
- No database.

Two-user support was described as a future plan in `v0.0_requirements.txt` but was not yet implemented. Data version was 1.

### 13.2 `29912a3` — 2026-08-02 — `Add support for 2 users`

Implemented the defining household workflow:

- Added mandatory `user` to both record types.
- Bumped written data version from 1 to 2.
- Added Boris/Milena constants and combined-view label.
- Added per-user filtering throughout `FinanceStore`.
- Added `Boris`, `Milena`, and combined tabs.
- Auto-assigned new records to the selected personal tab.
- Disabled adding in combined mode.
- Added `Who` columns in combined tables.
- Added optional `--user` to CLI reports and required user on CLI adds.
- Updated README examples and schema.

No automatic v1-to-v2 migration was added.

### 13.3 `9c0dbbb` — 2026-09-05 — `Add run script for easier launch`

Added the one-line Windows `run.bat` launcher targeting `.venv`.

### 13.4 `1b7eab7` — 2026-09-05 — `Change categories in config`

Replaced the smaller generic category list with the current more specific household list. Examples:

- Split housing into Rent, Bills / Utilities, and Home Stuff.
- Added Personal care, Sports, Maintenance, Car Stuff, Work Expenses, Hobbies, Gifts, and Holiday.
- Removed generic Food and Housing/Utilities names from future GUI choices.

Because storage does not enforce the configured list, historical category names can continue to exist.

### 13.5 `183d94c` — 2026-09-05 — `Update TODO with new entries`

Recorded planned edit, delete, and selected-month date-default work. Also marked easier launch done.

### 13.6 `47edf7f` — 2026-09-05 — `Add support for editing an entry in a table`

Added:

- Store lookup methods.
- General `update_record`.
- Income/spending conversion while preserving identity metadata.
- AddDialog edit mode and prefill behavior.
- Right-click Edit action.
- Month-selection normalization for Qt combo data.

This commit established the context-menu CRUD pattern used by deletion.

### 13.7 `3b2c693` — 2026-09-05 — `Add support for deleting an entry from a table`

Added:

- `FinanceStore.delete_record`.
- Right-click Delete.
- Detailed confirmation dialog.
- Correct month-list refresh when deleting the last record in a month.

### 13.8 `44b3886` — 2026-09-05 — `Change default month in add dialog to month user is on, not current month in reality`

Changed add-mode date initialization from today to the first day of the selected month, while retaining today as the fallback when no period exists.

### 13.9 `67e01b2` — 2026-09-05 — `Add spending chart`

Added:

- Tableau-style chart colors.
- Category aggregation in the store.
- QtCharts pie dialog.
- Main-window chart button.
- Hover-exploding slices.
- Amount/percentage legend.

### 13.10 `186bf22` — 2026-09-05 — `Allow changing month and person in spending chart window`

Changed the chart from a fixed snapshot into a dialog holding the shared store:

- Added independent person and period selectors.
- Preserved initial main-window context.
- Rebuilt the chart on selector changes.
- Scoped month choices to the selected chart person.

### 13.11 `1c63c72` — 2026-09-05 — `In spending chart below each category show how much each person spent`

Added:

- Per-category, per-person aggregation.
- Custom scrollable legend.
- Boris/Milena secondary lines.
- Theme-aware muted text color.
- Recursive chart-layout cleanup.
- Wider default chart window.
- Current category-filter and further-chart-sizing TODO entries.

## 14. Current TODOs and earlier ideas

### 14.1 Explicit current TODOs

`todo.txt` currently lists:

- Make the spending-chart window bigger by default.
- Add spending-table category filtering using a checklist-style dropdown.

As noted earlier, the chart was already widened in the last commit, so sizing needs clarification before implementation.

### 14.2 Ideas present in the original requirements

These are historical ideas, not current implemented behavior:

- Yearly reports.
- Arbitrary custom periods.
- Showing hidden IDs and `date_recorded`, possibly through columns or tooltips.
- Optionally hiding notes or moving them to tooltips.
- Selecting whether one or both tables are visible.
- Encrypting the JSON file.

An AI planning future work should distinguish these exploratory notes from confirmed requests.

## 15. Safe extension guidance

### 15.1 Adding a reusable financial rule or query

Put it in `finance`, not `ui`.

Examples:

- Amount validation belongs in models/store if every caller must obey it.
- A yearly summary belongs as a date-range query in the store; the UI can then expose it.
- Category rename/migration logic belongs in core tooling, not widget code.

### 15.2 Adding a GUI feature

The normal pattern is:

1. Add or reuse a `FinanceStore` method.
2. Add Qt controls and signal wiring in `ui`.
3. On mutation, call the store method and then `save()`.
4. Rebuild any period choices that may have changed.
5. Refresh the relevant tables/totals.

Avoid reading `finance_data.json` directly from a dialog.

### 15.3 Changing the schema

A schema change should include:

1. Increment `DATA_VERSION`.
2. Make `load` inspect the stored version.
3. Define migration behavior for old files.
4. Preserve a backup before destructive migration.
5. Test round trips and malformed/partial cases.
6. Update README and this document.

The current version constant alone is not enough.

### 15.4 Changing names or adding people

Renaming either person requires transforming every stored record. Adding a third person requires changes beyond `USERS`, including:

- Main-window tab construction and mode mapping.
- Chart person choices.
- Chart legend representation.
- CLI help.
- Any two-person-specific labels and tests.

### 15.5 Changing categories

Appending a category is low risk. Renaming or removing one does not change old records automatically. Decide whether historical records should retain the old label or be migrated.

Changing category order also changes pie colors for known categories.

### 15.6 Experimenting without risking real data

Never run mutation experiments against `default_data_path()`. Use a temporary path:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from finance.store import FinanceStore

with TemporaryDirectory() as directory:
    store = FinanceStore(Path(directory) / "test.json")
    # exercise mutations and save safely here
```

The ignored private file is production data for this project, even though the application is small.

## 16. Recommended test strategy

No tests currently exist. A compact high-value suite would focus first on the core.

### 16.1 Model tests

- Integer and fractional amount round trips.
- ISO date round trips.
- Empty and Unicode notes.
- Valid and invalid users.
- Legacy/unknown categories.

### 16.2 Store persistence tests

- Missing file creates an empty store.
- Save then load preserves all fields.
- Missing `next_id` is recomputed.
- Atomic replacement leaves no temp files after success.
- Invalid JSON and invalid schema produce a controlled error.
- Future migration from version 1 to version 2, if implemented.

### 16.3 Mutation tests

- Shared IDs across income and spending.
- Update in place.
- Income-to-spending and spending-to-income conversion.
- ID/user/recorded-date preservation.
- Deletion and non-reuse of IDs.
- Unknown ID/type errors.
- Positive-amount enforcement if moved into core.

### 16.4 Query tests

- Inclusive date endpoints.
- Boris, Milena, and combined scopes.
- Leap-year month boundaries.
- Empty totals.
- Gap calculation.
- Month deduplication and descending ordering.
- Category aggregation, tie ordering, and Uncategorized behavior.
- Per-person category split.

### 16.5 GUI smoke tests

- Empty store startup.
- Person-tab switching and `Who` visibility.
- Add defaults to selected month.
- Edit conversion and move to another month.
- Deleting the final record in a month.
- Combined-mode add hidden but edit/delete available.
- Chart in personal and combined scopes.
- Light/dark palette readability.

### 16.6 Manual cross-platform checks

- Windows launch through `run.bat`.
- macOS launch through the virtual environment.
- Fusion rendering and table selection.
- Atomic save on both platforms.
- Behavior when the shared/cloud file is temporarily unavailable.

## 17. Priorities if reliability work is requested

For this private app, likely high-value improvements are:

1. Add automatic, timestamped backups before save or migration.
2. Add friendly load/save error dialogs.
3. Move amount invariants into the core.
4. Add core unit tests.
5. Check schema versions and implement migrations.
6. Detect a data file changed since it was loaded.
7. Correct setup documentation/dependency naming.
8. Decide on a safe two-machine synchronization workflow.

These are recommendations, not existing commitments. They should be weighed against the explicit goal of keeping the app simple.

## 18. Facts an AI assistant should retain

- This is a private two-person household tool, not a product for arbitrary users.
- The configured people are Boris and Milena.
- The app tracks spending, income, and `income - spending`.
- Current reporting/navigation is monthly, though core queries accept arbitrary inclusive ranges.
- The GUI is PySide6/Qt; charts use QtCharts.
- The core is UI-independent and should remain so.
- The entire data set lives in ignored plaintext `finance_data.json`.
- The JSON schema is version 2 and requires a `user` on every record.
- There is no functioning migration framework despite the version field.
- Money is `Decimal` in memory but fractional values are converted through float for JSON output.
- Saves use a same-directory temporary file plus atomic replacement.
- Saves rewrite the whole file and are not safe for concurrent writers.
- GUI mutations explicitly save; store mutations alone do not.
- GUI amount validation is stronger than CLI/core validation.
- Combined mode prevents adding but still allows editing and deletion.
- User ownership and recording date survive edits and type conversions.
- Categories are fixed in the GUI but loose in storage/core/CLI.
- The dependency file is `lib_requirements.txt`.
- There are no automated tests, packaging metadata, CI, installer, or application-level backups.
- Private transaction contents should not be copied into prompts, documentation, tests, or commits.

This document describes current behavior rather than declaring every behavior ideal. When source, README, TODO text, and historical requirements disagree, current source code is the operational authority; explicit new decisions from Boris and Milena should be the product authority.
