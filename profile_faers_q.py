"""
FAERS profiling pass (READ-ONLY). Nothing is cleaned, dropped or changed.

What it does
  1. Loads the 7 raw $-delimited files of ONE quarter into a DuckDB database,
     every column as text, so nothing is silently converted.
  2. Measures the things we need to know before writing any cleaning rule:
     row counts, schema, null rates, date behaviour, code lists, drug-name mess,
     join coverage, orphan rows, deleted-case overlap, duplicate candidates.
  3. Writes a text report and a DuckDB working database under OUT_DIR.

Requirements: Python 3.10+; install packages with:
              python -m pip install -r requirements.txt
Run default:  python profile_faers_q.py
Other quarter: python profile_faers_q.py "C:\\raw_folder" "C:\\out_folder" 24Q4

The defaults below are set for this project and the 2024 Q3 quarter.
"""
import re
import sys
import time
import platform
from decimal import Decimal
from pathlib import Path

import duckdb

# =============================== SETTINGS ====================================
PROJECT_DIR = Path(__file__).resolve().parent
RAW_DIR = PROJECT_DIR / "raw data"                # quarter files are in nested ASCII/Deleted folders
OUT_DIR = PROJECT_DIR / "outputs" / "profile_24Q3"
QUARTER = "24Q3"                                  # two-digit year + Q + quarter number
MEMORY_LIMIT = "4GB"                              # cap for DuckDB (machine has 8 GB or less)
# =============================================================================

if len(sys.argv) >= 3:
    RAW_DIR, OUT_DIR = Path(sys.argv[1]), Path(sys.argv[2])
if len(sys.argv) >= 4:
    QUARTER = sys.argv[3]

# Row counts you measured on the raw files (data rows, header excluded).
EXPECTED_ROWS = {
    "24Q3": dict(demo=405513, drug=1907293, indi=1177133, outc=288275,
                 reac=1431718, rpsr=10087, ther=532854),
    "24Q4": dict(demo=410849, drug=2030938, indi=1219759, outc=308960,
                 reac=1472750, rpsr=11627, ther=561889),
    "25Q1": dict(demo=400514, drug=2008162, indi=1205861, outc=304027,
                 reac=1432926, rpsr=11033, ther=520395),
    "25Q2": dict(demo=393130, drug=1829056, indi=1136665, outc=295583,
                 reac=1340666, rpsr=11105, ther=512759),
    "25Q3": dict(demo=438512, drug=2148451, indi=1310274, outc=343251,
                 reac=1535133, rpsr=11107, ther=514480),
    "25Q4": dict(demo=385288, drug=1815349, indi=1168789, outc=289721,
                 reac=1349105, rpsr=10694, ther=454746),
    "26Q1": dict(demo=397224, drug=1703210, indi=1139940, outc=291580,
                 reac=1330675, rpsr=11426, ther=404946),
    "26Q2": dict(demo=422459, drug=1627225, indi=1124333, outc=303705,
                 reac=1394751, rpsr=11295, ther=326027),
}.get(QUARTER, {})

# Column lists from ASC_NTS (lower-case) -- used to detect schema drift.
EXPECTED_COLS = {
    "demo": "primaryid caseid caseversion i_f_code event_dt mfr_dt init_fda_dt fda_dt rept_cod auth_num "
            "mfr_num mfr_sndr lit_ref age age_cod age_grp sex e_sub wt wt_cod rept_dt to_mfr occp_cod "
            "reporter_country occr_country".split(),
    "drug": "primaryid caseid drug_seq role_cod drugname prod_ai val_vbm route dose_vbm cum_dose_chr "
            "cum_dose_unit dechal rechal lot_num exp_dt nda_num dose_amt dose_unit dose_form dose_freq".split(),
    "reac": "primaryid caseid pt drug_rec_act".split(),
    "outc": "primaryid caseid outc_cod".split(),
    "rpsr": "primaryid caseid rpsr_cod".split(),
    "ther": "primaryid caseid dsg_drug_seq start_dt end_dt dur dur_cod".split(),
    "indi": "primaryid caseid indi_drug_seq indi_pt".split(),
}
PREFIX = {"demo": "DEMO", "drug": "DRUG", "reac": "REAC", "outc": "OUTC",
          "rpsr": "RPSR", "ther": "THER", "indi": "INDI"}

# ------------------------------ quarter bounds --------------------------------
YY = 2000 + int(QUARTER[:2])
QN = int(QUARTER[-1])
Q_START = f"{YY}{(QN - 1) * 3 + 1:02d}01"
Q_END = f"{YY}{QN * 3:02d}{ {1: '31', 2: '30', 3: '30', 4: '31'}[QN] }"

# ------------------------------ output helpers -------------------------------
OUT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_PATH = OUT_DIR / f"profile_report_{QUARTER}.txt"
DB_PATH = OUT_DIR / f"profile_{QUARTER}.duckdb"
if REPORT_PATH.exists() or DB_PATH.exists():
    raise FileExistsError(
        f"Refusing to overwrite existing profile output in {OUT_DIR}. "
        "Choose a new OUT_DIR or move the existing report and database first."
    )
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


def show(title, sql, max_rows=40):
    """Run a query and print it as a plain-text table. Failures are reported, not fatal."""
    say(f"\n-- {title}")
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    except Exception as exc:  # keep going; one bad query must not kill the report
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


