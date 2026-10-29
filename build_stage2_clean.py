"""
FAERS STAGE 2 - build the CLEAN layer from the Stage 1 consolidated tables.

Principles
  * Stage 1 database is attached READ-ONLY and never modified.
  * For source fields projected into the clean layer, raw values are retained in *_raw columns where applicable.
    Stage 1 remains the source of truth for original columns not projected into these analysis tables.
  * Bad or doubtful values are never silently fixed: they get a *_status flag and the
    cleaned column is left blank (NULL) when the value cannot be trusted.
  * Decisions that are still open (parent-molecule mapping, biosimilars, combinations,
    country names) are NOT applied here. Review CSVs are produced instead.

Rules implemented (rules version below)
  R1  Time axis: first_received_* from INIT_FDA_DT; pre-window cases kept in their own bucket.
  R2  Age: converted to years using AGE_COD first; 0-120 only; bands from the number,
      FDA AGE_GRP only as fallback; disagreements flagged, both kept.
  R3  Reporter type from OCCP_COD with an explicit 'Not reported'; report channel from REPT_COD.
  R4  Duplicate candidates FLAGGED at case level (published 6-field rule); nothing is deleted.
  R5  'Provisional' = cases first received in the most recent quarters of THIS extract.
  R6  Drug identity: PROD_AI (trim/upper only), brand name kept raw, repeated drug_seq rows collapsed.
  R7  Event date invalid ONLY if malformed, before 1900, or after the case's latest FDA receipt.
      (An event after the FIRST receipt is normal in follow-up versions: informational flag only.)
  R8  Primary suspect: role PS; if absent, first listed ADMINISTERED drug (never DN) flagged as fallback.
  R9  Weight in kg, 0.2-500; sex F/M/Unknown; DN rows kept but flagged not administered.
  R10 Reactions: one row per case + reaction term, one canonical casing per term.
  R11 Serious = any outcome code; worst outcome ranked DE>LT>HO>DS>CA>RI>OT; death from DE.
  R12 Onset: full dates only, linked through drug_seq; negative intervals flagged.

Input : faers_stage1.duckdb      Output: faers_clean.duckdb + stage2_report.txt + review_*.csv
Run   : python build_stage2_clean.py
        (or: python build_stage2_clean.py "C:\\...\\faers_stage1.duckdb" "C:\\...\\faers_clean")
Needs about 3 GB free disk space. Close programs that have the Stage 1 database open.
"""
import sys
import time
from pathlib import Path

import duckdb

# =============================== SETTINGS ====================================
STAGE1_DB = Path(r"C:\path\to\faers_stage1.duckdb")
OUT_DIR = Path(r"C:\path\to\faers_clean")
MEMORY_LIMIT = "4GB"
PROVISIONAL_QUARTERS = 2     # cases first received in the last N quarters of the extract are 'provisional'
REVIEW_TOP_N = 1500          # rows in the ingredient review list
RULES_VERSION = "stage2-v1"
# =============================================================================
if len(sys.argv) >= 3:
    STAGE1_DB, OUT_DIR = Path(sys.argv[1]), Path(sys.argv[2])

OUT_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = OUT_DIR / "faers_clean.duckdb"
REPORT_PATH = OUT_DIR / "stage2_report.txt"
TMP = OUT_DIR / "duckdb_tmp"
TMP.mkdir(exist_ok=True)
if DB_PATH.exists():
    DB_PATH.unlink()
if not STAGE1_DB.exists():
    sys.exit(f"Stage 1 database not found: {STAGE1_DB}")

_report = open(REPORT_PATH, "w", encoding="utf-8")
_t0 = time.time()
_last = time.time()


def say(text=""):
    print(text)
    _report.write(text + "\n")
    _report.flush()


def step(title):
    global _last
    now = time.time()
    say(f"\n[{now - _t0:7.0f}s elapsed | +{now - _last:5.0f}s] {title}")
    _last = now


def _fmt(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v).replace("\n", " ")[:70]


def show(title, sql, max_rows=40):
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


def scalar(sql):
    return con.execute(sql).fetchone()[0]


def run(sql):
    con.execute(sql)


# -------------------------------- connect ------------------------------------
con = duckdb.connect(str(DB_PATH))
con.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
con.execute(f"SET temp_directory='{TMP.as_posix()}'")
con.execute("SET preserve_insertion_order=false")
con.execute("SET threads TO 4")
con.execute(f"ATTACH '{STAGE1_DB.as_posix()}' AS s1 (READ_ONLY)")

say("FAERS STAGE 2 - CLEAN LAYER BUILD REPORT")
say(time.strftime("Run started: %Y-%m-%d %H:%M:%S") + f" | rules {RULES_VERSION}")
say(f"Stage 1 input : {STAGE1_DB}")
say(f"Output        : {DB_PATH}")

FIRST_Q = scalar("SELECT min(source_quarter) FROM s1.stg_demo")
LAST_Q = scalar("SELECT max(source_quarter) FROM s1.stg_demo")


def q_start(label):
    return f"{2000 + int(label[:2])}-{(int(label[-1]) - 1) * 3 + 1:02d}-01"


WINDOW_START = q_start(FIRST_Q)
LAST_Q_START = q_start(LAST_Q)
PROV_START = str(scalar(f"SELECT (DATE '{LAST_Q_START}' - INTERVAL {3 * (PROVISIONAL_QUARTERS - 1)} MONTH)::DATE"))
say(f"Window starts {WINDOW_START} ({FIRST_Q}); latest quarter {LAST_Q}; provisional = first received on/after {PROV_START}")

run("CREATE TABLE clean_meta (key VARCHAR, value VARCHAR)")
con.executemany("INSERT INTO clean_meta VALUES (?, ?)", [
    ("rules_version", RULES_VERSION), ("built_at", time.strftime("%Y-%m-%d %H:%M:%S")),
    ("first_quarter", FIRST_Q), ("latest_quarter", LAST_Q), ("window_start", WINDOW_START),
    ("provisional_from", PROV_START), ("stage1_database", str(STAGE1_DB))])

