"""
FAERS STAGE 3 - add the signal tables used by Dashboard 2 (Signal Workbench).

Adds to faers_clean.duckdb (existing tables are not touched):
  signal_pairs        one row per primary-suspect drug x reaction pair (>= 3 cases) and window
  signal_meta         thresholds, window sizes, build time
  signal_admin_terms  reaction terms treated as administrative (edit the list below)

Design decisions:
  * Primary suspect only (ps_status = 'PS'); every case counted once per pair.
  * Comparator = all other cases with a primary-suspect ingredient.
  * Windows: 'All quarters' and 'Last 2 quarters' (= is_provisional cases).
    'Earlier quarters' (all other cases) is used only to decide whether a pair is NEW.
  * Every statistic is also computed after removing later duplicate candidates.
  * Defaults stored in flags: cases >= 3, PRR >= 2, chi-square (Yates) >= 4 (Evans et al.).
  * Administrative terms are flagged, not deleted.

Run: python build_stage3_signals.py "C:\\...\\faers_clean.duckdb"
CLOSE TABLEAU (or the data source) first: DuckDB allows only one writer.
"""
import sys
import time
from pathlib import Path

import duckdb

DB_PATH = Path(r"C:\path\to\faers_clean.duckdb")
MEMORY_LIMIT = "4GB"
MIN_CASES, PRR_MIN, CHI2_MIN = 3, 2.0, 4.0
if len(sys.argv) >= 2:
    DB_PATH = Path(sys.argv[1])
if not DB_PATH.exists():
    sys.exit(f"Database not found: {DB_PATH}")

# Starting list of non-clinical / product-use terms (lower case). Review and edit.
ADMIN_TERMS = [
    "off label use", "drug ineffective", "product use in unapproved indication", "product use issue",
    "product dose omission issue", "drug dose omission", "incorrect dose administered", "underdose",
    "wrong technique in product usage process", "intentional product misuse", "product storage error",
    "expired product administered", "therapeutic response decreased", "treatment failure",
    "inappropriate schedule of product administration", "incorrect route of product administration",
    "product administration error", "product quality issue", "product complaint", "no adverse event",
    "drug effect decreased", "drug interaction", "condition aggravated", "disease progression",
    "incorrect product administration duration", "product dose omission in error", "device malfunction",
    "wrong product administered", "accidental exposure to product", "drug exposure during pregnancy",
]

con = duckdb.connect(str(DB_PATH))
con.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
con.execute(f"SET temp_directory='{(DB_PATH.parent / 'duckdb_tmp').as_posix()}'")
con.execute("SET preserve_insertion_order=false")
t0 = time.time()
lines = []


def say(s=""):
    print(s)
    lines.append(s)


def scalar(sql):
    return con.execute(sql).fetchone()[0]


def show(title, sql, n=15):
    say(f"\n-- {title}")
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()[:n]
    body = [[("NULL" if v is None else f"{v:,.2f}" if isinstance(v, float) else f"{v:,}" if isinstance(v, int) else str(v)[:48]) for v in r] for r in rows]
    w = [max(len(c), *(len(r[i]) for r in body)) if body else len(c) for i, c in enumerate(cols)]
    say("   " + " | ".join(c.ljust(x) for c, x in zip(cols, w)))
    for r in body:
        say("   " + " | ".join(v.ljust(x) for v, x in zip(r, w)))


# ---- statistics as SQL (a = cases with drug AND reaction; dc = drug cases; rc = reaction cases; n = all cases)
def prr_sql(a, dc, rc, n):
    c, d = f"({rc}-{a})", f"({n}-{dc}-{rc}+{a})"
    return f"CASE WHEN {c} > 0 AND {dc} > 0 THEN ({a}::DOUBLE/{dc}) / ({c}::DOUBLE/({c}+{d})) END"