def dist(table, col, n=15, title=None):
    show(title or f"{table}.{col} - value counts (top {n})",
         f"""SELECT coalesce({col}, '<NULL>') AS value, count(*) AS n,
                    round(100.0 * count(*) / (SELECT count(*) FROM {table}), 2) AS pct
             FROM {table} GROUP BY 1 ORDER BY 2 DESC LIMIT {n}""")


def scalar(sql):
    return con.execute(sql).fetchone()[0]


# -------------------------------- connect ------------------------------------
TMP_DIR = OUT_DIR / "duckdb_tmp"
TMP_DIR.mkdir(exist_ok=True)
con = duckdb.connect(str(DB_PATH))
con.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
con.execute(f"SET temp_directory='{TMP_DIR.as_posix()}'")
con.execute("SET preserve_insertion_order=false")
con.execute("SET threads TO 4")

say("FAERS PROFILING REPORT (read-only)")
say(f"Quarter: {QUARTER}   window: {Q_START} to {Q_END}")
say(f"Raw folder: {RAW_DIR}")
say(f"Python {platform.python_version()} on {platform.system()} | duckdb {duckdb.__version__} | memory cap {MEMORY_LIMIT}")
say(time.strftime("Run started: %Y-%m-%d %H:%M:%S"))


# ============================ A. LOAD + SCHEMA ===============================
section("A. FILE LOAD, ROW COUNTS, SCHEMA")


def find_file(prefix):
    target = f"{prefix}{QUARTER}".lower()
    for p in RAW_DIR.rglob("*.txt"):
        if p.is_file() and p.suffix.lower() == ".txt" and p.stem.lower() == target:
            return p
    return None


load_rows = []
for t, prefix in PREFIX.items():
    path = find_file(prefix)
    if path is None:
        say(f"!! {prefix}{QUARTER}.txt NOT FOUND in {RAW_DIR}")
        continue
    con.execute(f"""
        CREATE TABLE {t} AS
        SELECT * FROM read_csv('{path.as_posix()}', delim='$', header=true, quote='', escape='',
                               all_varchar=true, ignore_errors=true, store_rejects=true,
                               rejects_table='rej_{t}', rejects_scan='rej_scan_{t}',
                               encoding='latin-1')""")
    # strip a BOM from the first column name if present
    for (_, name, *_rest) in con.execute(f"PRAGMA table_info('{t}')").fetchall():
        if "\ufeff" in name:
            con.execute(f'ALTER TABLE {t} RENAME COLUMN "{name}" TO "{name.replace(chr(0xFEFF), "")}"')
    n = scalar(f"SELECT count(*) FROM {t}")
    try:
        rejected = scalar(f"SELECT count(*) FROM rej_{t}")
    except Exception:
        rejected = "n/a"
    exp = EXPECTED_ROWS.get(t)
    load_rows.append((prefix + QUARTER + ".txt", round(path.stat().st_size / 1e6, 1), n,
                      exp if exp else "-", (n - exp) if exp else "-", rejected))

say("\n-- load summary (rows loaded vs the row count you measured; rejected = lines DuckDB refused)")
say("   file | size MB | rows loaded | expected | difference | reject records")
for r in load_rows:
    say("   " + " | ".join(str(x) for x in r))

for t in PREFIX:
    try:
        cols = [r[1].lower() for r in con.execute(f"PRAGMA table_info('{t}')").fetchall()]
    except Exception:
        continue
    missing = [c for c in EXPECTED_COLS[t] if c not in cols]
    extra = [c for c in cols if c not in EXPECTED_COLS[t]]
    order_ok = cols == EXPECTED_COLS[t]
    say(f"\n{t}: {len(cols)} columns | same names as ASC_NTS: {not missing and not extra} | "
        f"same order: {order_ok} | missing: {missing or 'none'} | unexpected: {extra or 'none'}")
    try:
        show(f"{t}: first reject records (if any)",
             f"SELECT * FROM rej_{t} LIMIT 5")
    except Exception:
        pass

# derived view used by several sections (NOTHING is written back to the raw tables)
con.execute(f"""
CREATE VIEW demo_x AS
SELECT d.*,
  try_cast(age AS DOUBLE) AS age_num,
  CASE age_cod WHEN 'YR' THEN try_cast(age AS DOUBLE)
               WHEN 'DEC' THEN try_cast(age AS DOUBLE) * 10
               WHEN 'MON' THEN try_cast(age AS DOUBLE) / 12
               WHEN 'WK'  THEN try_cast(age AS DOUBLE) / 52.1775
               WHEN 'DY'  THEN try_cast(age AS DOUBLE) / 365.25
               WHEN 'HR'  THEN try_cast(age AS DOUBLE) / 8766 END AS age_years,
  CASE wt_cod WHEN 'KG'  THEN try_cast(wt AS DOUBLE)
              WHEN 'LBS' THEN try_cast(wt AS DOUBLE) * 0.45359237
              WHEN 'GMS' THEN try_cast(wt AS DOUBLE) / 1000 END AS wt_kg
FROM demo d""")


# ============================ B. DEMO ========================================
section("B. DEMO - one row per case VERSION")
show("keys", """
    SELECT count(*) AS rows, count(DISTINCT primaryid) AS distinct_primaryid,
           count(DISTINCT caseid) AS distinct_caseid,
           count(*) FILTER (WHERE primaryid = caseid || caseversion) AS primaryid_equals_caseid_plus_version,
           count(*) FILTER (WHERE primaryid IS NULL OR caseid IS NULL) AS null_keys
    FROM demo""")