# ================================ DRUG =======================================
step("clean_drug: one row per case + drug_seq (repeated rows collapsed; dosing moved to clean_dose)")
run("""
CREATE TABLE clean_drug AS
WITH g AS (
  SELECT primaryid, caseid, drug_seq AS drug_seq_raw,
         min(upper(trim(role_cod))) AS role_cod, max(upper(trim(role_cod))) AS role_cod_max,
         min(trim(drugname)) AS drugname_raw, max(trim(drugname)) AS drugname_max,
         min(trim(prod_ai)) AS prod_ai_raw, max(trim(prod_ai)) AS prod_ai_max,
         min(trim(val_vbm)) AS val_vbm,
         max(CASE upper(trim(dechal)) WHEN 'Y' THEN 4 WHEN 'N' THEN 3 WHEN 'U' THEN 2 WHEN 'D' THEN 1 END) AS dechal_rank,
         max(CASE upper(trim(rechal)) WHEN 'Y' THEN 4 WHEN 'N' THEN 3 WHEN 'U' THEN 2 WHEN 'D' THEN 1 END) AS rechal_rank,
         min(trim(nda_num)) AS nda_num, count(*) AS n_source_rows
  FROM s1.stg_drug GROUP BY primaryid, caseid, drug_seq)
SELECT primaryid, caseid, drug_seq_raw, try_cast(drug_seq_raw AS INTEGER) AS drug_seq,
       nullif(role_cod, '') AS role_cod,
       CASE role_cod WHEN 'PS' THEN 'Primary suspect' WHEN 'SS' THEN 'Secondary suspect' WHEN 'C' THEN 'Concomitant'
                     WHEN 'I' THEN 'Interacting' WHEN 'DN' THEN 'Drug not administered' ELSE 'Unrecognised/blank' END AS role_label,
       (coalesce(role_cod, '') <> 'DN') AS is_administered,
       drugname_raw,
       nullif(upper(regexp_replace(drugname_raw, '\\s+', ' ', 'g')), '') AS drugname_std,
       prod_ai_raw,
       nullif(upper(regexp_replace(prod_ai_raw, '\\s+', ' ', 'g')), '') AS prod_ai_std,
       coalesce(nullif(upper(regexp_replace(prod_ai_raw, '\\s+', ' ', 'g')), ''),
                nullif(upper(regexp_replace(drugname_raw, '\\s+', ' ', 'g')), '')) AS ingredient_std,
       CASE WHEN nullif(upper(regexp_replace(prod_ai_raw, '\\s+', ' ', 'g')), '') IS NOT NULL THEN 'prod_ai'
            WHEN nullif(upper(regexp_replace(drugname_raw, '\\s+', ' ', 'g')), '') IS NOT NULL THEN 'drugname_fallback'
            ELSE 'missing' END AS ingredient_source,
       coalesce(strpos(coalesce(nullif(upper(prod_ai_raw), ''), upper(drugname_raw), ''), chr(92)) > 0, FALSE) AS is_combination,
       length(coalesce(nullif(prod_ai_raw, ''), drugname_raw, '')) - length(replace(coalesce(nullif(prod_ai_raw, ''), drugname_raw, ''), chr(92), '')) + 1 AS n_ingredients,
       nullif(val_vbm, '') AS val_vbm,
       CASE dechal_rank WHEN 4 THEN 'Y' WHEN 3 THEN 'N' WHEN 2 THEN 'U' WHEN 1 THEN 'D' END AS dechallenge,
       CASE rechal_rank WHEN 4 THEN 'Y' WHEN 3 THEN 'N' WHEN 2 THEN 'U' WHEN 1 THEN 'D' END AS rechallenge,
       (rechal_rank = 4) AS rechallenge_positive,
       nullif(nda_num, '') AS nda_num, (nda_num = '999999') AS is_otc_marker,
       n_source_rows,
       (role_cod <> role_cod_max OR coalesce(drugname_raw, '') <> coalesce(drugname_max, '')
        OR coalesce(prod_ai_raw, '') <> coalesce(prod_ai_max, '')) AS identity_conflict
FROM g""")
# ingredient_std must stay intact for combination products: recompute flag cleanly from the standardised text
run("UPDATE clean_drug SET n_ingredients = CASE WHEN ingredient_std IS NULL THEN NULL ELSE "
    "length(ingredient_std) - length(replace(ingredient_std, chr(92), '')) + 1 END, "
    "is_combination = coalesce(strpos(ingredient_std, chr(92)) > 0, FALSE)")

step("clean_dose: dosing detail for rows that carry any (kept separate so identity stays one row per drug)")
run("""
CREATE TABLE clean_dose AS
SELECT primaryid, caseid, drug_seq AS drug_seq_raw, upper(trim(role_cod)) AS role_cod,
       nullif(trim(route), '') AS route, nullif(trim(dose_vbm), '') AS dose_verbatim, nullif(trim(dose_amt), '') AS dose_amt,
       nullif(trim(dose_unit), '') AS dose_unit, nullif(trim(dose_form), '') AS dose_form, nullif(trim(dose_freq), '') AS dose_freq,
       nullif(trim(cum_dose_chr), '') AS cum_dose, nullif(trim(cum_dose_unit), '') AS cum_dose_unit
FROM s1.stg_drug
WHERE nullif(trim(dose_vbm), '') IS NOT NULL OR nullif(trim(dose_amt), '') IS NOT NULL OR nullif(trim(dose_unit), '') IS NOT NULL
   OR nullif(trim(dose_form), '') IS NOT NULL OR nullif(trim(dose_freq), '') IS NOT NULL OR nullif(trim(route), '') IS NOT NULL
   OR nullif(trim(cum_dose_chr), '') IS NOT NULL""")

# ================================ REACTION ===================================
step("clean_reaction: one row per case + reaction term, one canonical casing per term")
run("""
CREATE TABLE _pt_map AS
WITH f AS (SELECT lower(trim(pt)) AS pt_key, trim(pt) AS form, count(*) AS n
           FROM s1.stg_reac WHERE nullif(trim(pt), '') IS NOT NULL GROUP BY 1, 2),
     r AS (SELECT *, row_number() OVER (PARTITION BY pt_key ORDER BY (form <> upper(form)) DESC, n DESC, form) AS rn,
                  count(*) OVER (PARTITION BY pt_key) AS n_forms FROM f)
SELECT pt_key, n_forms,
       CASE WHEN form <> upper(form) THEN form ELSE upper(substr(form, 1, 1)) || lower(substr(form, 2)) END AS pt_std
FROM r WHERE rn = 1""")
run("""
CREATE TABLE clean_reaction AS
SELECT x.primaryid, x.caseid, m.pt_std AS pt, m.pt_key, count(*) AS n_source_rows,
       bool_or(nullif(trim(x.drug_rec_act), '') IS NOT NULL) AS recurred_on_rechallenge
FROM s1.stg_reac x JOIN _pt_map m ON m.pt_key = lower(trim(x.pt))
GROUP BY x.primaryid, x.caseid, m.pt_std, m.pt_key""")

# ================================ OUTCOME ====================================
step("clean_outcome: one row per case + outcome code")
run("""
CREATE TABLE clean_outcome AS
SELECT primaryid, caseid, upper(trim(outc_cod)) AS outcome_code,
       CASE upper(trim(outc_cod)) WHEN 'DE' THEN 'Death' WHEN 'LT' THEN 'Life-threatening' WHEN 'HO' THEN 'Hospitalization'
            WHEN 'DS' THEN 'Disability' WHEN 'CA' THEN 'Congenital anomaly' WHEN 'RI' THEN 'Required intervention'
            WHEN 'OT' THEN 'Other serious' ELSE 'Unrecognised' END AS outcome_label,
       CASE upper(trim(outc_cod)) WHEN 'DE' THEN 7 WHEN 'LT' THEN 6 WHEN 'HO' THEN 5 WHEN 'DS' THEN 4 WHEN 'CA' THEN 3
            WHEN 'RI' THEN 2 WHEN 'OT' THEN 1 ELSE 0 END AS severity_rank
FROM s1.stg_outc WHERE nullif(trim(outc_cod), '') IS NOT NULL GROUP BY primaryid, caseid, upper(trim(outc_cod))""")

