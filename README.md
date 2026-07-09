# Finance Tracker

A small, cross-platform (Windows + macOS) desktop app for tracking personal
income and spending, built with Python and Qt (PySide6). Data lives in a single
hand-editable JSON file — no database.

This is **v0.0**: the bare-minimum version.

## What it does

- Add **income** and **spending** records through a simple dialog.
- Pick a **month** to view (the dropdown only lists months that have records).
- See, for the selected month:
  - a **Spending** table (date, category, amount, note),
  - an **Income** table (date, amount, note),
  - totals: **Earned**, **Spent**, and the **Gap** (earned − spent; green when
    positive, red when negative).

Amounts are shown in euro (€). Dates are shown as `DD/MM/YYYY` and stored as
`YYYY-MM-DD` in the JSON file.

## Project layout

```
FinanceTracker/
├── finance/              # UI-independent core API (no Qt here)
│   ├── config.py         #   categories, currency, date formats, file path
│   ├── models.py         #   SpendingRecord, IncomeRecord
│   └── store.py          #   FinanceStore: load/save JSON + all queries
├── ui/                   # Qt (PySide6) UI — a thin client over `finance`
│   ├── add_dialog.py     #   modal "Add record" dialog
│   └── main_window.py    #   main window: period selector, summary, tables
├── main.py               # launches the GUI
├── cli.py                # tiny command-line client (proves the API is reusable)
├── requirements.txt
└── finance_data.json     # created on first save, next to the program
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
pip install -r requirements.txt
```

### macOS (Terminal)

```bash
cd /path/to/FinanceTracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run the app

```bash
python main.py
```

## Use the CLI (optional)

The CLI reads/writes the same `finance_data.json`:

```bash
python cli.py add-spending 14.20 Food 2026-06-11 --note "toilet paper"
python cli.py add-income 3560 2026-06-03
python cli.py summary 2026 6
python cli.py list 2026 6
```

## The data file

`finance_data.json` is created next to the program the first time you save a
record. It is plain JSON and safe to edit by hand. Example:

```json
{
  "version": 1,
  "next_id": 7,
  "spending": [
    { "id": 1, "amount": 14.2, "category": "Food", "date": "2026-06-11", "date_recorded": "2026-06-11", "note": "toilet paper" }
  ],
  "income": [
    { "id": 4, "amount": 3560, "date": "2026-06-03", "date_recorded": "2026-06-13", "note": "" }
  ]
}
```

IDs are a single shared sequence across both income and spending.

To share data between two machines, point both at the same file by editing
`DATA_FILENAME` / `default_data_path()` in `finance/config.py` (e.g. to a
Dropbox/iCloud/OneDrive folder).

## Changing categories or currency

Edit `finance/config.py`:

- `CATEGORIES` — the fixed list shown in the spending category dropdown.
- `CURRENCY_SYMBOL` — the symbol shown next to amounts.
