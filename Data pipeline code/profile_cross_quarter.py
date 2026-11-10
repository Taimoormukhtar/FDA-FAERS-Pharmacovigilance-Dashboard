"""
FAERS CROSS-QUARTER profiling pass (READ-ONLY).

Uses the per-quarter DuckDB files the first script already created
(profile_24Q3.duckdb ... profile_26Q2.duckdb). It does NOT touch the raw .txt
files and it does NOT modify those databases (they are attached read-only).

Questions it answers (these need ALL quarters at once, so the single-quarter
reports could not answer them):
  A. How many case-versions / distinct cases are there in total?
  B. How many quarters does one case appear in? Does the version always go up?
  C. Are there ties (same caseid + same version in two quarters)?
  D. What would "keep the latest version, drop deleted cases" leave us with?
  E. Do delete files ever list a case that is still published LATER?
  F. Is the data-quality drift (occupation, country, drug rows) real, and where does it come from?
  G. How did the DRUG table's structure change over time?

Requirements: Python 3.10+, duckdb (already installed).
Run:  python profile_cross_quarter.py
      (or: python profile_cross_quarter.py "C:\\folder\\that\\contains\\the\\profile_folders")
Edit ROOT below first. Output: cross_quarter_report.txt in ROOT.
"""
import sys
import time
from decimal import Decimal
from pathlib import Path

import duckdb

# =============================== SETTINGS ====================================
ROOT = Path(r"C:\path\to\the\outputs\folder")   # folder that CONTAINS profile_24Q3, profile_24Q4, ... (searched recursively)
MEMORY_LIMIT = "4GB"
# =============================================================================
if len(sys.argv) >= 2:
    ROOT = Path(sys.argv[1])

REPORT_PATH = ROOT / "cross_quarter_report.txt"
_report = open(REPORT_PATH, "w", encoding="utf-8")
_t0 = time.time()
_last = time.time()


def say(text=""):
    print(text)
    _report.write(text + "\n")
    _report.flush()


def section(title):
    global _last
    now = time.time()
    say(f"\n[{now - _last:6.1f}s since previous section]")
    _last = now
    say("\n" + "=" * 90)
    say(title)
    say("=" * 90)


def _fmt(v):
    if v is None:
        return "NULL"
    if isinstance(v, (float, Decimal)):
        return f"{float(v):,.2f}"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    return str(v).replace("\n", " ").replace("\r", " ")[:60]