# ================================ CASE (part 1) ==============================
step("clean_case (building): standardising demographics, dates, reporter, geography")
run(f"""
CREATE TABLE _c_demo AS
WITH b AS (
  SELECT caseid, primaryid, try_cast(caseversion AS INTEGER) AS caseversion, source_quarter,
         nullif(trim(init_fda_dt), '') AS init_raw, nullif(trim(fda_dt), '') AS fda_raw, nullif(trim(event_dt), '') AS event_raw,
         nullif(trim(age), '') AS age_raw, nullif(trim(age_cod), '') AS age_cod_raw, nullif(trim(age_grp), '') AS age_grp_raw,
         nullif(trim(wt), '') AS wt_raw, nullif(trim(wt_cod), '') AS wt_cod_raw, nullif(trim(sex), '') AS sex_raw,
         nullif(trim(rept_cod), '') AS rept_cod_raw, nullif(trim(occp_cod), '') AS occp_cod_raw,
         nullif(trim(reporter_country), '') AS reporter_country_raw, nullif(trim(occr_country), '') AS occr_country_raw,
         nullif(trim(mfr_sndr), '') AS mfr_sndr_raw
  FROM s1.stg_demo),
d AS (
  SELECT *,
    CASE WHEN regexp_matches(init_raw, '^[0-9]{{8}}$') THEN try_strptime(init_raw, '%Y%m%d')::DATE END AS init_date,
    CASE WHEN regexp_matches(fda_raw, '^[0-9]{{8}}$') THEN try_strptime(fda_raw, '%Y%m%d')::DATE END AS fda_date,
    CASE WHEN regexp_matches(event_raw, '^[0-9]{{8}}$') THEN try_strptime(event_raw, '%Y%m%d')::DATE END AS event_parsed,
    CASE WHEN regexp_matches(age_raw, '^[0-9]+(\\.[0-9]+)?$') THEN age_raw::DOUBLE END AS age_num,
    CASE WHEN regexp_matches(wt_raw, '^[0-9]+(\\.[0-9]+)?$') THEN wt_raw::DOUBLE END AS wt_num
  FROM b),
a AS (
  SELECT *,
    CASE upper(age_cod_raw) WHEN 'YR' THEN age_num WHEN 'DEC' THEN age_num * 10 WHEN 'MON' THEN age_num / 12.0
         WHEN 'WK' THEN age_num / 52.1775 WHEN 'DY' THEN age_num / 365.2425 WHEN 'HR' THEN age_num / 8765.82 END AS age_years_conv,
    CASE upper(wt_cod_raw) WHEN 'KG' THEN wt_num WHEN 'LBS' THEN wt_num * 0.45359237 WHEN 'GMS' THEN wt_num / 1000.0 END AS wt_kg_conv
  FROM d),
s AS (
  SELECT *,
    CASE WHEN age_raw IS NULL THEN 'missing' WHEN age_num IS NULL THEN 'non_numeric' WHEN age_cod_raw IS NULL THEN 'no_unit'
         WHEN age_years_conv IS NULL THEN 'unknown_unit' WHEN age_years_conv < 0 OR age_years_conv > 120 THEN 'implausible'
         ELSE 'ok' END AS age_status,
    CASE WHEN wt_raw IS NULL THEN 'missing' WHEN wt_num IS NULL THEN 'non_numeric' WHEN wt_cod_raw IS NULL THEN 'no_unit'
         WHEN wt_kg_conv IS NULL THEN 'unknown_unit' WHEN wt_kg_conv < 0.2 OR wt_kg_conv > 500 THEN 'implausible'
         ELSE 'ok' END AS wt_status,
    CASE WHEN event_raw IS NULL THEN 'missing'
         WHEN NOT regexp_matches(event_raw, '^[0-9]{{4}}([0-9]{{2}}([0-9]{{2}})?)?$') THEN 'malformed'
         WHEN length(event_raw) < 8 THEN 'partial'
         WHEN event_parsed IS NULL THEN 'malformed'
         WHEN event_parsed < DATE '1900-01-01' THEN 'invalid_before_1900'
         WHEN fda_date IS NOT NULL AND event_parsed > fda_date THEN 'invalid_after_latest_receipt'
         ELSE 'ok' END AS event_date_status
  FROM a),
f AS (
  SELECT *,
    CASE WHEN age_status = 'ok' THEN age_years_conv END AS age_years,
    CASE WHEN wt_status = 'ok' THEN wt_kg_conv END AS weight_kg,
    CASE WHEN event_date_status = 'ok' THEN event_parsed END AS event_date,
    coalesce(init_date, fda_date) AS first_received_date,
    CASE upper(sex_raw) WHEN 'F' THEN 'Female' WHEN 'M' THEN 'Male' ELSE 'Unknown' END AS sex_std,
    CASE WHEN upper(occr_country_raw) IN ('COUNTRY NOT SPECIFIED') THEN NULL ELSE upper(occr_country_raw) END AS occr_country_clean,
    CASE WHEN upper(reporter_country_raw) IN ('COUNTRY NOT SPECIFIED') THEN NULL ELSE upper(reporter_country_raw) END AS reporter_country_clean
  FROM s),
g AS (
  SELECT *,
    CASE WHEN age_years IS NULL THEN NULL WHEN age_years < 28 / 365.2425 THEN 'Neonate' WHEN age_years < 2 THEN 'Infant'
         WHEN age_years < 12 THEN 'Child' WHEN age_years < 18 THEN 'Adolescent' WHEN age_years < 65 THEN 'Adult' ELSE 'Elderly' END AS age_band_computed,
    CASE upper(age_grp_raw) WHEN 'N' THEN 'Neonate' WHEN 'I' THEN 'Infant' WHEN 'C' THEN 'Child' WHEN 'T' THEN 'Adolescent'
         WHEN 'A' THEN 'Adult' WHEN 'E' THEN 'Elderly' END AS age_band_fda
  FROM f)
SELECT
  caseid, primaryid, caseversion, source_quarter,
  init_raw AS init_fda_dt_raw, fda_raw AS fda_dt_raw, event_raw AS event_dt_raw,
  first_received_date,
  CASE WHEN init_date IS NOT NULL THEN 'init_fda_dt' WHEN fda_date IS NOT NULL THEN 'fda_dt_fallback' ELSE 'missing' END AS first_received_source,
  date_trunc('quarter', first_received_date)::DATE AS first_received_quarter_start,
  CASE WHEN first_received_date IS NULL THEN 'Unknown' WHEN first_received_date < DATE '{WINDOW_START}' THEN 'Before window'
       ELSE strftime(first_received_date, '%y') || 'Q' || quarter(first_received_date) END AS first_received_quarter,
  CASE WHEN first_received_date IS NULL THEN 'Unknown' WHEN first_received_date < DATE '{WINDOW_START}' THEN 'Before window' ELSE 'In window' END AS first_received_cohort,
  coalesce(date_trunc('quarter', first_received_date)::DATE >= DATE '{PROV_START}', FALSE) AS is_provisional,
  fda_date AS latest_received_date,
  event_date, event_date_status,
  (event_date IS NOT NULL AND init_date IS NOT NULL AND event_date > init_date) AS event_after_first_receipt,
  age_raw, age_cod_raw, age_years, age_status,
  coalesce(age_band_computed, age_band_fda) AS age_band,
  CASE WHEN age_band_computed IS NOT NULL THEN 'computed' WHEN age_band_fda IS NOT NULL THEN 'fda_age_grp' ELSE 'none' END AS age_band_source,
  age_grp_raw, (age_band_computed IS NOT NULL AND age_band_fda IS NOT NULL AND age_band_computed <> age_band_fda) AS age_grp_conflict,
  CASE coalesce(age_band_computed, age_band_fda) WHEN 'Neonate' THEN 1 WHEN 'Infant' THEN 2 WHEN 'Child' THEN 3 WHEN 'Adolescent' THEN 4
       WHEN 'Adult' THEN 5 WHEN 'Elderly' THEN 6 END AS age_band_order,
  CASE WHEN coalesce(age_band_computed, age_band_fda) IN ('Neonate', 'Infant', 'Child', 'Adolescent') THEN 'Pediatric (<18)'
       WHEN coalesce(age_band_computed, age_band_fda) = 'Adult' THEN 'Adult (18-64)'
       WHEN coalesce(age_band_computed, age_band_fda) = 'Elderly' THEN 'Elderly (65+)' ELSE 'Unknown' END AS age_cohort,
  wt_raw, wt_cod_raw, weight_kg, wt_status,
  sex_raw, sex_std,
  rept_cod_raw,
  CASE upper(rept_cod_raw) WHEN 'EXP' THEN 'Manufacturer - expedited' WHEN 'PER' THEN 'Manufacturer - periodic'
       WHEN 'DIR' THEN 'Direct to FDA' WHEN '5DAY' THEN 'Combination product - 5-day' WHEN '30DAY' THEN 'Combination product - 30-day'
       WHEN NULL THEN 'Not reported' ELSE coalesce('Unrecognised code', 'Not reported') END AS report_channel,
  CASE WHEN rept_cod_raw IS NULL THEN 'Not reported' WHEN upper(rept_cod_raw) IN ('EXP', 'PER') THEN 'Manufacturer'
       WHEN upper(rept_cod_raw) = 'DIR' THEN 'Direct' WHEN upper(rept_cod_raw) IN ('5DAY', '30DAY') THEN 'Combination product'
       ELSE 'Unrecognised code' END AS channel_group,
  occp_cod_raw,
  CASE WHEN occp_cod_raw IS NULL THEN 'Not reported' ELSE
       CASE upper(occp_cod_raw) WHEN 'MD' THEN 'Physician' WHEN 'PH' THEN 'Pharmacist' WHEN 'HP' THEN 'Healthcare professional (unspecified)'
            WHEN 'OT' THEN 'Other healthcare professional' WHEN 'LW' THEN 'Lawyer' WHEN 'CN' THEN 'Consumer' ELSE 'Unrecognised code' END END AS reporter_type,
  CASE WHEN occp_cod_raw IS NULL THEN 'Not reported' ELSE
       CASE WHEN upper(occp_cod_raw) IN ('MD', 'PH', 'HP', 'OT') THEN 'Healthcare professional'
            WHEN upper(occp_cod_raw) = 'CN' THEN 'Consumer' WHEN upper(occp_cod_raw) = 'LW' THEN 'Lawyer' ELSE 'Unrecognised code' END END AS reporter_group,
  reporter_country_raw, occr_country_raw, reporter_country_clean, occr_country_clean,
  CASE WHEN occr_country_raw IS NULL THEN 'missing' WHEN upper(occr_country_raw) = 'COUNTRY NOT SPECIFIED' THEN 'not_specified'
       WHEN upper(occr_country_raw) = 'EU' THEN 'region_code' WHEN NOT regexp_matches(upper(occr_country_raw), '^[A-Z]{{2}}$') THEN 'non_standard_code'
       ELSE 'ok' END AS occr_country_status,
  CASE WHEN reporter_country_raw IS NULL THEN 'missing' WHEN upper(reporter_country_raw) = 'COUNTRY NOT SPECIFIED' THEN 'not_specified'
       WHEN upper(reporter_country_raw) = 'EU' THEN 'region_code' WHEN NOT regexp_matches(upper(reporter_country_raw), '^[A-Z]{{2}}$') THEN 'non_standard_code'
       ELSE 'ok' END AS reporter_country_status,
  coalesce(occr_country_clean, reporter_country_clean) AS geo_country_code,
  CASE WHEN occr_country_clean IS NOT NULL THEN 'occurrence' WHEN reporter_country_clean IS NOT NULL THEN 'reporter_fallback' ELSE 'none' END AS geo_country_source,
  mfr_sndr_raw, nullif(upper(regexp_replace(mfr_sndr_raw, '\\s+', ' ', 'g')), '') AS mfr_sndr_std,
  (age_raw IS NOT NULL) AS _has_age_raw, age_cod_raw AS _age_cod
FROM g""")
# the report_channel expression above uses a CASE ... WHEN NULL guard that never matches; fix it explicitly
run("UPDATE _c_demo SET report_channel = 'Not reported' WHERE rept_cod_raw IS NULL")