show("caseversion distribution", """
    SELECT CASE WHEN try_cast(caseversion AS INTEGER) IS NULL THEN 'invalid/NULL'
                WHEN try_cast(caseversion AS INTEGER) = 1 THEN '1'
                WHEN try_cast(caseversion AS INTEGER) = 2 THEN '2'
                WHEN try_cast(caseversion AS INTEGER) <= 5 THEN '3-5'
                WHEN try_cast(caseversion AS INTEGER) <= 10 THEN '6-10'
                ELSE '11+' END AS version_bucket,
           count(*) AS n FROM demo GROUP BY 1 ORDER BY min(try_cast(caseversion AS INTEGER))""")
show("i_f_code vs caseversion (is i_f_code consistent with version?)", """
    SELECT coalesce(i_f_code,'<NULL>') AS i_f_code,
           CASE WHEN try_cast(caseversion AS INTEGER) = 1 THEN 'version 1' ELSE 'version 2+' END AS version,
           count(*) AS n FROM demo GROUP BY 1, 2 ORDER BY 1, 2""")
for c in ["rept_cod", "sex", "age_cod", "age_grp", "occp_cod", "e_sub", "to_mfr", "wt_cod"]:
    dist("demo", c, 15)

# proper null-rate table (built programmatically)
null_sql = " UNION ALL ".join(
    f"SELECT '{c}' AS column_name, round(100.0 * count(*) FILTER (WHERE {c} IS NULL) / count(*), 2) AS pct_null FROM demo"
    for c in EXPECTED_COLS["demo"])
show("DEMO null rate per column (percent of rows blank)", null_sql + " ORDER BY 2 DESC", 30)


# ============================ C. DATES =======================================
section("C. DATES - precision, range, and the 'which quarter is this row' question")
date_cols = ["event_dt", "mfr_dt", "init_fda_dt", "fda_dt", "rept_dt"]
prec_sql = " UNION ALL ".join(f"""
    SELECT '{c}' AS col,
           CASE WHEN {c} IS NULL THEN 'NULL' WHEN length({c}) = 8 THEN 'YYYYMMDD'
                WHEN length({c}) = 6 THEN 'YYYYMM' WHEN length({c}) = 4 THEN 'YYYY'
                ELSE 'other_len_' || length({c}) END AS precision,
           count(*) AS n, min({c}) AS min_value, max({c}) AS max_value,
           count(*) FILTER (WHERE length({c}) = 8 AND try_strptime({c}, '%Y%m%d') IS NULL) AS invalid_full_dates
    FROM demo GROUP BY 1, 2""" for c in date_cols)
show("date precision per column", prec_sql + " ORDER BY 1, 2", 40)

show(f"init_fda_dt: first received BEFORE / INSIDE / AFTER {QUARTER} ({Q_START}-{Q_END})", f"""
    SELECT CASE WHEN init_fda_dt IS NULL THEN 'NULL'
                WHEN init_fda_dt < '{Q_START}' THEN 'before quarter (older case updated in this file)'
                WHEN init_fda_dt > '{Q_END}' THEN 'after quarter' ELSE 'inside quarter (new case)' END AS bucket,
           count(*) AS n, round(100.0 * count(*) / (SELECT count(*) FROM demo), 2) AS pct
    FROM demo GROUP BY 1 ORDER BY 2 DESC""")
show("init_fda_dt by YEAR (how old are the cases in this file?)", """
    SELECT substr(init_fda_dt, 1, 4) AS init_year, count(*) AS n FROM demo GROUP BY 1 ORDER BY 1""", 30)
show(f"fda_dt: BEFORE / INSIDE / AFTER {QUARTER}", f"""
    SELECT CASE WHEN fda_dt IS NULL THEN 'NULL' WHEN fda_dt < '{Q_START}' THEN 'before quarter'
                WHEN fda_dt > '{Q_END}' THEN 'after quarter' ELSE 'inside quarter' END AS bucket,
           count(*) AS n, round(100.0 * count(*) / (SELECT count(*) FROM demo), 2) AS pct
    FROM demo GROUP BY 1 ORDER BY 2 DESC""")
show("fda_dt by month", "SELECT substr(fda_dt, 1, 6) AS fda_month, count(*) AS n FROM demo GROUP BY 1 ORDER BY 1", 40)
show("date anomalies (full dates only)", """
    SELECT count(*) FILTER (WHERE length(init_fda_dt)=8 AND length(fda_dt)=8 AND init_fda_dt > fda_dt) AS init_after_fda,
           count(*) FILTER (WHERE length(event_dt)=8 AND length(fda_dt)=8 AND event_dt > fda_dt) AS event_after_fda,
           count(*) FILTER (WHERE length(event_dt)=8 AND length(init_fda_dt)=8 AND event_dt > init_fda_dt) AS event_after_init_fda,
           count(*) FILTER (WHERE length(event_dt) >= 4 AND substr(event_dt,1,4) < '1990') AS event_before_1990,
           count(*) FILTER (WHERE length(rept_dt)=8 AND length(fda_dt)=8 AND rept_dt > fda_dt) AS rept_after_fda
    FROM demo""")
show("event_dt year distribution (last 15 years present)", """
    SELECT substr(event_dt, 1, 4) AS event_year, count(*) AS n FROM demo
    WHERE event_dt IS NOT NULL GROUP BY 1 ORDER BY 1 DESC LIMIT 15""")
