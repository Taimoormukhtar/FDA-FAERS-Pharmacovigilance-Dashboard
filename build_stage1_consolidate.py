"""
FAERS STAGE 1 - consolidate the 8 quarters into ONE set of 7 tables.

What this stage does (and ONLY this):
  1. Keeps the latest version of every case across all quarters
     (highest caseversion; ties -> latest fda_dt -> latest quarter).
  2. Removes every case listed in any DELETE file.
  3. Keeps child rows (DRUG, REAC, OUTC, RPSR, THER, INDI) only when they belong to
     the winning (primaryid, quarter) of a kept case.
  4. Adds ONE column, source_quarter. Nothing else is changed: every value stays
     exactly as FDA delivered it, as text. No cleaning, no standardisation yet.
  5. Proves it: row-by-row fidelity check against the source, orphan checks,
     "no deleted case survives" checks, and audit tables that explain every row removed.

Input : the 8 profile_??Q?.duckdb files made by the profiling script (attached READ-ONLY, never modified).
Output: faers_stage1.duckdb  (+ stage1_report.txt) in OUT_DIR.   Needs about 2 GB of free disk space.

Run:  python build_stage1_consolidate.py
      (or: python build_stage1_consolidate.py "C:\\folder\\with\\profile_folders" "C:\\output\\folder")
Close any program that has the profile_*.duckdb files open (VS Code SQL extensions, DBeaver ...).
"""
import sys
import time
from pathlib import Path

import duckdb

# =============================== SETTINGS ====================================
ROOT = Path(r"C:\path\to\the\outputs\folder")     # folder that CONTAINS profile_24Q3, profile_24Q4, ...
OUT_DIR = Path(r"C:\path\to\faers_clean")         # new folder for the consolidated database
MEMORY_LIMIT = "4GB"
# =============================================================================
if len(sys.argv) >= 3:
    ROOT, OUT_DIR = Path(sys.argv[1]), Path(sys.argv[2])

TABLES = ["demo", "drug", "reac", "outc", "rpsr", "ther", "indi"]
OUT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_PATH = OUT_DIR / "stage1_report.txt"
DB_PATH = OUT_DIR / "faers_stage1.duckdb"
TMP = OUT_DIR / "duckdb_tmp"
TMP.mkdir(exist_ok=True)
if DB_PATH.exists():
    DB_PATH.unlink()

_report = open(REPORT_PATH, "w", encoding="utf-8")
_t0 = time.time()


def say(text=""):
    print(text)
    _report.write(text + "\n")
    _report.flush()


def table(title, rows, cols):
    say(f"\n-- {title}")
    body = [[f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else str(v) for v in r] for r in rows]
    widths = [max(len(c), *(len(r[i]) for r in body)) if body else len(c) for i, c in enumerate(cols)]
    say("   " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)))
    say("   " + "-+-".join("-" * w for w in widths))
    for r in body:
        say("   " + " | ".join(v.ljust(w) for v, w in zip(r, widths)))


def scalar(sql):
    return con.execute(sql).fetchone()[0]


# -------------------------------- locate inputs ------------------------------
found = {p.stem[-4:]: p for p in ROOT.rglob("profile_??Q?.duckdb")}
LABELS = sorted(found)
if not LABELS:
    sys.exit(f"No profile_??Q?.duckdb files found under {ROOT}")

con = duckdb.connect(str(DB_PATH))
con.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
con.execute(f"SET temp_directory='{TMP.as_posix()}'")
con.execute("SET preserve_insertion_order=false")
con.execute("SET threads TO 4")
for lab in LABELS:
    con.execute(f"ATTACH '{found[lab].as_posix()}' AS db_{lab} (READ_ONLY)")

say("FAERS STAGE 1 - CONSOLIDATION REPORT")
say(time.strftime("Run started: %Y-%m-%d %H:%M:%S"))
say(f"Quarters: {', '.join(LABELS)}")
say(f"Output database: {DB_PATH}")

# every quarter DB must contain all 7 tables + del_ids
for lab in LABELS:
    have = {r[0] for r in con.execute(
        f"SELECT table_name FROM information_schema.tables WHERE table_catalog = 'db_{lab}'").fetchall()}
    miss = [t for t in TABLES + ["del_ids"] if t not in have]
    if miss:
        sys.exit(f"db_{lab} is missing tables: {miss}")