# ================================ PRIMARY SUSPECT ============================
step("primary suspect per case (PS; else first ADMINISTERED drug, flagged; never DN)")
run("""
CREATE TABLE _c_ps AS
SELECT primaryid, drug_seq_raw, drug_seq, role_cod, drugname_raw, drugname_std, prod_ai_std, ingredient_std, ingredient_source,
       is_combination, n_ingredients, CASE WHEN role_cod = 'PS' THEN 'PS' ELSE 'fallback_first_administered' END AS ps_status
FROM (SELECT d.*, row_number() OVER (PARTITION BY primaryid ORDER BY (coalesce(role_cod, '') = 'PS') DESC, drug_seq NULLS LAST, drug_seq_raw) AS rn
      FROM clean_drug d WHERE is_administered)
WHERE rn = 1""")
run("CREATE TABLE _c_drugcount AS SELECT primaryid, count(*) AS n_drug_entries FROM clean_drug GROUP BY 1")

# ================================ DUPLICATE CANDIDATES =======================
step("duplicate candidates (flag only): same PS ingredient + age + sex + event date + reporter country + reaction")
run("""
CREATE TABLE _dup_base AS
SELECT c.caseid, c.primaryid, c.first_received_date, p.ingredient_std AS ai, c.age_raw || '|' || c.age_cod_raw AS age_key,
       c.sex_std, c.event_date, c.reporter_country_clean AS country
FROM _c_demo c JOIN _c_ps p ON p.primaryid = c.primaryid
WHERE p.ps_status = 'PS' AND p.ingredient_std IS NOT NULL AND c.age_raw IS NOT NULL AND c.age_cod_raw IS NOT NULL
  AND c.sex_std IN ('Female', 'Male') AND c.event_date IS NOT NULL AND c.reporter_country_clean IS NOT NULL""")
