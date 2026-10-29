# FDA FAERS Pharmacovigilance Dashboard

An ongoing project to explore FDA Adverse Event Reporting System (FAERS) quarterly data and prepare a pharmacovigilance dashboard. The repository now includes profiling, cross-quarter case consolidation, and a rule-based Stage 2 clean layer. Dashboard-specific aggregates and visualizations are still future work.

FAERS reports describe suspected adverse events. They do not establish that a drug caused an event.

## Current project contents

- `profile_faers_q.py` — profiles one quarter's seven FAERS ASCII tables and writes a DuckDB database and text report.
- `profile_cross_quarter.py` — compares existing quarter profile databases and writes a cross-quarter report.
- `profile_cross_quarter_walkthrough.ipynb` — guided, cell-by-cell version of the cross-quarter analysis.
- `build_stage1_consolidate.py` — consolidates the latest case versions from the quarter profile databases into seven Stage 1 tables, with audit tables and integrity checks.
- `build_stage1_consolidate_walkthrough.ipynb` — guided, cell-by-cell explanation of the Stage 1 consolidation script. It reads the saved Stage 1 database and source profiles read-only; it does not rerun the consolidation.
- `build_stage2_clean.py` — builds a separate clean layer from Stage 1, retains selected raw fields alongside cleaned fields, and writes audit and review outputs.
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

## Understand and review Stage 1

Open `build_stage1_consolidate_walkthrough.ipynb` in VS Code, select the project's `.venv` kernel, and run cells from top to bottom. The notebook expects the eight `profile_??Q?.duckdb` files under `outputs` and a completed Stage 1 output folder; generated data is not included in the repository.

The consolidation script writes one latest-version-per-case set of seven tables and audit tables. It intentionally preserves the delivered values as text; it does not produce cleaned, analysis-ready dashboard tables.

To build Stage 1 from the eight profile databases, use a new output folder:

```powershell
python .\build_stage1_consolidate.py ".\outputs" ".\outputs\stage1_consolidated"
```

## Build the Stage 2 clean layer

After Stage 1 is complete, run the cleaner with the Stage 1 database and a **new output folder**:

```powershell
python .\build_stage2_clean.py ".\outputs\stage1_consolidated\faers_stage1.duckdb" ".\outputs\stage2_clean"
```

Stage 2 attaches the Stage 1 database read-only. It writes `faers_clean.duckdb`, `stage2_report.txt`, and review CSVs for ingredients, country codes, code values, and reaction casing. It retains raw values for source fields projected into the clean tables and adds standardized fields and status flags. The clean database is an analysis-oriented projection, not a row-for-row copy; keep Stage 1 as the source for original columns not represented in Stage 2. The output folder is local and ignored by Git; it must be regenerated from the FAERS data and Stage 1 database.

**Important:** both consolidation scripts delete an existing output database at their configured output path before rebuilding. Use a new output folder for a new run, or back up any database you intend to keep.

The clean tables have different grains and should not be joined indiscriminately:

| Table | Grain | Main join |
|---|---|---|
| `clean_case` | One row per selected case | `caseid` identifies the case; `primaryid` identifies its selected report version |
| `clean_drug` | One row per case/report and `drug_seq` | Join to `clean_case` by `primaryid`; to dose/indication details by `primaryid` plus `drug_seq_raw` |
| `clean_dose` | Source drug rows containing dose or route details | May contain multiple rows per drug sequence; use `primaryid` plus `drug_seq_raw` |
| `clean_reaction` | One row per report and reaction term | Join to `clean_case` by `primaryid`; multiple reactions per case are expected |
| `clean_outcome` | One row per report and outcome code | Join to `clean_case` by `primaryid`; multiple outcomes per case are expected |
| `clean_onset` | One row per case with a selected administered primary-suspect drug | Join to `clean_case` by `primaryid`; onset is available only when dates support it |
| `clean_indication` | One row per report, drug sequence, and indication term | Join to case by `primaryid`; to drug by `primaryid` plus `drug_seq_raw` |
| `clean_report_source` | One row per report and source code | Join to `clean_case` by `primaryid`; multiple source codes may occur |

Aggregate each child table to the intended chart grain before joining multiple child tables. Joining, for example, drugs directly to reactions and outcomes can multiply rows and inflate counts. Preserve `caseid`, `primaryid`, and `drug_seq_raw` when exporting or building relationships.

Stage 2 is a cleaned analytical layer, not causal evidence or a complete dashboard model. Drug parent-molecule and country-name mapping remain review tasks; duplicate candidates are flags, not confirmed duplicates; recent cases are provisional; and missing or implausible values remain unavailable with status flags. FAERS has no exposure denominator and is subject to reporting bias and reporting delays.

## Next project stage

Review and approve the ingredient and country review files, decide how the duplicate flags will be used in signal analyses, then build dashboard-specific aggregates and visualizations. Keep the clean case/drug/reaction/outcome tables as separate grains and validate each published metric against a defined case-level denominator.