def show(title, sql, max_rows=60):
    say(f"\n-- {title}")
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    except Exception as exc:
        say(f"   !! query failed: {exc}")
        return []
    if not rows:
        say("   (no rows)")
        return rows
    body = [[_fmt(v) for v in r] for r in rows[:max_rows]]
    widths = [max(len(str(c)), *(len(r[i]) for r in body)) for i, c in enumerate(cols)]
    say("   " + " | ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    say("   " + "-+-".join("-" * w for w in widths))
    for r in body:
        say("   " + " | ".join(v.ljust(w) for v, w in zip(r, widths)))
    if len(rows) > max_rows:
        say(f"   ... ({len(rows) - max_rows:,} more rows not shown)")
    return rows


# -------------------------------- find + attach ------------------------------
found = {}
for p in ROOT.rglob("profile_??Q?.duckdb"):
    found[p.stem[-4:]] = p
LABELS = sorted(found)          # '24Q3' < '24Q4' < '25Q1' ... sorts correctly as text
if not LABELS:
    sys.exit(f"No profile_??Q?.duckdb files found under {ROOT}")

TMP = ROOT / "duckdb_tmp_cross"
TMP.mkdir(exist_ok=True)
con = duckdb.connect()          # in-memory working database
con.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
con.execute(f"SET temp_directory='{TMP.as_posix()}'")
con.execute("SET preserve_insertion_order=false")
con.execute("SET threads TO 4")
for lab in LABELS:
    con.execute(f"ATTACH '{found[lab].as_posix()}' AS db_{lab} (READ_ONLY)")

say("FAERS CROSS-QUARTER PROFILING REPORT (read-only)")
say(f"Quarters found: {', '.join(LABELS)}")
say(time.strftime("Run started: %Y-%m-%d %H:%M:%S"))
FIRST, LAST = LABELS[0], LABELS[-1]
WIN_START = f"20{FIRST[:2]}{(int(FIRST[-1]) - 1) * 3 + 1:02d}01"

# combined DEMO (only the columns needed here) -- small enough to hold in memory
for i, lab in enumerate(LABELS, start=1):
    sel = f"""SELECT {i} AS q_idx, '{lab}' AS q, primaryid, caseid, try_cast(caseversion AS INTEGER) AS ver,
                     init_fda_dt, fda_dt, event_dt, rept_cod, mfr_sndr, sex, age, occp_cod,
                     reporter_country, occr_country FROM db_{lab}.demo"""
    con.execute(("CREATE TABLE case_q AS " if i == 1 else "INSERT INTO case_q ") + sel)
con.execute(f"CREATE TABLE del_ids AS SELECT * FROM db_{FIRST}.del_ids")


# ============================ A. INVENTORY ===================================
section("A. INVENTORY")
show("rows and distinct caseid per quarter file", """
    SELECT q, count(*) AS case_version_rows, count(DISTINCT caseid) AS distinct_caseid FROM case_q GROUP BY 1 ORDER BY 1""")
show("all quarters together", """
    SELECT count(*) AS case_version_rows, count(DISTINCT caseid) AS distinct_caseid,
           count(*) - count(DISTINCT caseid) AS rows_that_are_repeat_appearances,
           count(DISTINCT primaryid) AS distinct_primaryid FROM case_q""")

# ============================ B. APPEARANCES + VERSIONS ======================
section("B. HOW OFTEN DOES A CASE APPEAR, AND DOES ITS VERSION GO UP?")
show("number of quarter files each caseid appears in", """
    WITH a AS (SELECT caseid, count(*) AS n FROM case_q GROUP BY 1)
    SELECT n AS appears_in_n_quarters, count(*) AS cases FROM a GROUP BY 1 ORDER BY 1""")
show("version behaviour between consecutive appearances of the same case", """
    WITH o AS (SELECT caseid, ver, lag(ver) OVER (PARTITION BY caseid ORDER BY q_idx) AS prev_ver FROM case_q)
    SELECT count(*) FILTER (WHERE prev_ver IS NOT NULL) AS later_appearances,
           count(*) FILTER (WHERE ver > prev_ver) AS version_went_up,
           count(*) FILTER (WHERE ver = prev_ver) AS same_version_again,
           count(*) FILTER (WHERE ver < prev_ver) AS version_went_DOWN
    FROM o""")
show("examples where the version did NOT go up (first 10)", """
    WITH o AS (SELECT caseid, q, ver, fda_dt, lag(ver) OVER (PARTITION BY caseid ORDER BY q_idx) AS prev_ver,
                      lag(q) OVER (PARTITION BY caseid ORDER BY q_idx) AS prev_q FROM case_q)
    SELECT caseid, prev_q, prev_ver, q AS later_q, ver AS later_ver, fda_dt FROM o
    WHERE prev_ver IS NOT NULL AND ver <= prev_ver ORDER BY caseid LIMIT 10""", 10)
show("same (caseid, version) in more than one quarter (FAQ 29 type ties)", """
    SELECT count(*) AS caseid_version_pairs, coalesce(sum(n), 0) AS rows_involved,
           count(*) FILTER (WHERE n_fda > 1) AS pairs_with_different_fda_dt
    FROM (SELECT caseid, ver, count(*) AS n, count(DISTINCT fda_dt) AS n_fda FROM case_q GROUP BY 1, 2 HAVING count(*) > 1)""")
show("cases whose init_fda_dt differs between versions (should be 0)", """
    SELECT count(*) AS cases FROM (SELECT caseid FROM case_q GROUP BY 1 HAVING count(DISTINCT init_fda_dt) > 1)""")

# ============================ C. WINNER RULE =================================
section("C. WHAT 'KEEP THE LATEST VERSION, DROP DELETED CASES' WOULD LEAVE")
con.execute("""
    CREATE TABLE winners AS
    SELECT * EXCLUDE (rn) FROM (
        SELECT *, row_number() OVER (PARTITION BY caseid ORDER BY ver DESC NULLS LAST, fda_dt DESC, q_idx DESC) AS rn
        FROM case_q) WHERE rn = 1""")
con.execute("CREATE TABLE final_case AS SELECT * FROM winners WHERE caseid NOT IN (SELECT caseid FROM del_ids)")
show("funnel", """
    SELECT (SELECT count(*) FROM case_q) AS all_case_version_rows,
           (SELECT count(*) FROM winners) AS after_keep_latest_version,
           (SELECT count(*) FROM winners WHERE caseid IN (SELECT caseid FROM del_ids)) AS removed_as_deleted,
           (SELECT count(*) FROM final_case) AS final_cases""")
show("final cases: which quarter file supplies the winning version", """
    SELECT q AS winning_quarter, count(*) AS cases FROM final_case GROUP BY 1 ORDER BY 1""")
show(f"final cases by quarter FIRST received by FDA (init_fda_dt); 'before window' = first received before {WIN_START}", f"""
    SELECT CASE WHEN init_fda_dt < '{WIN_START}' THEN 'before window'
                ELSE substr(init_fda_dt, 3, 2) || 'Q' || ((try_cast(substr(init_fda_dt, 5, 2) AS INTEGER) - 1) // 3 + 1) END AS first_received_quarter,
           count(*) AS cases,
           round(100.0 * count(*) FILTER (WHERE ver > 1) / count(*), 1) AS pct_with_follow_up_version
    FROM final_case GROUP BY 1 ORDER BY 1""")
show("final cases: first-received quarter vs winning quarter (how many are 'moved' by using the file quarter?)", f"""
    SELECT count(*) AS cases,
           count(*) FILTER (WHERE init_fda_dt >= '{WIN_START}' AND
                 substr(init_fda_dt, 3, 2) || 'Q' || ((try_cast(substr(init_fda_dt, 5, 2) AS INTEGER) - 1) // 3 + 1) <> q) AS new_cases_whose_winning_file_is_a_later_quarter,
           count(*) FILTER (WHERE init_fda_dt < '{WIN_START}') AS cases_first_received_before_window
    FROM final_case""")

# ============================ D. DELETE FILES ================================
section("D. DELETE FILES vs PUBLICATION ORDER")
show("cases matched by a delete file: was the case seen in a quarter AFTER the first delete file that lists it?", """
    WITH dq AS (SELECT caseid, min(src) AS first_delete_q FROM del_ids GROUP BY 1),
         a  AS (SELECT caseid, max(q) AS last_seen_q, min(q) AS first_seen_q FROM case_q GROUP BY 1)
    SELECT count(*) AS deleted_cases_present_in_window,
           count(*) FILTER (WHERE a.first_seen_q < dq.first_delete_q) AS first_seen_before_delete_file,
           count(*) FILTER (WHERE a.last_seen_q = dq.first_delete_q) AS last_seen_same_quarter_as_delete_file,
           count(*) FILTER (WHERE a.last_seen_q > dq.first_delete_q) AS seen_in_LATER_quarter_than_delete_file
    FROM a JOIN dq USING (caseid)""")
show("deleted cases present in window, by quarter of their first delete file", """
    WITH dq AS (SELECT caseid, min(src) AS first_delete_q FROM del_ids GROUP BY 1)
    SELECT first_delete_q, count(DISTINCT c.caseid) AS cases FROM case_q c JOIN dq USING (caseid) GROUP BY 1 ORDER BY 1""")

# ============================ E. DRIFT =======================================
section("E. DATA-QUALITY DRIFT BY QUARTER (all rows of each quarter file)")
show("completeness by quarter", """
    SELECT q, count(*) AS rows,
           round(100.0 * count(*) FILTER (WHERE occp_cod IS NULL) / count(*), 1) AS pct_occp_null,
           round(100.0 * count(*) FILTER (WHERE reporter_country = 'COUNTRY NOT SPECIFIED') / count(*), 1) AS pct_country_not_specified,
           round(100.0 * count(*) FILTER (WHERE occr_country IS NULL) / count(*), 1) AS pct_occr_country_null,
           round(100.0 * count(*) FILTER (WHERE sex IS NULL) / count(*), 1) AS pct_sex_null,
           round(100.0 * count(*) FILTER (WHERE age IS NULL) / count(*), 1) AS pct_age_null,
           round(100.0 * count(*) FILTER (WHERE event_dt IS NULL) / count(*), 1) AS pct_event_dt_null
    FROM case_q GROUP BY 1 ORDER BY 1""")
show("occupation missing by quarter and report type (where does the 2026 gap come from?)", """
    SELECT q, rept_cod, count(*) AS rows, round(100.0 * count(*) FILTER (WHERE occp_cod IS NULL) / count(*), 1) AS pct_occp_null
    FROM case_q GROUP BY 1, 2 ORDER BY 1, 2""", 60)
show(f"top senders among rows with missing occupation in {LAST}", f"""
    SELECT mfr_sndr, rept_cod, count(*) AS rows_with_null_occp FROM case_q
    WHERE q = '{LAST}' AND occp_cod IS NULL GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10""", 10)
show(f"same list for {FIRST}, for contrast", f"""
    SELECT mfr_sndr, rept_cod, count(*) AS rows_with_null_occp FROM case_q
    WHERE q = '{FIRST}' AND occp_cod IS NULL GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10""", 10)
show("same drift measured on FINAL cases, grouped by the quarter they were first received", f"""
    SELECT CASE WHEN init_fda_dt < '{WIN_START}' THEN 'before window'
                ELSE substr(init_fda_dt, 3, 2) || 'Q' || ((try_cast(substr(init_fda_dt, 5, 2) AS INTEGER) - 1) // 3 + 1) END AS first_received_quarter,
           count(*) AS cases,
           round(100.0 * count(*) FILTER (WHERE occp_cod IS NULL) / count(*), 1) AS pct_occp_null,
           round(100.0 * count(*) FILTER (WHERE age IS NULL) / count(*), 1) AS pct_age_null,
           round(100.0 * count(*) FILTER (WHERE sex IS NULL) / count(*), 1) AS pct_sex_null
    FROM final_case GROUP BY 1 ORDER BY 1""")

# ============================ F. DRUG STRUCTURE ==============================
section("F. HOW THE DRUG TABLE CHANGED OVER TIME")
parts = []
for lab in LABELS:
    parts.append(f"""
    SELECT '{lab}' AS q, count(*) AS drug_rows,
           count(DISTINCT primaryid || '|' || drug_seq) AS distinct_case_seq,
           round(1.0 * count(*) / count(DISTINCT primaryid), 2) AS rows_per_case,
           round(1.0 * count(DISTINCT primaryid || '|' || drug_seq) / count(DISTINCT primaryid), 2) AS distinct_seq_per_case,
           count(*) FILTER (WHERE role_cod = 'PS') AS ps_rows,
           count(DISTINCT primaryid || '|' || drug_seq) FILTER (WHERE role_cod = 'PS') AS ps_distinct_seq,
           count(DISTINCT primaryid || '|' || upper(trim(prod_ai))) FILTER (WHERE role_cod = 'PS') AS ps_distinct_case_ingredient
    FROM db_{lab}.drug""")
show("drug rows vs distinct drug_seq vs distinct case x ingredient, per quarter", " UNION ALL ".join(parts) + " ORDER BY 1")
parts = []
for lab in LABELS:
    parts.append(f"""
    SELECT '{lab}' AS q, count(*) AS repeated_seq_groups,
           count(*) FILTER (WHERE dn > 1) AS differ_drugname, count(*) FILTER (WHERE ai > 1) AS differ_prod_ai,
           count(*) FILTER (WHERE rc > 1) AS differ_role, count(*) FILTER (WHERE rt > 1) AS differ_route,
           count(*) FILTER (WHERE da > 1) AS differ_dose_amt, count(*) FILTER (WHERE df > 1) AS differ_dose_freq,
           count(*) FILTER (WHERE lt > 1) AS differ_lot
    FROM (SELECT primaryid, drug_seq, count(DISTINCT drugname) AS dn, count(DISTINCT prod_ai) AS ai,
                 count(DISTINCT role_cod) AS rc, count(DISTINCT route) AS rt, count(DISTINCT dose_amt) AS da,
                 count(DISTINCT dose_freq) AS df, count(DISTINCT lot_num) AS lt
          FROM db_{lab}.drug GROUP BY 1, 2 HAVING count(*) > 1)""")
show("when one drug_seq has several rows: what differs between those rows? (counts of groups)", " UNION ALL ".join(parts) + " ORDER BY 1")
parts = []
for lab in LABELS:
    parts.append(f"""
    SELECT '{lab}' AS q, count(*) AS cases_with_ps,
           round(100.0 * count(*) FILTER (WHERE n = 1) / count(*), 1) AS pct_one_ps_ingredient,
           round(100.0 * count(*) FILTER (WHERE n = 2) / count(*), 1) AS pct_two,
           round(100.0 * count(*) FILTER (WHERE n >= 3) / count(*), 1) AS pct_three_or_more
    FROM (SELECT primaryid, count(DISTINCT upper(trim(prod_ai))) AS n FROM db_{lab}.drug WHERE role_cod = 'PS' GROUP BY 1)""")
show("how many DIFFERENT primary-suspect ingredients does one case carry?", " UNION ALL ".join(parts) + " ORDER BY 1")

section("DONE")
say(f"Total run time: {time.time() - _t0:,.1f} seconds")
say(f"Report written to: {REPORT_PATH}")
con.close()
_report.close()