run("""
CREATE TABLE _dup_rows AS
SELECT b.caseid, b.first_received_date, b.ai, b.age_key, b.sex_std, b.event_date, b.country, r.pt_key
FROM _dup_base b JOIN clean_reaction r ON r.primaryid = b.primaryid""")
run("""
CREATE TABLE _dup_groups AS
SELECT ai, age_key, sex_std, event_date, country, pt_key, count(DISTINCT caseid) AS n_cases
FROM _dup_rows GROUP BY ai, age_key, sex_std, event_date, country, pt_key HAVING count(DISTINCT caseid) > 1""")
run("""
CREATE TABLE _dup_members AS
SELECT caseid, count(*) AS duplicate_group_count, bool_or(rk > 1) AS is_later_duplicate FROM (
  SELECT r.caseid, row_number() OVER (PARTITION BY r.ai, r.age_key, r.sex_std, r.event_date, r.country, r.pt_key
                                       ORDER BY r.first_received_date NULLS LAST, r.caseid) AS rk
  FROM _dup_rows r JOIN _dup_groups g ON g.ai = r.ai AND g.age_key = r.age_key AND g.sex_std = r.sex_std
       AND g.event_date = r.event_date AND g.country = r.country AND g.pt_key = r.pt_key)
GROUP BY caseid""")

# ================================ CASE (final) ===============================
step("clean_case: assembling the final one-row-per-case table")
run("""
CREATE TABLE _c_outc AS
SELECT primaryid, count(*) AS n_outcome_codes, max(severity_rank) AS worst_rank,
       bool_or(outcome_code = 'DE') AS has_death_outcome, bool_or(outcome_code = 'LT') AS has_life_threatening,
       bool_or(outcome_code = 'HO') AS has_hospitalization, bool_or(outcome_code = 'DS') AS has_disability,
       bool_or(outcome_code = 'CA') AS has_congenital_anomaly, bool_or(outcome_code = 'RI') AS has_required_intervention,
       bool_or(outcome_code = 'OT') AS has_other_serious
FROM clean_outcome GROUP BY primaryid""")
run("CREATE TABLE _c_reac_n AS SELECT primaryid, count(*) AS n_reactions FROM clean_reaction GROUP BY 1")
run("""
CREATE TABLE clean_case AS
SELECT c.* EXCLUDE (_has_age_raw, _age_cod),
  coalesce(o.primaryid IS NOT NULL, FALSE) AS has_outcome_code,
  CASE WHEN o.primaryid IS NOT NULL THEN 'Serious outcome coded' ELSE 'No serious outcome coded' END AS serious_flag,
  CASE o.worst_rank WHEN 7 THEN 'Death' WHEN 6 THEN 'Life-threatening' WHEN 5 THEN 'Hospitalization' WHEN 4 THEN 'Disability'
       WHEN 3 THEN 'Congenital anomaly' WHEN 2 THEN 'Required intervention' WHEN 1 THEN 'Other serious' WHEN 0 THEN 'Unrecognised' END AS worst_outcome,
  coalesce(o.n_outcome_codes, 0) AS n_outcome_codes,
  coalesce(o.has_death_outcome, FALSE) AS has_death_outcome, coalesce(o.has_life_threatening, FALSE) AS has_life_threatening,
  coalesce(o.has_hospitalization, FALSE) AS has_hospitalization, coalesce(o.has_disability, FALSE) AS has_disability,
  coalesce(o.has_congenital_anomaly, FALSE) AS has_congenital_anomaly, coalesce(o.has_required_intervention, FALSE) AS has_required_intervention,
  coalesce(o.has_other_serious, FALSE) AS has_other_serious,
  coalesce(r.n_reactions, 0) AS n_reactions,
  CASE WHEN p.primaryid IS NOT NULL THEN p.ps_status WHEN dc.primaryid IS NOT NULL THEN 'none_all_drugs_not_administered'
       ELSE 'none_no_drug_rows' END AS ps_status,
  p.drug_seq_raw AS ps_drug_seq_raw, p.ingredient_std AS ps_ingredient_std, p.ingredient_source AS ps_ingredient_source,
  p.drugname_std AS ps_drugname_std, p.is_combination AS ps_is_combination,
  CASE WHEN m.caseid IS NOT NULL THEN 'candidate' WHEN b.caseid IS NOT NULL THEN 'checked_no_match' ELSE 'not_checkable' END AS duplicate_check_status,
  coalesce(m.duplicate_group_count, 0) AS duplicate_group_count,
  coalesce(m.is_later_duplicate, FALSE) AS is_later_duplicate,
  NOT coalesce(m.is_later_duplicate, FALSE) AS in_duplicate_reduced_set
FROM _c_demo c
LEFT JOIN _c_outc o ON o.primaryid = c.primaryid
LEFT JOIN _c_reac_n r ON r.primaryid = c.primaryid
LEFT JOIN _c_ps p ON p.primaryid = c.primaryid
LEFT JOIN _c_drugcount dc ON dc.primaryid = c.primaryid
LEFT JOIN _dup_members m ON m.caseid = c.caseid
LEFT JOIN _dup_base b ON b.caseid = c.caseid""")

# ================================ ONSET ======================================
step("clean_onset: time from therapy start to event for the primary suspect (full dates only)")
run("""
CREATE TABLE _ther_agg AS
SELECT primaryid, dsg_drug_seq AS drug_seq_raw, count(*) AS n_therapy_rows,
       min(CASE WHEN regexp_matches(trim(start_dt), '^[0-9]{8}$') THEN try_strptime(trim(start_dt), '%Y%m%d')::DATE END) AS start_full,
       max(CASE WHEN nullif(trim(start_dt), '') IS NOT NULL AND NOT regexp_matches(trim(start_dt), '^[0-9]{8}$') THEN 1 ELSE 0 END) AS has_partial_start
FROM s1.stg_ther GROUP BY primaryid, dsg_drug_seq""")
run("""
CREATE TABLE clean_onset AS
WITH j AS (
  SELECT c.caseid, p.primaryid, p.drug_seq_raw, p.ingredient_std, p.ps_status, t.start_full AS therapy_start_date, c.event_date,
         c.latest_received_date, t.primaryid AS t_pid, t.has_partial_start
  FROM _c_ps p JOIN clean_case c ON c.primaryid = p.primaryid
       LEFT JOIN _ther_agg t ON t.primaryid = p.primaryid AND t.drug_seq_raw = p.drug_seq_raw)
SELECT caseid, primaryid, drug_seq_raw, ingredient_std, ps_status, therapy_start_date, event_date,
  CASE WHEN event_date IS NULL THEN 'missing_or_invalid_event_date' WHEN t_pid IS NULL THEN 'no_therapy_row'
       WHEN therapy_start_date IS NULL AND has_partial_start = 1 THEN 'partial_start_only' WHEN therapy_start_date IS NULL THEN 'missing_start'
       WHEN therapy_start_date < DATE '1900-01-01' OR therapy_start_date > latest_received_date THEN 'invalid_start_date'
       WHEN date_diff('day', therapy_start_date, event_date) < 0 THEN 'negative_interval' ELSE 'ok' END AS onset_status,
  CASE WHEN event_date IS NOT NULL AND therapy_start_date IS NOT NULL THEN date_diff('day', therapy_start_date, event_date) END AS onset_days_raw
FROM j""")
run("ALTER TABLE clean_onset ADD COLUMN onset_days BIGINT")
run("UPDATE clean_onset SET onset_days = onset_days_raw WHERE onset_status = 'ok'")
run("ALTER TABLE clean_onset ADD COLUMN onset_bucket VARCHAR")
run("""UPDATE clean_onset SET onset_bucket = CASE WHEN onset_days <= 1 THEN '0-1 days' WHEN onset_days <= 7 THEN '2-7 days'
     WHEN onset_days <= 30 THEN '8-30 days' WHEN onset_days <= 90 THEN '31-90 days' WHEN onset_days <= 365 THEN '91-365 days'
     ELSE 'Over 1 year' END WHERE onset_status = 'ok'""")