def chi_sql(a, dc, rc, n):
    b, c, d = f"({dc}-{a})", f"({rc}-{a})", f"({n}-{dc}-{rc}+{a})"
    return (f"CASE WHEN {dc} > 0 AND {rc} > 0 AND ({c}+{d}) > 0 AND ({b}+{d}) > 0 THEN "
            f"{n}::DOUBLE * pow(greatest(0, abs({a}::DOUBLE*{d} - {b}::DOUBLE*{c}) - {n}/2.0), 2) / "
            f"({dc}::DOUBLE*({c}+{d})*{rc}*({b}+{d})) END")


def ci_low_sql(a, dc, rc, n):
    c, d = f"({rc}-{a})", f"({n}-{dc}-{rc}+{a})"
    return (f"CASE WHEN {c} > 0 AND {dc} > 0 THEN exp(ln(({a}::DOUBLE/{dc}) / ({c}::DOUBLE/({c}+{d}))) - "
            f"1.96*sqrt(1.0/{a} - 1.0/{dc} + 1.0/{c} - 1.0/({c}+{d}))) END")


say("FAERS STAGE 3 - SIGNAL TABLES")
say(time.strftime("Run started: %Y-%m-%d %H:%M:%S"))

# ---- self-test of the formulas with the worked example (a=50, drug=1000, reaction=550, N=101000)
ex = con.execute(f"SELECT {prr_sql('50', '1000', '550', '101000')}, {chi_sql('50', '1000', '550', '101000')}").fetchone()
formula_ok = abs(ex[0] - 10.0) < 1e-9 and abs(ex[1] - 361.9) < 0.5
say(f"[{'PASS' if formula_ok else 'FAIL'}] worked example: PRR={ex[0]:.2f} (expect 10.00), chi-square={ex[1]:.1f} (expect about 361.9)")
if not formula_ok:
    con.close()
    sys.exit("Statistics self-test failed; database was not changed.")

# ---- counts
# Keep the previous published tables intact unless the complete build and checks succeed.
con.execute("BEGIN TRANSACTION")
BASE = "FROM clean_case c JOIN clean_reaction r ON r.primaryid = c.primaryid WHERE c.ps_status = 'PS' AND c.ps_ingredient_std IS NOT NULL"
con.execute("DROP TABLE IF EXISTS _pc")
con.execute(f"""
CREATE TABLE _pc AS
SELECT c.ps_ingredient_std AS drug, r.pt_key, any_value(r.pt) AS pt,
  count(*) AS a_all, count(*) FILTER (WHERE c.is_provisional) AS a_rec, count(*) FILTER (WHERE NOT c.is_provisional) AS a_ear,
  count(*) FILTER (WHERE c.in_duplicate_reduced_set) AS a_all_dr,
  count(*) FILTER (WHERE c.is_provisional AND c.in_duplicate_reduced_set) AS a_rec_dr,
  count(*) FILTER (WHERE c.has_outcome_code) AS ser_all, count(*) FILTER (WHERE c.has_outcome_code AND c.is_provisional) AS ser_rec,
  count(*) FILTER (WHERE c.has_death_outcome) AS dth_all, count(*) FILTER (WHERE c.has_death_outcome AND c.is_provisional) AS dth_rec,
  CASE WHEN bool_or(c.first_received_quarter = 'Before window') THEN 'Before window'
       ELSE arg_min(c.first_received_quarter, c.first_received_quarter_start) END AS first_seen
{BASE} GROUP BY c.ps_ingredient_std, r.pt_key""")
CASES = "FROM clean_case WHERE ps_status = 'PS' AND ps_ingredient_std IS NOT NULL"
con.execute(f"""CREATE OR REPLACE TABLE _dt AS SELECT ps_ingredient_std AS drug, count(*) AS d_all,
  count(*) FILTER (WHERE is_provisional) AS d_rec, count(*) FILTER (WHERE NOT is_provisional) AS d_ear,
  count(*) FILTER (WHERE in_duplicate_reduced_set) AS d_all_dr,
  count(*) FILTER (WHERE is_provisional AND in_duplicate_reduced_set) AS d_rec_dr {CASES} GROUP BY 1""")