show("reporting lag: event_dt -> init_fda_dt (full dates only)", """
    WITH x AS (SELECT date_diff('day', try_strptime(event_dt,'%Y%m%d'), try_strptime(init_fda_dt,'%Y%m%d')) AS lag
               FROM demo WHERE length(event_dt) = 8 AND length(init_fda_dt) = 8)
    SELECT CASE WHEN lag < 0 THEN 'negative (event after first FDA receipt)' WHEN lag <= 30 THEN '0-30 days'
                WHEN lag <= 90 THEN '31-90 days' WHEN lag <= 365 THEN '91-365 days' ELSE 'over 365 days' END AS lag_bucket,
           count(*) AS n FROM x WHERE lag IS NOT NULL GROUP BY 1 ORDER BY min(lag)""")


# ============================ D. AGE + WEIGHT ================================
section("D. AGE AND WEIGHT")
show("age parsing", """
    SELECT count(*) FILTER (WHERE age IS NOT NULL) AS age_present,
           count(*) FILTER (WHERE age IS NOT NULL AND age_num IS NULL) AS age_not_numeric,
           count(*) FILTER (WHERE age IS NOT NULL AND age_cod IS NULL) AS age_without_unit,
           count(*) FILTER (WHERE age IS NOT NULL AND age_cod IS NOT NULL AND age_years IS NULL) AS unit_not_recognised,
           count(*) FILTER (WHERE age_years < 0) AS negative_age,
           count(*) FILTER (WHERE age_years > 120) AS over_120_years
    FROM demo_x""")
show("age in years (summary)", """
    SELECT count(age_years) AS n, round(min(age_years),2) AS min_y, round(quantile_cont(age_years,0.01),1) AS p1,
           round(quantile_cont(age_years,0.5),1) AS median, round(quantile_cont(age_years,0.99),1) AS p99,
           round(max(age_years),1) AS max_y FROM demo_x""")
show("age_grp vs computed age in years (does the group label match the number?)", """
    SELECT coalesce(age_grp,'<NULL>') AS age_grp, count(*) AS rows, count(age_years) AS with_age,
           round(min(age_years),1) AS min_y, round(quantile_cont(age_years,0.5),1) AS median_y,
           round(max(age_years),1) AS max_y FROM demo_x GROUP BY 1 ORDER BY 1""")
show("age vs age_grp presence", """
    SELECT count(*) FILTER (WHERE age_years IS NOT NULL AND age_grp IS NULL) AS age_but_no_group,
           count(*) FILTER (WHERE age_years IS NULL AND age_grp IS NOT NULL) AS group_but_no_age,
           count(*) FILTER (WHERE age_years IS NULL AND age_grp IS NULL) AS neither
    FROM demo_x""")
show("weight", """
    SELECT count(*) FILTER (WHERE wt IS NOT NULL) AS wt_present,
           count(*) FILTER (WHERE wt IS NOT NULL AND wt_cod IS NULL) AS wt_without_unit,
           count(*) FILTER (WHERE wt IS NOT NULL AND try_cast(wt AS DOUBLE) IS NULL) AS wt_not_numeric,
           round(min(wt_kg),2) AS min_kg, round(quantile_cont(wt_kg,0.5),1) AS median_kg, round(max(wt_kg),1) AS max_kg,
           count(*) FILTER (WHERE wt_kg < 0.2 OR wt_kg > 500) AS implausible_kg
    FROM demo_x""")


# ============================ E. GEOGRAPHY + SENDERS =========================
section("E. COUNTRIES AND SENDERS")
dist("demo", "reporter_country", 15)
dist("demo", "occr_country", 15)
show("country code shape (anything that is not a 2-letter code)", """
    SELECT 'reporter_country' AS col, coalesce(reporter_country,'<NULL>') AS value, count(*) AS n FROM demo
      WHERE reporter_country IS NULL OR length(reporter_country) <> 2 GROUP BY 2
    UNION ALL
    SELECT 'occr_country', coalesce(occr_country,'<NULL>'), count(*) FROM demo
      WHERE occr_country IS NULL OR length(occr_country) <> 2 GROUP BY 2
    ORDER BY 1, 3 DESC""", 20)
show("reporter_country vs occr_country", """
    SELECT count(*) FILTER (WHERE reporter_country = occr_country) AS same,
           count(*) FILTER (WHERE reporter_country <> occr_country) AS different,
           count(*) FILTER (WHERE occr_country IS NULL) AS occr_missing FROM demo""")
show("mfr_sndr variety", """
    SELECT count(DISTINCT mfr_sndr) AS distinct_raw, count(DISTINCT upper(trim(mfr_sndr))) AS distinct_upper_trim,
           count(*) FILTER (WHERE mfr_sndr IS NULL) AS null_rows FROM demo""")
dist("demo", "mfr_sndr", 15)


# ============================ F. DRUG ========================================
section("F. DRUG - the biggest table and the messiest column (drug names)")
show("keys and shape", """
    SELECT count(*) AS rows, count(DISTINCT primaryid) AS cases_with_drugs,
           count(DISTINCT drugname) AS distinct_drugname_raw,
           count(DISTINCT upper(trim(drugname))) AS distinct_drugname_upper_trim,
           count(DISTINCT prod_ai) AS distinct_prod_ai_raw,
           count(DISTINCT upper(trim(prod_ai))) AS distinct_prod_ai_upper_trim FROM drug""")