# ================================ INDICATION + SOURCE ========================
step("clean_indication and clean_report_source")
run("""
CREATE TABLE clean_indication AS
SELECT primaryid, caseid, indi_drug_seq AS drug_seq_raw, trim(indi_pt) AS indication_pt, count(*) AS n_source_rows,
       (lower(trim(indi_pt)) = 'product used for unknown indication') AS is_unknown_indication
FROM s1.stg_indi WHERE nullif(trim(indi_pt), '') IS NOT NULL GROUP BY primaryid, caseid, indi_drug_seq, trim(indi_pt)""")
run("""
CREATE TABLE clean_report_source AS
SELECT primaryid, caseid, upper(trim(rpsr_cod)) AS source_code,
       CASE upper(trim(rpsr_cod)) WHEN 'HP' THEN 'Health professional' WHEN 'CSM' THEN 'Consumer' WHEN 'FGN' THEN 'Foreign'
            WHEN 'SDY' THEN 'Study' WHEN 'LIT' THEN 'Literature' WHEN 'UF' THEN 'User facility' WHEN 'CR' THEN 'Company representative'
            WHEN 'DT' THEN 'Distributor' WHEN 'OTH' THEN 'Other' ELSE 'Unrecognised' END AS source_label
FROM s1.stg_rpsr WHERE nullif(trim(rpsr_cod), '') IS NOT NULL GROUP BY primaryid, caseid, upper(trim(rpsr_cod))""")

# ================================ AUDIT ======================================
step("audit tables and checks")
say("\n" + "=" * 90 + "\nAUDIT - what each rule did (nothing is hidden)\n" + "=" * 90)

run("CREATE TABLE audit_stage2 (area VARCHAR, item VARCHAR, n BIGINT, note VARCHAR)")


def audit(area, item, sql, note=""):
    n = scalar(sql)
    con.execute("INSERT INTO audit_stage2 VALUES (?, ?, ?, ?)", [area, item, int(n) if n is not None else None, note])
    say(f"   {area:<12} {item:<62} {int(n) if n is not None else 'NULL':>12,}  {note}" if n is not None else f"   {area:<12} {item}: NULL")


say("\n-- row counts: Stage 1 source vs clean tables")
audit("rows", "stg_demo rows", "SELECT count(*) FROM s1.stg_demo")
audit("rows", "clean_case rows", "SELECT count(*) FROM clean_case")
audit("rows", "stg_drug rows", "SELECT count(*) FROM s1.stg_drug")
audit("rows", "clean_drug rows (one per case + drug_seq)", "SELECT count(*) FROM clean_drug")
audit("rows", "clean_drug source rows represented", "SELECT sum(n_source_rows) FROM clean_drug")
audit("rows", "clean_dose rows", "SELECT count(*) FROM clean_dose")
audit("rows", "stg_reac rows", "SELECT count(*) FROM s1.stg_reac")
audit("rows", "clean_reaction rows (one per case + term)", "SELECT count(*) FROM clean_reaction")
audit("rows", "stg_outc rows", "SELECT count(*) FROM s1.stg_outc")
audit("rows", "clean_outcome rows", "SELECT count(*) FROM clean_outcome")
audit("rows", "clean_onset rows (one per case with a drug)", "SELECT count(*) FROM clean_onset")
audit("rows", "clean_indication rows", "SELECT count(*) FROM clean_indication")
audit("rows", "clean_report_source rows", "SELECT count(*) FROM clean_report_source")

say("\n-- demographic and date rules")
for st in ["ok", "missing", "partial", "malformed", "invalid_before_1900", "invalid_after_latest_receipt"]:
    audit("event_date", f"status = {st}", f"SELECT count(*) FROM clean_case WHERE event_date_status = '{st}'")
audit("event_date", "informational: event after FIRST receipt (kept)", "SELECT count(*) FROM clean_case WHERE event_after_first_receipt",
      "normal in follow-up versions")
for st in ["ok", "missing", "non_numeric", "no_unit", "unknown_unit", "implausible"]:
    audit("age", f"status = {st}", f"SELECT count(*) FROM clean_case WHERE age_status = '{st}'")
for st in ["computed", "fda_age_grp", "none"]:
    audit("age", f"age_band source = {st}", f"SELECT count(*) FROM clean_case WHERE age_band_source = '{st}'")
audit("age", "FDA age_grp disagrees with computed band", "SELECT count(*) FROM clean_case WHERE age_grp_conflict")
audit("age", "age_grp present AND age valid (comparable)", "SELECT count(*) FROM clean_case WHERE age_grp_raw IS NOT NULL AND age_years IS NOT NULL")
for st in ["ok", "missing", "non_numeric", "no_unit", "unknown_unit", "implausible"]:
    audit("weight", f"status = {st}", f"SELECT count(*) FROM clean_case WHERE wt_status = '{st}'")
audit("sex", "raw value not F/M (-> Unknown)", "SELECT count(*) FROM clean_case WHERE sex_std = 'Unknown'")
for st in ["ok", "missing", "not_specified", "region_code", "non_standard_code"]:
    audit("country", f"occurrence country status = {st}", f"SELECT count(*) FROM clean_case WHERE occr_country_status = '{st}'")
audit("reporter", "reporter_type = Not reported", "SELECT count(*) FROM clean_case WHERE reporter_type = 'Not reported'")
audit("reporter", "reporter_type = Unrecognised code", "SELECT count(*) FROM clean_case WHERE reporter_type = 'Unrecognised code'", "must be 0")
audit("reporter", "report_channel = Unrecognised code", "SELECT count(*) FROM clean_case WHERE report_channel = 'Unrecognised code'", "must be 0")
audit("time", "first received in window", "SELECT count(*) FROM clean_case WHERE first_received_cohort = 'In window'")
audit("time", "first received before window", "SELECT count(*) FROM clean_case WHERE first_received_cohort = 'Before window'")
audit("time", "first_received fell back to fda_dt", "SELECT count(*) FROM clean_case WHERE first_received_source <> 'init_fda_dt'")
audit("time", "provisional (recent quarters of this extract)", "SELECT count(*) FROM clean_case WHERE is_provisional")

say("\n-- drug rules")
audit("drug", "drug_seq groups with differing identity (must be 0)", "SELECT count(*) FROM clean_drug WHERE identity_conflict",
      "if >0 the collapse rule needs review")
