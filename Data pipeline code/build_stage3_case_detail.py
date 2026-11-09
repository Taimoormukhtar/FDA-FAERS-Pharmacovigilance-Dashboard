"""Build signal_case_detail for Dashboard 2 selected-pair case panels."""
import sys
import time
from pathlib import Path

import duckdb


DB_PATH = Path(
    r"D:\FDA FAERS Pharmacovigilance Dashboard\outputs"
    r"\stage2_clean_20261029_verified\faers_clean.duckdb"
)
if len(sys.argv) >= 2:
    DB_PATH = Path(sys.argv[1])
if not DB_PATH.is_file():
    sys.exit(f"Database not found: {DB_PATH}")

con = duckdb.connect(str(DB_PATH))
con.execute("SET memory_limit='4GB'")
con.execute(f"SET temp_directory='{(DB_PATH.parent / 'duckdb_tmp').as_posix()}'")
con.execute("SET preserve_insertion_order=false")
t0 = time.time()
report = []
transaction_open = False


def scalar(sql):
    return con.execute(sql).fetchone()[0]


def say(message=""):
    print(message)
    report.append(message)


required_tables = {"clean_case", "clean_reaction", "clean_onset", "clean_drug", "signal_pairs"}
available_tables = {
    row[0]
    for row in con.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'main' AND table_type = 'BASE TABLE'
    """).fetchall()
}
missing_tables = required_tables - available_tables
if missing_tables:
    con.close()
    sys.exit(f"Required source tables are missing: {', '.join(sorted(missing_tables))}")

say("FAERS STAGE 3B - SIGNAL CASE DETAIL")
say(time.strftime("Run started: %Y-%m-%d %H:%M:%S"))

con.execute("BEGIN TRANSACTION")
transaction_open = True
try:
    con.execute("""
    CREATE OR REPLACE TABLE signal_case_detail AS
    WITH eligible_pairs AS (
      SELECT DISTINCT ps_ingredient_std, pt_key
      FROM signal_pairs
      WHERE "window" = 'All quarters'
    )
    SELECT
      length(c.ps_ingredient_std)::VARCHAR || ':' || c.ps_ingredient_std ||
        length(r.pt_key)::VARCHAR || ':' || r.pt_key AS pair_id,
      c.primaryid,
      c.caseid,
      c.ps_ingredient_std,
      r.pt,
      r.pt_key,
      c.first_received_date,
      c.first_received_quarter,
      c.is_provisional,
      c.in_duplicate_reduced_set,
      c.duplicate_check_status,
      coalesce(c.worst_outcome, 'No serious outcome coded') AS worst_outcome,
      coalesce(c.age_band, 'Unknown') AS age_band,
      coalesce(c.age_band_order, 99) AS age_band_order,
      c.age_years,
      c.sex_std,
      c.channel_group,
      coalesce(o.onset_status, 'no_onset_row') AS onset_status,
      o.onset_bucket,
      o.onset_days,
      d.route_std,
      coalesce(d.dechallenge, 'Not recorded') AS dechallenge,
      coalesce(d.rechallenge, 'Not recorded') AS rechallenge,
      d.rechallenge_positive
    FROM clean_case c
    JOIN clean_reaction r ON r.primaryid = c.primaryid
    JOIN eligible_pairs p
      ON p.ps_ingredient_std = c.ps_ingredient_std AND p.pt_key = r.pt_key
    LEFT JOIN clean_onset o ON o.primaryid = c.primaryid
    LEFT JOIN clean_drug d
      ON d.primaryid = c.primaryid
     AND d.drug_seq_raw = c.ps_drug_seq_raw
     AND d.role_cod = 'PS'
    WHERE c.ps_status = 'PS'
      AND c.ps_ingredient_std IS NOT NULL
    """)

    n_rows = scalar("SELECT count(*) FROM signal_case_detail")
    expected_all = scalar("""
      SELECT coalesce(sum(cases), 0)
      FROM signal_pairs WHERE "window" = 'All quarters'
    """)
    duplicate_case_pairs = scalar("""
      SELECT count(*) FROM (
        SELECT primaryid, pair_id
        FROM signal_case_detail
        GROUP BY 1, 2
        HAVING count(*) > 1
      )
    """)
    missing_keys = scalar("""
      SELECT count(*) FROM signal_case_detail
      WHERE primaryid IS NULL OR pair_id IS NULL
    """)
    bad_all = scalar("""
      WITH detail AS (
        SELECT ps_ingredient_std, pt_key,
               count(*) AS n,
               count(*) FILTER (WHERE in_duplicate_reduced_set) AS n_dr
        FROM signal_case_detail
        GROUP BY 1, 2
      )
      SELECT count(*)
      FROM detail d
      FULL OUTER JOIN (
        SELECT ps_ingredient_std, pt_key, cases, cases_dr
        FROM signal_pairs WHERE "window" = 'All quarters'
      ) s USING (ps_ingredient_std, pt_key)
      WHERE d.n IS DISTINCT FROM s.cases
         OR d.n_dr IS DISTINCT FROM s.cases_dr
    """)
    bad_recent = scalar("""
      WITH detail AS (
        SELECT ps_ingredient_std, pt_key,
               count(*) AS n,
               count(*) FILTER (WHERE in_duplicate_reduced_set) AS n_dr
        FROM signal_case_detail
        WHERE is_provisional
        GROUP BY 1, 2
        HAVING count(*) >= 3
      )
      SELECT count(*)
      FROM detail d
      FULL OUTER JOIN (
        SELECT ps_ingredient_std, pt_key, cases, cases_dr
        FROM signal_pairs WHERE "window" = 'Last 2 quarters'
      ) s USING (ps_ingredient_std, pt_key)
      WHERE d.n IS DISTINCT FROM s.cases
         OR d.n_dr IS DISTINCT FROM s.cases_dr
    """)
    bad_pair_ids = scalar("""
      SELECT count(*)
      FROM signal_case_detail
      WHERE pair_id IS DISTINCT FROM
        length(ps_ingredient_std)::VARCHAR || ':' || ps_ingredient_std ||
        length(pt_key)::VARCHAR || ':' || pt_key
    """)

    checks = [
        ("rows equal All-quarters pair-case counts", n_rows == expected_all),
        ("one row per case/reaction pair", duplicate_case_pairs == 0),
        ("case and pair keys are populated", missing_keys == 0),
        ("All-quarters counts and duplicate-reduced counts match", bad_all == 0),
        ("Last-2-quarter counts and duplicate-reduced counts match", bad_recent == 0),
        ("pair_id matches signal_pairs key encoding", bad_pair_ids == 0),
    ]
    say(f"Rows built: {n_rows:,}; expected from All quarters: {expected_all:,}")
    say(f"Duplicate primaryid/pair_id rows: {duplicate_case_pairs:,}")
    say(f"Rows with missing primaryid or pair_id: {missing_keys:,}")
    say(f"All-quarters pair-count mismatches: {bad_all:,}")
    say(f"Last-2-quarter pair-count mismatches: {bad_recent:,}")
    say(f"pair_id encoding mismatches: {bad_pair_ids:,}")

    for column in [
        "first_received_quarter",
        "worst_outcome",
        "age_band",
        "sex_std",
        "channel_group",
        "onset_status",
        "onset_bucket",
        "route_std",
        "dechallenge",
        "rechallenge",
        "duplicate_check_status",
    ]:
        rows = con.execute(f"""
          SELECT {column}, count(*) AS n
          FROM signal_case_detail
          GROUP BY 1
          ORDER BY n DESC
          LIMIT 15
        """).fetchall()
        say(f"\n-- {column} value counts (top 15)")
        for value, count in rows:
            say(f"{'NULL' if value is None else value}: {count:,}")

    if all(passed for _, passed in checks):
        con.execute("COMMIT")
        transaction_open = False
    else:
        con.execute("ROLLBACK")
        transaction_open = False
        raise RuntimeError("Validation failed; previous signal_case_detail was preserved.")
except Exception:
    if transaction_open:
        con.execute("ROLLBACK")
    con.close()
    raise

con.execute("CHECKPOINT")
say(f"\nRESULT: ALL CHECKS PASSED | run time {time.time() - t0:.0f}s | database {DB_PATH}")
con.close()
(DB_PATH.parent / "stage3_case_detail_report.txt").write_text(
    "\n".join(report) + "\n", encoding="utf-8"
)