dist("drug", "role_cod", 10, "role_cod (look for DN = Drug Not Administered)")
dist("drug", "val_vbm", 5)
show("drug rows per case", """
    WITH c AS (SELECT primaryid, count(*) AS n FROM drug GROUP BY 1)
    SELECT CASE WHEN n=1 THEN '1' WHEN n=2 THEN '2' WHEN n<=5 THEN '3-5' WHEN n<=10 THEN '6-10'
                WHEN n<=20 THEN '11-20' ELSE '21+' END AS drug_rows_per_case, count(*) AS cases
    FROM c GROUP BY 1 ORDER BY min(n)""")
show("(primaryid, drug_seq) NOT unique? (FAQ 52)", """
    SELECT count(*) AS seq_groups_with_multiple_rows, coalesce(sum(n),0) AS rows_involved
    FROM (SELECT primaryid, drug_seq, count(*) AS n FROM drug GROUP BY 1, 2 HAVING count(*) > 1)""")
show("same active ingredient repeated under different drug_seq in one case", """
    SELECT count(*) AS groups, coalesce(sum(n),0) AS rows_involved
    FROM (SELECT primaryid, upper(trim(prod_ai)) AS ai, count(DISTINCT drug_seq) AS n FROM drug
          WHERE prod_ai IS NOT NULL GROUP BY 1, 2 HAVING count(DISTINCT drug_seq) > 1)""")
show("primary suspect (PS) coverage (FAQ 30: some cases have none)", """
    SELECT count(*) AS cases,
           count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM drug g WHERE g.primaryid = d.primaryid)) AS no_drug_rows_at_all,
           count(*) FILTER (WHERE EXISTS (SELECT 1 FROM drug g WHERE g.primaryid = d.primaryid)
                              AND NOT EXISTS (SELECT 1 FROM drug g WHERE g.primaryid = d.primaryid AND g.role_cod = 'PS')) AS drugs_but_no_PS
    FROM demo d""")
show("prod_ai / drugname quality on PS rows", """
    SELECT count(*) AS ps_rows,
           count(*) FILTER (WHERE prod_ai IS NULL) AS prod_ai_null,
           count(*) FILTER (WHERE upper(trim(drugname)) = upper(trim(prod_ai))) AS drugname_equals_prod_ai,
           count(*) FILTER (WHERE strpos(prod_ai, chr(92)) > 0) AS prod_ai_has_backslash,
           count(*) FILTER (WHERE strpos(prod_ai, ';') > 0) AS prod_ai_has_semicolon,
           count(*) FILTER (WHERE strpos(prod_ai, ',') > 0) AS prod_ai_has_comma,
           count(*) FILTER (WHERE drugname <> trim(drugname) OR prod_ai <> trim(prod_ai)) AS has_stray_whitespace,
           count(*) FILTER (WHERE strpos(drugname, '?') > 0) AS drugname_has_question_mark
    FROM drug WHERE role_cod = 'PS'""")
show("top 25 PS active ingredients (distinct cases)", """
    SELECT coalesce(upper(trim(prod_ai)), '<NULL prod_ai>') AS prod_ai, count(DISTINCT primaryid) AS cases
    FROM drug WHERE role_cod = 'PS' GROUP BY 1 ORDER BY 2 DESC LIMIT 25""", 25)
show("top 25 PS drugname (distinct cases)", """
    SELECT upper(trim(drugname)) AS drugname, count(DISTINCT primaryid) AS cases
    FROM drug WHERE role_cod = 'PS' GROUP BY 1 ORDER BY 2 DESC LIMIT 25""", 25)
show("how much of the PS volume do the top-K drug names cover? (tells us how far a mapping table must go)", """
    WITH c AS (SELECT upper(trim(coalesce(prod_ai, drugname))) AS d, count(DISTINCT primaryid) AS n
               FROM drug WHERE role_cod = 'PS' GROUP BY 1),
         r AS (SELECT n, row_number() OVER (ORDER BY n DESC) AS rk, sum(n) OVER () AS tot FROM c)
    SELECT k AS top_k_names, round(100.0 * sum(n) FILTER (WHERE rk <= k) / max(tot), 1) AS pct_of_ps_volume,
           (SELECT count(*) FROM c) AS total_distinct_names
    FROM r CROSS JOIN (VALUES (50), (100), (500), (1000), (5000)) AS t(k) GROUP BY k ORDER BY k""")
dist("drug", "route", 15)
dist("drug", "dechal", 6)
dist("drug", "rechal", 6)
dist("drug", "dose_freq", 15)
dist("drug", "lot_num", 5, "lot_num - most common values (placeholder text?)")
show("nda_num 999999 (OTC marker, FAQ 23)", "SELECT count(*) FILTER (WHERE nda_num = '999999') AS otc_marker_rows, count(*) AS rows FROM drug")


# ============================ G. REAC ========================================
section("G. REAC - reactions (MedDRA preferred terms)")
show("shape", """
    SELECT count(*) AS rows, count(DISTINCT primaryid) AS cases_with_reactions, count(DISTINCT pt) AS distinct_pt,
           count(DISTINCT lower(trim(pt))) AS distinct_pt_lower_trim,
           count(*) FILTER (WHERE pt = upper(pt) AND pt <> lower(pt)) AS pt_all_uppercase_rows,
           count(*) FILTER (WHERE pt <> trim(pt)) AS pt_stray_whitespace,
           count(*) FILTER (WHERE strpos(pt, '?') > 0) AS pt_has_question_mark,
           count(*) FILTER (WHERE drug_rec_act IS NOT NULL) AS drug_rec_act_filled FROM reac""")