audit("drug", "rows with role DN (kept, flagged)", "SELECT count(*) FROM clean_drug WHERE role_cod = 'DN'")
audit("drug", "ingredient from drugname fallback", "SELECT count(*) FROM clean_drug WHERE ingredient_source = 'drugname_fallback'")
audit("drug", "ingredient missing entirely", "SELECT count(*) FROM clean_drug WHERE ingredient_source = 'missing'")
audit("drug", "combination products (backslash in ingredient)", "SELECT count(*) FROM clean_drug WHERE is_combination")
for st in ["PS", "fallback_first_administered", "none_all_drugs_not_administered", "none_no_drug_rows"]:
    audit("primary", f"ps_status = {st}", f"SELECT count(*) FROM clean_case WHERE ps_status = '{st}'")
audit("primary", "cases with more than one PS drug_seq", "SELECT count(*) FROM (SELECT primaryid FROM clean_drug WHERE role_cod = 'PS' GROUP BY 1 HAVING count(*) > 1)")

say("\n-- reaction and outcome rules")
audit("reaction", "reaction terms delivered in more than one casing", "SELECT count(*) FROM _pt_map WHERE n_forms > 1")
audit("reaction", "duplicate case+term rows collapsed", "SELECT sum(n_source_rows - 1) FROM clean_reaction")
audit("outcome", "cases with at least one outcome code", "SELECT count(*) FROM clean_case WHERE has_outcome_code")
audit("outcome", "cases with a death outcome (DE)", "SELECT count(*) FROM clean_case WHERE has_death_outcome")
audit("outcome", "cases with reaction term 'Death'", "SELECT count(DISTINCT caseid) FROM clean_reaction WHERE pt_key = 'death'", "different concept from DE")

say("\n-- onset rules")
for r in con.execute("SELECT onset_status, count(*) FROM clean_onset GROUP BY 1 ORDER BY 2 DESC").fetchall():
    audit("onset", f"status = {r[0]}", f"SELECT count(*) FROM clean_onset WHERE onset_status = '{r[0]}'")

say("\n-- duplicate candidates (flag only, nothing removed)")
for st in ["candidate", "checked_no_match", "not_checkable"]:
    audit("duplicates", f"status = {st}", f"SELECT count(*) FROM clean_case WHERE duplicate_check_status = '{st}'")
audit("duplicates", "cases that are the later member of a candidate group", "SELECT count(*) FROM clean_case WHERE is_later_duplicate")
audit("duplicates", "cases in the duplicate-reduced set", "SELECT count(*) FROM clean_case WHERE in_duplicate_reduced_set")

# code-value audit (does anything undocumented exist?)
run("CREATE TABLE audit_code_values (table_name VARCHAR, column_name VARCHAR, value VARCHAR, n BIGINT, documented BOOLEAN)")
code_specs = [
    ("demo", "rept_cod", "s1.stg_demo", "rept_cod", ["EXP", "PER", "DIR", "5DAY", "30DAY"]),
    ("demo", "occp_cod", "s1.stg_demo", "occp_cod", ["MD", "PH", "OT", "LW", "CN", "HP"]),
    ("demo", "sex", "s1.stg_demo", "sex", ["F", "M", "UNK"]),
    ("demo", "age_cod", "s1.stg_demo", "age_cod", ["DEC", "YR", "MON", "WK", "DY", "HR"]),
    ("demo", "age_grp", "s1.stg_demo", "age_grp", ["N", "I", "C", "T", "A", "E"]),
    ("demo", "wt_cod", "s1.stg_demo", "wt_cod", ["KG", "LBS", "GMS"]),
    ("demo", "e_sub", "s1.stg_demo", "e_sub", ["Y", "N"]),
    ("drug", "role_cod", "s1.stg_drug", "role_cod", ["PS", "SS", "C", "I", "DN"]),
    ("drug", "dechal", "s1.stg_drug", "dechal", ["Y", "N", "U", "D"]),
    ("drug", "rechal", "s1.stg_drug", "rechal", ["Y", "N", "U", "D"]),
    ("outc", "outc_cod", "s1.stg_outc", "outc_cod", ["DE", "LT", "HO", "DS", "CA", "RI", "OT"]),
    ("rpsr", "rpsr_cod", "s1.stg_rpsr", "rpsr_cod", ["FGN", "SDY", "LIT", "CSM", "HP", "UF", "CR", "DT", "OTH"]),
    ("ther", "dur_cod", "s1.stg_ther", "dur_cod", ["YR", "MON", "WK", "DAY", "HR", "MIN", "SEC"]),
]
for tname, cname, src, col, docs in code_specs:
    lst = ",".join(f"'{d}'" for d in docs)
    run(f"""INSERT INTO audit_code_values
            SELECT '{tname}', '{cname}', coalesce(upper(trim({col})), '<blank>'), count(*),
                   coalesce(upper(trim({col})), '<blank>') IN ({lst}) FROM {src} GROUP BY 1, 2, 3""")
show("code values NOT in the documented lists (ASC_NTS + FAQ 45 for HP); '<blank>' is expected for optional columns",
     "SELECT table_name, column_name, value, n FROM audit_code_values WHERE NOT documented AND value <> '<blank>' ORDER BY n DESC", 40)
show("occupation codes found in the data", "SELECT value, n FROM audit_code_values WHERE column_name = 'occp_cod' ORDER BY n DESC", 20)
show("role codes found in the data", "SELECT value, n FROM audit_code_values WHERE column_name = 'role_cod' ORDER BY n DESC", 20)

# ================================ INTEGRITY CHECKS ===========================
say("\n" + "=" * 90 + "\nINTEGRITY CHECKS\n" + "=" * 90)
results = []