con.execute("""CREATE OR REPLACE TABLE _rt AS SELECT pt_key, sum(a_all)::BIGINT AS r_all, sum(a_rec)::BIGINT AS r_rec,
  sum(a_ear)::BIGINT AS r_ear, sum(a_all_dr)::BIGINT AS r_all_dr, sum(a_rec_dr)::BIGINT AS r_rec_dr FROM _pc GROUP BY 1""")
con.execute(f"""CREATE OR REPLACE TABLE _nt AS SELECT count(*) AS n_all, count(*) FILTER (WHERE is_provisional) AS n_rec,
  count(*) FILTER (WHERE NOT is_provisional) AS n_ear, count(*) FILTER (WHERE in_duplicate_reduced_set) AS n_all_dr,
  count(*) FILTER (WHERE is_provisional AND in_duplicate_reduced_set) AS n_rec_dr {CASES}""")
con.execute("""CREATE OR REPLACE TABLE _x AS SELECT p.*, d.d_all, d.d_rec, d.d_ear, d.d_all_dr, d.d_rec_dr,
  r.r_all, r.r_rec, r.r_ear, r.r_all_dr, r.r_rec_dr, n.* FROM _pc p JOIN _dt d USING (drug) JOIN _rt r USING (pt_key) CROSS JOIN _nt n""")
say(f"pair counts built: {scalar('SELECT count(*) FROM _x'):,} drug-reaction pairs [{time.time() - t0:.0f}s]")


def window(tbl, label, k, sfx):
    """k = 'all' / 'rec' / 'ear'; sfx = '_dr' variant exists for all/rec only."""
    a, dc, rc, n = f"a_{k}", f"d_{k}", f"r_{k}", f"n_{k}"
    has_dr = k in ("all", "rec")
    adr, ddr, rdr, ndr = (f"a_{k}_dr", f"d_{k}_dr", f"r_{k}_dr", f"n_{k}_dr") if has_dr else ("NULL", "NULL", "NULL", "NULL")
    ser = f"ser_{k}" if k != "ear" else "NULL"
    dth = f"dth_{k}" if k != "ear" else "NULL"
    con.execute(f"""
    CREATE OR REPLACE TABLE {tbl} AS
    SELECT '{label}' AS "window", drug AS ps_ingredient_std, pt, pt_key, {a} AS cases, {dc} AS drug_cases, {rc} AS reaction_cases, {n} AS total_cases,
      {adr} AS cases_dr, {ddr} AS drug_cases_dr, {rdr} AS reaction_cases_dr, {ndr} AS total_cases_dr,
      {ser} AS serious_cases, {dth} AS death_cases, first_seen AS first_seen_quarter,
      {prr_sql(a, dc, rc, n)} AS prr, {chi_sql(a, dc, rc, n)} AS chi_square, {ci_low_sql(a, dc, rc, n)} AS prr_ci_low,
      {prr_sql(adr, ddr, rdr, ndr) if has_dr else 'NULL'} AS prr_dr, {chi_sql(adr, ddr, rdr, ndr) if has_dr else 'NULL'} AS chi_square_dr
    FROM _x WHERE {a} >= {MIN_CASES}""")
    con.execute(f"""ALTER TABLE {tbl} ADD COLUMN flagged_default BOOLEAN; UPDATE {tbl} SET flagged_default =
      coalesce(cases >= {MIN_CASES} AND prr >= {PRR_MIN} AND chi_square >= {CHI2_MIN}, FALSE)""")