show("reactions per case", """
    WITH c AS (SELECT primaryid, count(*) AS n FROM reac GROUP BY 1)
    SELECT CASE WHEN n=1 THEN '1' WHEN n=2 THEN '2' WHEN n<=5 THEN '3-5' WHEN n<=10 THEN '6-10' ELSE '11+' END AS reactions_per_case,
           count(*) AS cases FROM c GROUP BY 1 ORDER BY min(n)""")
show("duplicate (primaryid, pt) rows", """
    SELECT count(*) AS groups, coalesce(sum(n),0) AS rows_involved
    FROM (SELECT primaryid, lower(trim(pt)) AS p, count(*) AS n FROM reac GROUP BY 1, 2 HAVING count(*) > 1)""")
show("top 20 reactions (distinct cases, case-folded)", """
    SELECT lower(trim(pt)) AS pt, count(DISTINCT primaryid) AS cases FROM reac GROUP BY 1 ORDER BY 2 DESC LIMIT 20""", 20)


# ============================ H. OUTC + RPSR =================================
section("H. OUTC (outcomes) and RPSR (report sources)")
dist("outc", "outc_cod", 10)
show("outcome coverage", """
    SELECT (SELECT count(*) FROM demo) AS cases,
           (SELECT count(DISTINCT primaryid) FROM outc) AS cases_with_any_outcome,
           round(100.0 * (SELECT count(DISTINCT primaryid) FROM outc) / (SELECT count(*) FROM demo), 2) AS pct_cases_with_outcome""")
show("worst outcome per case (DE > LT > HO > DS > CA > RI > OT)", """
    WITH o AS (SELECT primaryid, max(CASE outc_cod WHEN 'DE' THEN 7 WHEN 'LT' THEN 6 WHEN 'HO' THEN 5 WHEN 'DS' THEN 4
                                         WHEN 'CA' THEN 3 WHEN 'RI' THEN 2 WHEN 'OT' THEN 1 ELSE 0 END) AS r FROM outc GROUP BY 1)
    SELECT CASE r WHEN 7 THEN 'DE' WHEN 6 THEN 'LT' WHEN 5 THEN 'HO' WHEN 4 THEN 'DS' WHEN 3 THEN 'CA'
                  WHEN 2 THEN 'RI' WHEN 1 THEN 'OT' ELSE 'unrecognised' END AS worst_outcome, count(*) AS cases
    FROM o GROUP BY r ORDER BY r DESC""")
show("outcome rows per case", """
    WITH c AS (SELECT primaryid, count(*) AS n FROM outc GROUP BY 1)
    SELECT n AS outcome_rows_per_case, count(*) AS cases FROM c GROUP BY 1 ORDER BY 1""")
dist("rpsr", "rpsr_cod", 12)
show("RPSR coverage", """
    SELECT count(DISTINCT primaryid) AS cases_with_source,
           round(100.0 * count(DISTINCT primaryid) / (SELECT count(*) FROM demo), 2) AS pct_of_cases FROM rpsr""")


# ============================ I. THER ========================================
section("I. THER - therapy dates, and whether time-to-onset is feasible")
show("shape", """
    SELECT count(*) AS rows, count(DISTINCT primaryid) AS cases_with_therapy_rows,
           count(*) FILTER (WHERE start_dt IS NULL) AS start_null, count(*) FILTER (WHERE end_dt IS NULL) AS end_null,
           count(*) FILTER (WHERE dur IS NOT NULL) AS dur_filled FROM ther""")
show("start_dt / end_dt precision", """
    SELECT 'start_dt' AS col, CASE WHEN start_dt IS NULL THEN 'NULL' WHEN length(start_dt)=8 THEN 'YYYYMMDD'
                                   WHEN length(start_dt)=6 THEN 'YYYYMM' WHEN length(start_dt)=4 THEN 'YYYY' ELSE 'other' END AS precision,
           count(*) AS n FROM ther GROUP BY 2
    UNION ALL
    SELECT 'end_dt', CASE WHEN end_dt IS NULL THEN 'NULL' WHEN length(end_dt)=8 THEN 'YYYYMMDD'
                          WHEN length(end_dt)=6 THEN 'YYYYMM' WHEN length(end_dt)=4 THEN 'YYYY' ELSE 'other' END, count(*) FROM ther GROUP BY 2
    ORDER BY 1, 2""")
dist("ther", "dur_cod", 10)
show("time-to-onset feasibility for PS drugs (needs a FULL start date and a FULL event date)", """
    WITH ps AS (SELECT DISTINCT primaryid, drug_seq FROM drug WHERE role_cod = 'PS'),
         st AS (SELECT primaryid, dsg_drug_seq AS drug_seq, min(start_dt) FILTER (WHERE length(start_dt) = 8) AS start_dt
                FROM ther GROUP BY 1, 2),
         j AS (SELECT p.primaryid, s.start_dt, d.event_dt
               FROM ps p LEFT JOIN st s ON s.primaryid = p.primaryid AND s.drug_seq = p.drug_seq
                         LEFT JOIN demo d ON d.primaryid = p.primaryid)
    SELECT count(*) AS ps_drug_entries,
           count(start_dt) AS with_full_start,
           count(*) FILTER (WHERE length(event_dt) = 8) AS with_full_event,
           count(*) FILTER (WHERE start_dt IS NOT NULL AND length(event_dt) = 8) AS usable_for_onset,
           round(100.0 * count(*) FILTER (WHERE start_dt IS NOT NULL AND length(event_dt) = 8) / count(*), 2) AS pct_usable
    FROM j""")