def check(name, ok, detail=""):
    results.append(ok)
    say(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


n_case, n_case_dist = con.execute("SELECT count(*), count(DISTINCT caseid) FROM clean_case").fetchone()
n_s1 = scalar("SELECT count(*) FROM s1.stg_demo")
check("clean_case has exactly one row per case and the same count as stg_demo", n_case == n_case_dist == n_s1, f"{n_case:,} / {n_case_dist:,} / {n_s1:,}")
check("clean_drug accounts for every Stage 1 drug row", scalar("SELECT sum(n_source_rows) FROM clean_drug") == scalar("SELECT count(*) FROM s1.stg_drug"))
check("clean_drug identity never differs inside a repeated drug_seq", scalar("SELECT count(*) FROM clean_drug WHERE identity_conflict") == 0)
blank_pt = scalar("SELECT count(*) FROM s1.stg_reac WHERE nullif(trim(pt), '') IS NULL")
check("clean_reaction accounts for every non-blank reaction row",
      scalar("SELECT sum(n_source_rows) FROM clean_reaction") == scalar("SELECT count(*) FROM s1.stg_reac") - blank_pt, f"{blank_pt:,} blank-PT rows excluded")
for t in ["clean_drug", "clean_dose", "clean_reaction", "clean_outcome", "clean_onset", "clean_indication", "clean_report_source"]:
    orphan = scalar(f"SELECT count(*) FROM {t} WHERE primaryid NOT IN (SELECT primaryid FROM clean_case)")
    check(f"no orphan rows in {t}", orphan == 0, f"{orphan:,} found")
check("ages kept are within 0-120", scalar("SELECT count(*) FROM clean_case WHERE age_years < 0 OR age_years > 120") == 0)
check("weights kept are within 0.2-500 kg", scalar("SELECT count(*) FROM clean_case WHERE weight_kg < 0.2 OR weight_kg > 500") == 0)
check("kept event dates lie between 1900 and the latest receipt date",
      scalar("SELECT count(*) FROM clean_case WHERE event_date < DATE '1900-01-01' OR event_date > latest_received_date") == 0)
check("ps_status covers every case", scalar("SELECT count(*) FROM clean_case WHERE ps_status IS NULL") == 0)
check("no unrecognised occupation or report-type codes", scalar("SELECT count(*) FROM clean_case WHERE reporter_type = 'Unrecognised code' OR report_channel = 'Unrecognised code'") == 0,
      "if FAIL, see the 'NOT in the documented lists' table above; the data is still kept")
raw_cols = [("sex_raw", "sex"), ("age_raw", "age"), ("event_dt_raw", "event_dt"), ("occp_cod_raw", "occp_cod"),
            ("rept_cod_raw", "rept_cod"), ("occr_country_raw", "occr_country"), ("age_grp_raw", "age_grp")]
bad = 0
for ccol, scol in raw_cols:
    bad += scalar(f"""SELECT count(*) FROM clean_case c JOIN s1.stg_demo d ON d.primaryid = c.primaryid
                      WHERE c.{ccol} IS DISTINCT FROM nullif(trim(d.{scol}), '')""")
check("raw columns in clean_case equal the Stage 1 values (trimmed, blank = NULL)", bad == 0, f"{bad:,} differences")

# ================================ REVIEW FILES ===============================
step("review files for the decisions that are still open")
ING_CSV = (OUT_DIR / "review_ingredients.csv").as_posix()
con.execute(f"""
COPY (
  WITH c AS (SELECT ingredient_std, count(DISTINCT caseid) AS ps_cases, max(is_combination::INT) AS is_combination, max(n_ingredients) AS n_ingredients
             FROM clean_drug WHERE role_cod = 'PS' AND ingredient_std IS NOT NULL GROUP BY 1),
       r AS (SELECT *, sum(ps_cases) OVER (ORDER BY ps_cases DESC, ingredient_std) AS cum_cases, sum(ps_cases) OVER () AS tot,
                    row_number() OVER (ORDER BY ps_cases DESC, ingredient_std) AS rnk FROM c)
  SELECT rnk AS rank, ingredient_std, ps_cases, round(100.0 * cum_cases / tot, 2) AS cumulative_pct_of_ps_cases,
         is_combination, n_ingredients,
         regexp_matches(ingredient_std, '-[A-Z]{{4}}$') AS looks_like_biosimilar_suffix,
         regexp_matches(ingredient_std, '\\b(HYDROCHLORIDE|HCL|DIHYDROCHLORIDE|HYDROBROMIDE|SODIUM|DISODIUM|POTASSIUM|CALCIUM|MAGNESIUM|SULFATE|SULPHATE|PHOSPHATE|ACETATE|CITRATE|MALEATE|MESYLATE|TARTRATE|FUMARATE|SUCCINATE|BESYLATE|BROMIDE|CHLORIDE|NITRATE|LACTATE|GLUCONATE|STEARATE|PALMITATE|DIPROPIONATE|PROPIONATE|VALERATE|ANHYDROUS|MONOHYDRATE|DIHYDRATE|TRIHYDRATE|HEMIHYDRATE)\\b') AS has_salt_or_ester_word,
         CASE WHEN is_combination = 1 THEN NULL ELSE trim(regexp_replace(regexp_replace(ingredient_std, '-[A-Z]{{4}}$', ''),
              '\\s+(HYDROCHLORIDE|HCL|DIHYDROCHLORIDE|HYDROBROMIDE|SODIUM|DISODIUM|POTASSIUM|CALCIUM|MAGNESIUM|SULFATE|SULPHATE|PHOSPHATE|ACETATE|CITRATE|MALEATE|MESYLATE|TARTRATE|FUMARATE|SUCCINATE|BESYLATE|BROMIDE|CHLORIDE|NITRATE|LACTATE|GLUCONATE|STEARATE|PALMITATE|DIPROPIONATE|PROPIONATE|VALERATE|ANHYDROUS|MONOHYDRATE|DIHYDRATE|TRIHYDRATE|HEMIHYDRATE)\\b', '', 'g')) END AS proposed_parent_ingredient,
         '' AS approved_parent_ingredient
  FROM r WHERE rnk <= {REVIEW_TOP_N} ORDER BY rnk
) TO '{ING_CSV}' (HEADER, DELIMITER ',')""")
CTRY_CSV = (OUT_DIR / "review_country_codes.csv").as_posix()
con.execute(f"""
COPY (
  SELECT 'occurrence' AS source, coalesce(occr_country_raw, '<blank>') AS code, occr_country_status AS status, count(*) AS cases
  FROM clean_case GROUP BY 1, 2, 3
  UNION ALL
  SELECT 'reporter', coalesce(reporter_country_raw, '<blank>'), reporter_country_status, count(*) FROM clean_case GROUP BY 1, 2, 3
  ORDER BY 1, 4 DESC) TO '{CTRY_CSV}' (HEADER, DELIMITER ',')""")
CODE_CSV = (OUT_DIR / "review_code_values.csv").as_posix()
con.execute(f"COPY (SELECT * FROM audit_code_values ORDER BY table_name, column_name, n DESC) TO '{CODE_CSV}' (HEADER, DELIMITER ',')")
PT_CSV = (OUT_DIR / "review_reaction_casing.csv").as_posix()
con.execute(f"""
COPY (SELECT m.pt_key, m.pt_std AS chosen_form, m.n_forms FROM _pt_map m WHERE m.n_forms > 1 ORDER BY m.pt_key) TO '{PT_CSV}' (HEADER, DELIMITER ',')""")
say(f"written: {ING_CSV}\n         {CTRY_CSV}\n         {CODE_CSV}\n         {PT_CSV}")
con.execute("DROP TABLE IF EXISTS _pt_map")
for t in ["_c_demo", "_c_ps", "_c_drugcount", "_dup_base", "_dup_rows", "_dup_groups",
          "_dup_members", "_c_outc", "_c_reac_n", "_ther_agg"]:
    run(f"DROP TABLE IF EXISTS {t}")

# ================================ SUMMARY ====================================
show("clean tables", """SELECT table_name FROM information_schema.tables
     WHERE table_catalog = current_database() AND table_schema = 'main'
       AND table_name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY 1""")
con.execute("CHECKPOINT")
say("\n" + "=" * 90)
say(f"RESULT: {'ALL CHECKS PASSED' if all(results) else 'SOME CHECKS FAILED - read the FAIL lines before using this output'}")
say(f"Database size: {DB_PATH.stat().st_size / 1e6:,.0f} MB | total run time: {time.time() - _t0:,.0f} seconds")
say(f"Report: {REPORT_PATH}")
con.close()
_report.close()