window("_w_all", "All quarters", "all", "")
window("_w_rec", "Last 2 quarters", "rec", "")
window("_w_ear", "Earlier quarters", "ear", "")
last_two_quarters = con.execute("""
  SELECT string_agg(first_received_quarter, ', ' ORDER BY quarter_start)
  FROM (
    SELECT first_received_quarter, min(first_received_quarter_start) AS quarter_start
    FROM clean_case WHERE first_received_quarter_start IS NOT NULL
    GROUP BY first_received_quarter ORDER BY quarter_start DESC LIMIT 2
  )
""").fetchone()[0]
con.execute("CREATE OR REPLACE TABLE signal_admin_terms (pt_key VARCHAR)")
con.executemany("INSERT INTO signal_admin_terms VALUES (?)", [(t,) for t in ADMIN_TERMS])
con.execute("""CREATE OR REPLACE TABLE _emerge AS
  SELECT r.ps_ingredient_std, r.pt_key, (r.flagged_default AND NOT coalesce(e.flagged_default, FALSE)) AS is_emerging
  FROM _w_rec r LEFT JOIN _w_ear e ON e.ps_ingredient_std = r.ps_ingredient_std AND e.pt_key = r.pt_key""")
con.execute("DROP TABLE IF EXISTS signal_pairs")
con.execute(f"""
CREATE TABLE signal_pairs AS
SELECT length(w.ps_ingredient_std)::VARCHAR || ':' || w.ps_ingredient_std ||
       length(w.pt_key)::VARCHAR || ':' || w.pt_key AS pair_id,
  w."window", w.ps_ingredient_std, w.pt, w.pt_key,
  w.cases, w.drug_cases, w.reaction_cases, w.total_cases, w.cases_dr, w.drug_cases_dr, w.reaction_cases_dr, w.total_cases_dr,
  w.serious_cases, w.death_cases, w.first_seen_quarter, w.prr, w.chi_square, w.prr_ci_low, w.prr_dr, w.chi_square_dr,
  w.flagged_default,
  coalesce(w.cases_dr >= {MIN_CASES} AND w.prr_dr >= {PRR_MIN} AND w.chi_square_dr >= {CHI2_MIN}, FALSE) AS flagged_dr_default,
  (w."window" = 'Last 2 quarters' AND coalesce(e.is_emerging, FALSE)) AS is_emerging,
  (w.pt_key IN (SELECT pt_key FROM signal_admin_terms)) AS is_administrative
FROM (SELECT * FROM _w_all UNION ALL SELECT * FROM _w_rec) w
LEFT JOIN _emerge e ON e.ps_ingredient_std = w.ps_ingredient_std AND e.pt_key = w.pt_key""")
con.execute("ALTER TABLE signal_pairs ADD COLUMN robust_to_duplicate_reduction BOOLEAN")
con.execute("UPDATE signal_pairs SET robust_to_duplicate_reduction = (flagged_default AND flagged_dr_default)")
con.execute("CREATE OR REPLACE TABLE signal_meta (key VARCHAR, value VARCHAR)")
con.executemany("INSERT INTO signal_meta VALUES (?, ?)", [
    ("built_at", time.strftime("%Y-%m-%d %H:%M:%S")), ("min_cases", str(MIN_CASES)), ("prr_min", str(PRR_MIN)),
    ("chi_square_min", str(CHI2_MIN)), ("chi_square_type", "Yates-corrected"), ("counting", "primary suspect only, distinct cases"),
    ("comparator", "all other cases with a primary-suspect ingredient"),
    ("last2_quarters", last_two_quarters),
    ("cases_all", str(scalar("SELECT n_all FROM _nt"))), ("cases_last2", str(scalar("SELECT n_rec FROM _nt")))])
for t in ["_pc", "_dt", "_rt", "_nt", "_x", "_w_all", "_w_rec", "_w_ear", "_emerge"]:
    con.execute(f"DROP TABLE IF EXISTS {t}")

# ---- proof
say("\n" + "=" * 80 + "\nCHECKS\n" + "=" * 80)
show("rows per window", 'SELECT "window", count(*) AS pairs, count(*) FILTER (WHERE flagged_default) AS flagged, '
     'count(*) FILTER (WHERE robust_to_duplicate_reduction) AS robust, count(*) FILTER (WHERE is_emerging) AS emerging, '
     'count(*) FILTER (WHERE is_administrative) AS administrative FROM signal_pairs GROUP BY 1 ORDER BY 1')