show("time-to-onset distribution (days from therapy start to event, usable rows only)", """
    WITH ps AS (SELECT DISTINCT primaryid, drug_seq FROM drug WHERE role_cod = 'PS'),
         st AS (SELECT primaryid, dsg_drug_seq AS drug_seq, min(start_dt) FILTER (WHERE length(start_dt) = 8) AS start_dt
                FROM ther GROUP BY 1, 2),
         j AS (SELECT date_diff('day', try_strptime(s.start_dt,'%Y%m%d'), try_strptime(d.event_dt,'%Y%m%d')) AS onset
               FROM ps p JOIN st s ON s.primaryid = p.primaryid AND s.drug_seq = p.drug_seq
                         JOIN demo d ON d.primaryid = p.primaryid
               WHERE s.start_dt IS NOT NULL AND length(d.event_dt) = 8)
    SELECT CASE WHEN onset < 0 THEN 'negative (event BEFORE therapy start)' WHEN onset <= 1 THEN '0-1 days'
                WHEN onset <= 7 THEN '2-7 days' WHEN onset <= 30 THEN '8-30 days' WHEN onset <= 90 THEN '31-90 days'
                WHEN onset <= 365 THEN '91-365 days' ELSE 'over 365 days' END AS onset_bucket, count(*) AS n
    FROM j WHERE onset IS NOT NULL GROUP BY 1 ORDER BY min(onset)""")


# ============================ J. INDI ========================================
section("J. INDI - indications")
show("shape", """
    SELECT count(*) AS rows, count(DISTINCT primaryid) AS cases_with_indications, count(DISTINCT indi_pt) AS distinct_indi_pt,
           count(*) FILTER (WHERE indi_pt = 'Product used for unknown indication') AS unknown_indication_rows FROM indi""")
show("top 15 indications", "SELECT indi_pt, count(*) AS n FROM indi GROUP BY 1 ORDER BY 2 DESC LIMIT 15", 15)
show("PS drugs that have at least one indication row", """
    WITH ps AS (SELECT DISTINCT primaryid, drug_seq FROM drug WHERE role_cod = 'PS'),
         i AS (SELECT DISTINCT primaryid, indi_drug_seq AS drug_seq FROM indi)
    SELECT count(*) AS ps_drug_entries, count(i.primaryid) AS with_indication,
           round(100.0 * count(i.primaryid) / count(*), 2) AS pct
    FROM ps LEFT JOIN i ON i.primaryid = ps.primaryid AND i.drug_seq = ps.drug_seq""")


# ============================ K. JOIN INTEGRITY ==============================
section("K. JOIN INTEGRITY - orphans and coverage")
orph = " UNION ALL ".join(f"""
    SELECT '{t}' AS child_table, count(*) AS rows,
           count(*) FILTER (WHERE d.primaryid IS NULL) AS orphan_rows_no_demo,
           count(*) FILTER (WHERE d.primaryid IS NOT NULL AND d.caseid <> c.caseid) AS caseid_mismatch
    FROM {t} c LEFT JOIN demo d ON d.primaryid = c.primaryid""" for t in ["drug", "reac", "outc", "rpsr", "ther", "indi"])
show("child rows whose primaryid is missing in DEMO / caseid disagrees with DEMO", orph)
show("THER / INDI rows whose drug_seq has no matching DRUG row", """
    SELECT 'ther' AS child_table, count(*) AS rows, count(*) FILTER (WHERE g.primaryid IS NULL) AS no_matching_drug_seq
    FROM ther c LEFT JOIN (SELECT DISTINCT primaryid, drug_seq FROM drug) g
         ON g.primaryid = c.primaryid AND g.drug_seq = c.dsg_drug_seq
    UNION ALL
    SELECT 'indi', count(*), count(*) FILTER (WHERE g.primaryid IS NULL)
    FROM indi c LEFT JOIN (SELECT DISTINCT primaryid, drug_seq FROM drug) g
         ON g.primaryid = c.primaryid AND g.drug_seq = c.indi_drug_seq""")
show("what share of DEMO cases has at least one row in each file?", """
    SELECT count(*) AS cases,
      round(100.0 * count(*) FILTER (WHERE EXISTS (SELECT 1 FROM drug  x WHERE x.primaryid = d.primaryid)) / count(*), 2) AS pct_with_drug,
      round(100.0 * count(*) FILTER (WHERE EXISTS (SELECT 1 FROM reac  x WHERE x.primaryid = d.primaryid)) / count(*), 2) AS pct_with_reac,
      round(100.0 * count(*) FILTER (WHERE EXISTS (SELECT 1 FROM outc  x WHERE x.primaryid = d.primaryid)) / count(*), 2) AS pct_with_outc,
      round(100.0 * count(*) FILTER (WHERE EXISTS (SELECT 1 FROM rpsr  x WHERE x.primaryid = d.primaryid)) / count(*), 2) AS pct_with_rpsr,
      round(100.0 * count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ther  x WHERE x.primaryid = d.primaryid)) / count(*), 2) AS pct_with_ther,
      round(100.0 * count(*) FILTER (WHERE EXISTS (SELECT 1 FROM indi  x WHERE x.primaryid = d.primaryid)) / count(*), 2) AS pct_with_indi
    FROM demo d""")