# ============================ 1. DECIDE WHICH CASES SURVIVE ==================
say("\n" + "=" * 90 + "\n1. CASE SELECTION\n" + "=" * 90)
for i, lab in enumerate(LABELS, start=1):
    sel = f"""SELECT {i} AS q_idx, '{lab}' AS source_quarter, primaryid, caseid,
                     try_cast(caseversion AS INTEGER) AS ver, fda_dt FROM db_{lab}.demo"""
    con.execute(("CREATE TABLE _keys_all AS " if i == 1 else "INSERT INTO _keys_all ") + sel)

con.execute(f"""
    CREATE TABLE _del AS
    SELECT caseid, min(src) AS first_delete_q FROM db_{LABELS[0]}.del_ids GROUP BY caseid""")

con.execute("""
    CREATE TABLE _winners AS
    SELECT * EXCLUDE (rn) FROM (
        SELECT *, row_number() OVER (PARTITION BY caseid
                                     ORDER BY ver DESC NULLS LAST, fda_dt DESC NULLS LAST, q_idx DESC, primaryid DESC) AS rn
        FROM _keys_all) WHERE rn = 1""")
con.execute("""
    CREATE TABLE _keep AS
    SELECT primaryid, caseid, source_quarter FROM _winners WHERE caseid NOT IN (SELECT caseid FROM _del)""")

n_all = scalar("SELECT count(*) FROM _keys_all")
n_latest = scalar("SELECT count(*) FROM _winners")
n_deleted = scalar("SELECT count(*) FROM _winners WHERE caseid IN (SELECT caseid FROM _del)")
n_final = scalar("SELECT count(*) FROM _keep")
con.execute("CREATE TABLE audit_case_funnel (step VARCHAR, cases BIGINT)")
con.executemany("INSERT INTO audit_case_funnel VALUES (?, ?)", [
    ("all case-version rows in the 8 quarter files", n_all),
    ("after keeping only the latest version of each case", n_latest),
    ("removed because a DELETE file lists the case", n_deleted),
    ("FINAL cases kept", n_final)])
table("case funnel", [(r[0], r[1]) for r in con.execute("SELECT * FROM audit_case_funnel").fetchall()], ["step", "cases"])

con.execute("""
    CREATE TABLE audit_case_lineage AS
    SELECT a.caseid, count(*) AS n_appearances, min(a.source_quarter) AS first_seen_q, max(a.source_quarter) AS last_seen_q,
           max(a.ver) AS max_version, w.source_quarter AS winning_quarter, w.primaryid AS winning_primaryid,
           d.first_delete_q, d.caseid IS NOT NULL AS was_deleted
    FROM _keys_all a JOIN _winners w USING (caseid) LEFT JOIN _del d USING (caseid)
    GROUP BY a.caseid, w.source_quarter, w.primaryid, d.first_delete_q, d.caseid""")
con.execute("""
    CREATE TABLE audit_removed_deleted_cases AS
    SELECT caseid, first_delete_q, n_appearances, first_seen_q, last_seen_q, winning_primaryid
    FROM audit_case_lineage WHERE was_deleted""")
table("kept cases by the quarter file that supplies their latest version",
      con.execute("SELECT source_quarter, count(*) FROM _keep GROUP BY 1 ORDER BY 1").fetchall(), ["winning_quarter", "cases"])

# ============================ 2. BUILD THE 7 CONSOLIDATED TABLES =============
say("\n" + "=" * 90 + "\n2. CONSOLIDATED TABLES (values untouched; one added column: source_quarter)\n" + "=" * 90)
con.execute("CREATE TABLE audit_table_rows (table_name VARCHAR, source_quarter VARCHAR, rows_in_source BIGINT, rows_kept BIGINT, rows_removed BIGINT)")
summary = []
for t in TABLES:
    for i, lab in enumerate(LABELS):
        before = scalar(f"SELECT count(*) FROM stg_{t}") if i else 0
        sel = f"""SELECT '{lab}' AS source_quarter, x.* FROM db_{lab}.{t} x
                  WHERE x.primaryid IN (SELECT primaryid FROM _keep WHERE source_quarter = '{lab}')"""
        con.execute((f"CREATE TABLE stg_{t} AS " if i == 0 else f"INSERT INTO stg_{t} ") + sel)
        kept = scalar(f"SELECT count(*) FROM stg_{t}") - before
        src = scalar(f"SELECT count(*) FROM db_{lab}.{t}")
        con.execute("INSERT INTO audit_table_rows VALUES (?, ?, ?, ?, ?)", [t, lab, src, kept, src - kept])
    tot_src, tot_kept = con.execute(
        "SELECT sum(rows_in_source), sum(rows_kept) FROM audit_table_rows WHERE table_name = ?", [t]).fetchone()
    summary.append((f"stg_{t}", int(tot_src), int(tot_kept), int(tot_src - tot_kept),
                    f"{100.0 * (tot_src - tot_kept) / tot_src:.1f}%" if tot_src else "-"))
    say(f"built stg_{t}: {int(tot_kept):,} rows kept of {int(tot_src):,}  [{time.time() - _t0:,.0f}s elapsed]")