ok = True
# independent recount of the pair with the most cases, straight from the clean tables
top = con.execute("""SELECT ps_ingredient_std, pt_key, cases, drug_cases, reaction_cases, total_cases, prr, chi_square FROM signal_pairs
                     WHERE "window" = 'All quarters' ORDER BY cases DESC LIMIT 1""").fetchone()
a = scalar(f"SELECT count(*) {BASE} AND c.ps_ingredient_std = '{top[0].replace(chr(39), chr(39) * 2)}' AND r.pt_key = '{top[1].replace(chr(39), chr(39) * 2)}'")
dc = scalar(f"SELECT count(*) {CASES} AND ps_ingredient_std = '{top[0].replace(chr(39), chr(39) * 2)}'")
n = scalar(f"SELECT count(*) {CASES}")
rc = scalar(f"SELECT count(*) {BASE} AND r.pt_key = '{top[1].replace(chr(39), chr(39) * 2)}'")
ok &= (a, dc, rc, n) == top[2:6]
say(f"[{'PASS' if (a, dc, rc, n) == top[2:6] else 'FAIL'}] independent recount of the biggest pair {top[0]} | {top[1]}: a={a:,} drug={dc:,} reaction={rc:,} N={n:,}")
bad = scalar('SELECT count(*) FROM signal_pairs WHERE cases > drug_cases OR cases > reaction_cases OR drug_cases + reaction_cases - cases > total_cases')
ok &= bad == 0
say(f"[{'PASS' if bad == 0 else 'FAIL'}] no pair has impossible counts ({bad:,} found)")
dup = scalar('SELECT count(*) FROM (SELECT "window", pair_id FROM signal_pairs GROUP BY 1, 2 HAVING count(*) > 1)')
ok &= dup == 0
say(f"[{'PASS' if dup == 0 else 'FAIL'}] no duplicate display IDs per window ({dup:,} duplicates)")
dup_keys = scalar('SELECT count(*) FROM (SELECT "window", ps_ingredient_std, pt_key FROM signal_pairs GROUP BY 1, 2, 3 HAVING count(*) > 1)')
ok &= dup_keys == 0
say(f"[{'PASS' if dup_keys == 0 else 'FAIL'}] one row per drug/reaction key and window ({dup_keys:,} duplicates)")