# ============================ L. DELETE FILES ================================
section("L. DELETE FILES (all quarters found in the raw folder) vs this quarter's DEMO")
con.execute("CREATE TABLE del_ids (src VARCHAR, caseid VARCHAR)")
del_files = sorted(p for p in RAW_DIR.rglob("*.txt")
                   if p.is_file() and p.suffix.lower() == ".txt" and p.stem.lower().startswith("delete"))
bad_lines = 0
for p in del_files:
    m = re.search(r"(\d\dQ\d)", p.stem)
    src = m.group(1) if m else p.stem
    lines = [ln.strip() for ln in p.read_text(encoding="latin-1").splitlines() if ln.strip()]
    good = [ln for ln in lines if ln.isdigit()]
    bad_lines += len(lines) - len(good)
    con.executemany("INSERT INTO del_ids VALUES (?, ?)", [(src, g) for g in good])
say(f"Delete files read: {len(del_files)} | non-numeric lines skipped: {bad_lines}")
show("per delete file: ids listed, how many exist in THIS quarter's DEMO", """
    SELECT src AS delete_file_quarter, count(*) AS ids_listed, count(DISTINCT caseid) AS unique_ids,
           count(DISTINCT caseid) FILTER (WHERE caseid IN (SELECT caseid FROM demo)) AS ids_found_in_demo
    FROM del_ids GROUP BY 1 ORDER BY 1""")
show("id format check (lengths): delete ids vs DEMO caseid vs DEMO primaryid", """
    SELECT 'delete ids' AS source, length(caseid) AS len, count(*) AS n FROM del_ids GROUP BY 2
    UNION ALL SELECT 'demo caseid', length(caseid), count(*) FROM demo GROUP BY 2
    UNION ALL SELECT 'demo primaryid', length(primaryid), count(*) FROM demo GROUP BY 2
    ORDER BY 1, 2""")
show("rows that WOULD be removed from this quarter by the delete rule (union of all delete files)", """
    SELECT 'demo' AS table_name, count(*) AS rows_removed FROM demo WHERE caseid IN (SELECT caseid FROM del_ids)
    UNION ALL SELECT 'drug', count(*) FROM drug WHERE caseid IN (SELECT caseid FROM del_ids)
    UNION ALL SELECT 'reac', count(*) FROM reac WHERE caseid IN (SELECT caseid FROM del_ids)
    UNION ALL SELECT 'outc', count(*) FROM outc WHERE caseid IN (SELECT caseid FROM del_ids)
    UNION ALL SELECT 'rpsr', count(*) FROM rpsr WHERE caseid IN (SELECT caseid FROM del_ids)
    UNION ALL SELECT 'ther', count(*) FROM ther WHERE caseid IN (SELECT caseid FROM del_ids)
    UNION ALL SELECT 'indi', count(*) FROM indi WHERE caseid IN (SELECT caseid FROM del_ids)""")
show("for matched cases: which delete file lists them, and is it the same quarter or a later one?", """
    SELECT x.src AS listed_in_delete_file, count(DISTINCT d.caseid) AS cases
    FROM demo d JOIN del_ids x ON x.caseid = d.caseid GROUP BY 1 ORDER BY 1""")


# ============================ M. DUPLICATE CANDIDATES ========================
section("M. DUPLICATE CANDIDATES (measured only - nothing is removed)")
show("same mfr_num under more than one caseid", """
    SELECT count(*) AS mfr_nums_shared, coalesce(sum(n),0) AS cases_involved
    FROM (SELECT mfr_num, count(DISTINCT caseid) AS n FROM demo WHERE mfr_num IS NOT NULL
          GROUP BY 1 HAVING count(DISTINCT caseid) > 1)""")
show("published 'extreme duplication' rule (same PS active ingredient + age + sex + event date + reporter country + PT, "
     "different caseid); complete records only", """
    WITH base AS (
        SELECT d.caseid, upper(trim(g.prod_ai)) AS ai, d.age || '|' || d.age_cod AS age_key, d.sex,
               d.event_dt, d.reporter_country, r.pt
        FROM demo d JOIN drug g ON g.primaryid = d.primaryid JOIN reac r ON r.primaryid = d.primaryid
        WHERE g.role_cod = 'PS' AND g.prod_ai IS NOT NULL AND d.age IS NOT NULL AND d.age_cod IS NOT NULL
          AND d.sex IN ('M', 'F') AND length(d.event_dt) = 8 AND d.reporter_country IS NOT NULL),
    grp AS (SELECT ai, age_key, sex, event_dt, reporter_country, pt, count(DISTINCT caseid) AS n
            FROM base GROUP BY ALL HAVING count(DISTINCT caseid) > 1)
    SELECT (SELECT count(*) FROM base) AS complete_case_drug_reaction_rows,
           count(*) AS duplicate_groups, coalesce(sum(n), 0) AS cases_in_those_groups, coalesce(max(n), 0) AS largest_group
    FROM grp""")

# ============================ DONE ===========================================
section("DONE")
say(f"Total run time: {time.time() - _t0:,.1f} seconds")
say(f"Report written to: {REPORT_PATH}")
say("Next: paste or upload this report so we can decide the cleaning rules from real numbers.")
con.close()
_report.close()