table("rows per table: source (all 8 quarters stacked) vs kept", summary,
      ["table", "rows_in_source", "rows_kept", "rows_removed", "pct_removed"])

# ============================ 3. PROOF ========================================
say("\n" + "=" * 90 + "\n3. INTEGRITY CHECKS\n" + "=" * 90)
results = []


def check(name, ok, detail=""):
    results.append(ok)
    say(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


d_rows, d_case, d_pid = con.execute("SELECT count(*), count(DISTINCT caseid), count(DISTINCT primaryid) FROM stg_demo").fetchone()
check("stg_demo has exactly one row per case", d_rows == d_case == d_pid, f"rows={d_rows:,} caseids={d_case:,} primaryids={d_pid:,}")
check("stg_demo rows equal the case funnel's FINAL cases", d_rows == n_final, f"{d_rows:,} vs {n_final:,}")
for t in TABLES:
    n = scalar(f"SELECT count(*) FROM stg_{t} WHERE caseid IN (SELECT caseid FROM _del)")
    check(f"no deleted case survives in stg_{t}", n == 0, f"{n:,} found")
for t in TABLES[1:]:
    orphan = scalar(f"SELECT count(*) FROM stg_{t} c WHERE c.primaryid NOT IN (SELECT primaryid FROM stg_demo)")
    check(f"no orphan rows in stg_{t}", orphan == 0, f"{orphan:,} found")
    wrong_q = scalar(f"""SELECT count(*) FROM stg_{t} c JOIN stg_demo d USING (primaryid)
                         WHERE c.source_quarter <> d.source_quarter OR c.caseid <> d.caseid""")
    check(f"stg_{t} rows come from the same quarter and caseid as their DEMO row", wrong_q == 0, f"{wrong_q:,} mismatches")
for t in TABLES:
    non_text = scalar(f"""SELECT count(*) FROM information_schema.columns
                          WHERE table_name = 'stg_{t}' AND data_type <> 'VARCHAR'""")
    check(f"every column in stg_{t} is still text", non_text == 0, f"{non_text} non-text columns")

say("\n-- fidelity: every kept row is identical to its source row (multiset comparison, all columns)")
bad_total = 0
for t in TABLES:
    for lab in LABELS:
        diff = scalar(f"""SELECT count(*) FROM (
                              SELECT * EXCLUDE (source_quarter) FROM stg_{t} WHERE source_quarter = '{lab}'
                              EXCEPT ALL SELECT * FROM db_{lab}.{t})""")
        bad_total += diff
        if diff:
            say(f"   !! stg_{t} / {lab}: {diff:,} rows differ from source")
check("all kept rows are byte-identical to the source rows", bad_total == 0, f"{bad_total:,} differing rows")

con.execute("CHECKPOINT")
size_mb = DB_PATH.stat().st_size / 1e6
say("\n" + "=" * 90)
say(f"RESULT: {'ALL CHECKS PASSED' if all(results) else 'SOME CHECKS FAILED - do not use this output yet'}")
say(f"Database size: {size_mb:,.0f} MB | total run time: {time.time() - _t0:,.0f} seconds")
say("Tables: stg_demo stg_drug stg_reac stg_outc stg_rpsr stg_ther stg_indi")
say("Audit : audit_case_funnel, audit_table_rows, audit_case_lineage, audit_removed_deleted_cases")
say(f"Report : {REPORT_PATH}")
con.close()
_report.close()