# Re-evaluate the published arithmetic and flags from the four cell counts.
bad_stats = scalar(f"""
WITH cells AS (
  SELECT *, drug_cases - cases AS b, reaction_cases - cases AS c,
         total_cases - drug_cases - reaction_cases + cases AS d,
         drug_cases_dr - cases_dr AS b_dr, reaction_cases_dr - cases_dr AS c_dr,
         total_cases_dr - drug_cases_dr - reaction_cases_dr + cases_dr AS d_dr
  FROM signal_pairs
), expected AS (
  SELECT *,
         CASE WHEN c > 0 AND drug_cases > 0
              THEN (cases::DOUBLE / drug_cases) /
                   (c::DOUBLE / (c + d))
         END AS expected_prr,
         CASE WHEN c > 0 AND drug_cases > 0
              THEN exp(ln((cases::DOUBLE / drug_cases) / (c::DOUBLE / (c + d)))
                   - 1.96 * sqrt(1.0 / cases - 1.0 / drug_cases + 1.0 / c - 1.0 / (c + d)))
         END AS expected_prr_ci_low,
         CASE WHEN drug_cases::DOUBLE * (c + d) * reaction_cases::DOUBLE * (b + d) > 0
              THEN total_cases::DOUBLE * pow(
                   greatest(0, abs(cases::DOUBLE * d - b::DOUBLE * c) - total_cases / 2.0), 2)
                   / (drug_cases::DOUBLE * (c + d) * reaction_cases * (b + d))
         END AS expected_chi_square,
         CASE WHEN c_dr > 0 AND drug_cases_dr > 0
              THEN (cases_dr::DOUBLE / drug_cases_dr) /
                   (c_dr::DOUBLE / (c_dr + d_dr))
         END AS expected_prr_dr,
         CASE WHEN drug_cases_dr::DOUBLE * (c_dr + d_dr) * reaction_cases_dr::DOUBLE * (b_dr + d_dr) > 0
              THEN total_cases_dr::DOUBLE * pow(
                   greatest(0, abs(cases_dr::DOUBLE * d_dr - b_dr::DOUBLE * c_dr)
                   - total_cases_dr / 2.0), 2)
                   / (drug_cases_dr::DOUBLE * (c_dr + d_dr) * reaction_cases_dr * (b_dr + d_dr))
         END AS expected_chi_square_dr
  FROM cells
)
SELECT count(*) FROM expected
WHERE b < 0 OR c < 0 OR d < 0 OR b_dr < 0 OR c_dr < 0 OR d_dr < 0
   OR (expected_prr IS NULL AND prr IS NOT NULL)
   OR (expected_prr IS NOT NULL AND (prr IS NULL OR abs(prr - expected_prr) > 1e-10 * greatest(1, abs(expected_prr))))
   OR (expected_prr_ci_low IS NULL AND prr_ci_low IS NOT NULL)
   OR (expected_prr_ci_low IS NOT NULL AND (prr_ci_low IS NULL OR abs(prr_ci_low - expected_prr_ci_low) > 1e-10 * greatest(1, abs(expected_prr_ci_low))))
   OR (expected_chi_square IS NULL AND chi_square IS NOT NULL)
   OR (expected_chi_square IS NOT NULL AND (chi_square IS NULL OR abs(chi_square - expected_chi_square) > 1e-10 * greatest(1, abs(expected_chi_square))))
   OR (expected_prr_dr IS NULL AND prr_dr IS NOT NULL)
   OR (expected_prr_dr IS NOT NULL AND (prr_dr IS NULL OR abs(prr_dr - expected_prr_dr) > 1e-10 * greatest(1, abs(expected_prr_dr))))
   OR (expected_chi_square_dr IS NULL AND chi_square_dr IS NOT NULL)
   OR (expected_chi_square_dr IS NOT NULL AND (chi_square_dr IS NULL OR abs(chi_square_dr - expected_chi_square_dr) > 1e-10 * greatest(1, abs(expected_chi_square_dr))))
   OR flagged_default IS DISTINCT FROM coalesce(cases >= {MIN_CASES} AND expected_prr >= {PRR_MIN}
                                                 AND expected_chi_square >= {CHI2_MIN}, FALSE)
   OR flagged_dr_default IS DISTINCT FROM coalesce(cases_dr >= {MIN_CASES} AND expected_prr_dr >= {PRR_MIN}
                                                    AND expected_chi_square_dr >= {CHI2_MIN}, FALSE)
   OR robust_to_duplicate_reduction IS DISTINCT FROM (flagged_default AND flagged_dr_default)
   OR is_administrative IS DISTINCT FROM (pt_key IN (SELECT pt_key FROM signal_admin_terms))
""")
ok &= bad_stats == 0
say(f"[{'PASS' if bad_stats == 0 else 'FAIL'}] published PRR, Yates chi-square, and flags match independent cell-count formulas ({bad_stats:,} mismatches)")
bad_rows = scalar(f"""SELECT count(*) FROM signal_pairs
  WHERE cases < {MIN_CASES} OR cases > drug_cases OR cases > reaction_cases
     OR drug_cases + reaction_cases - cases > total_cases
     OR cases_dr > cases OR drug_cases_dr > drug_cases OR reaction_cases_dr > reaction_cases
     OR total_cases_dr > total_cases OR serious_cases > cases OR death_cases > serious_cases""")
