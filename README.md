# FDA FAERS Pharmacovigilance Dashboard

An ongoing project to explore FDA Adverse Event Reporting System (FAERS) quarterly data and build a pharmacovigilance dashboard. The project is currently in the **data profiling and rule-validation** stage; it does not yet produce a cleaned, analysis-ready dataset.

FAERS reports describe suspected adverse events. They do not establish that a drug caused an event.

## Current project contents

- `profile_faers_q.py` — profiles one quarter's seven FAERS ASCII tables and writes a DuckDB database and text report.
- `profile_cross_quarter.py` — compares existing quarter profile databases and writes a cross-quarter report.
- `profile_cross_quarter_walkthrough.ipynb` — guided, cell-by-cell version of the cross-quarter analysis.
- `inspect_profile_db.py`, `explore_demo.py` — helper scripts used to inspect and explore the profiling outputs.
- `requirements.txt` — Python package dependencies.

## Data is not stored in this repository

Downloaded archives, extracted source files, generated DuckDB databases, and reports are deliberately excluded from Git. The source archives and generated outputs are sizable and can be recreated locally. The repository includes empty `raw data` and `outputs` folders as reminders of where local files go.

Download the quarterly FAERS ASCII data from the [FDA FAERS data downloads page](https://www.fda.gov/drugs/fda-adverse-event-reporting-system-faers/fda-adverse-event-reporting-system-faers-public-dashboard). Extract the quarter files somewhere underneath `raw data`; the profiling script searches that folder recursively for the expected text files.

## Set up Python

From a terminal opened in the repository folder, create and activate a virtual environment, then install dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Use Python 3.10 or newer. The requirements install `ipykernel` so the environment can run the notebook. In VS Code, select `.venv` as the Python interpreter and as the notebook kernel.

## Profile a quarter

After extracting the quarter's files under `raw data`, run the single-quarter profiling script. For example:

```powershell
python .\profile_faers_q.py ".\raw data" ".\outputs\profile_24Q3" 24Q3
```

Repeat with the corresponding quarter code for each quarter. The script refuses to overwrite an existing report or database; choose a fresh output folder or move the existing files before rerunning it.

## Run the cross-quarter walkthrough

Open `profile_cross_quarter_walkthrough.ipynb` in VS Code or Jupyter, choose the project's `.venv` kernel, and run cells from top to bottom. It reads the per-quarter DuckDB databases in read-only mode and uses temporary in-memory tables. Its candidate “latest version, remove deleted cases” rule is for investigation only, not a finalized cleaning policy.

Alternatively, after all quarter databases exist, run the script from PowerShell:

```powershell
python .\profile_cross_quarter.py ".\outputs"
```

This writes `outputs/cross_quarter_report.txt` and may use a temporary DuckDB spill folder beneath `outputs`.

## Next project stage

Review the profiling findings and agree on documented rules for case-version selection, deleted cases, missing values, repeated drug rows, and joins across the FAERS tables. Then implement the rules in a separate transformation step and validate its row counts and join keys before connecting it to the dashboard.