ok &= bad_rows == 0
say(f"[{'PASS' if bad_rows == 0 else 'FAIL'}] minimum-case, subset, duplicate-reduction, and outcome count constraints ({bad_rows:,} violations)")
bad_windows = scalar("""SELECT count(*) FROM (
  SELECT "window" FROM signal_pairs GROUP BY 1
  HAVING "window" NOT IN ('All quarters', 'Last 2 quarters')
)""")
window_count = scalar('SELECT count(DISTINCT "window") FROM signal_pairs')
bad_windows += int(window_count != 2)
ok &= bad_windows == 0
say(f"[{'PASS' if bad_windows == 0 else 'FAIL'}] exactly the two requested windows ({window_count} found, {bad_windows:,} issues)")
bad_recent = scalar("""WITH recent AS (
  SELECT first_received_quarter_start
  FROM clean_case WHERE first_received_quarter_start IS NOT NULL
  GROUP BY first_received_quarter_start ORDER BY first_received_quarter_start DESC LIMIT 2
)
SELECT count(*) FROM clean_case c
WHERE c.is_provisional IS DISTINCT FROM
      coalesce(c.first_received_quarter_start IN (SELECT first_received_quarter_start FROM recent), FALSE)""")
ok &= bad_recent == 0
say(f"[{'PASS' if bad_recent == 0 else 'FAIL'}] provisional cases match the latest two received quarters ({last_two_quarters}; {bad_recent:,} mismatches)")
bad_totals = scalar("""SELECT count(*) FROM (
  SELECT "window" FROM signal_pairs GROUP BY 1 HAVING count(DISTINCT total_cases) <> 1
)""")
ok &= bad_totals == 0
say(f"[{'PASS' if bad_totals == 0 else 'FAIL'}] each window has one consistent total-case denominator ({bad_totals:,} windows with inconsistent totals)")
bad_first_seen = scalar("""WITH expected AS (
  SELECT c.ps_ingredient_std, r.pt_key,
         CASE WHEN bool_or(c.first_received_quarter = 'Before window') THEN 'Before window'
              ELSE arg_min(c.first_received_quarter, c.first_received_quarter_start) END AS first_seen_quarter
  FROM clean_case c JOIN clean_reaction r ON r.primaryid = c.primaryid
  WHERE c.ps_status = 'PS' AND c.ps_ingredient_std IS NOT NULL
  GROUP BY 1, 2
)
SELECT count(*) FROM signal_pairs s
JOIN expected e ON e.ps_ingredient_std = s.ps_ingredient_std AND e.pt_key = s.pt_key
WHERE s.first_seen_quarter IS DISTINCT FROM e.first_seen_quarter""")
ok &= bad_first_seen == 0
say(f"[{'PASS' if bad_first_seen == 0 else 'FAIL'}] first-seen quarter uses chronological dates and preserves pre-window cases ({bad_first_seen:,} mismatches)")
bad_emerging = scalar("""SELECT count(*) FROM signal_pairs
  WHERE is_emerging AND ("window" <> 'Last 2 quarters' OR NOT flagged_default)""")
ok &= bad_emerging == 0
say(f"[{'PASS' if bad_emerging == 0 else 'FAIL'}] emerging flags are limited to currently flagged recent-window pairs ({bad_emerging:,} violations)")
show("top 12 flagged non-administrative pairs by cases (sanity check)",
     'SELECT ps_ingredient_std, pt, cases, round(prr, 1) AS prr, round(chi_square, 0) AS chi2, robust_to_duplicate_reduction AS robust '
     'FROM signal_pairs WHERE "window" = \'All quarters\' AND flagged_default AND NOT is_administrative ORDER BY cases DESC', 12)
show("top 12 emerging pairs by cases (Last 2 quarters window)",
     'SELECT ps_ingredient_std, pt, cases, round(prr, 1) AS prr, round(chi_square, 0) AS chi2 FROM signal_pairs '
     'WHERE "window" = \'Last 2 quarters\' AND is_emerging AND NOT is_administrative ORDER BY cases DESC', 12)
if ok:
    con.execute("COMMIT")
    con.execute("CHECKPOINT")
else:
    con.execute("ROLLBACK")
say(f"\nRESULT: {'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'} | run time {time.time() - t0:.0f}s | database {DB_PATH.stat().st_size / 1e6:,.0f} MB")
(DB_PATH.parent / "stage3_report.txt").write_text("\n".join(lines), encoding="utf-8")
con.close()
if not ok:
    sys.exit("Validation failed; the previous published signal tables were preserved.")
