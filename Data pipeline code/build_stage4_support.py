"""
FAERS STAGE 4 - support tables for Dashboards 3, 4 and 5.  CLOSE TABLEAU FIRST (DuckDB allows one writer).

Adds three tables to faers_clean.duckdb. Existing tables are not touched; everything is one
transaction, so a failed check rolls back and leaves the database exactly as it was.

  profile_reaction     clean_reaction + the drug-level PRR for that reaction + two flags.
                       Why: lets Tableau show a PRR dot next to each reaction bar with no data blend.
  ref_indication_area  indication term -> therapeutic area (keyword rules, approximate; no licensed MedDRA).
  case_area            ONE therapeutic area per case (from the primary-suspect drug's indications).
                       Why: a case can have several indications; one area per case avoids double counting
                       in the therapeutic-area filter (Dashboards 1, 4) and the area x age heatmap.

Dashboard 5 needs no new table (clean_case + clean_onset are enough).

Run: python build_stage4_support.py "C:\\...\\faers_clean.duckdb"
Output: stage4_report.txt and review_indication_area.csv next to the database.
"""
import sys
import time
from pathlib import Path

import duckdb

DB = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\path\to\faers_clean.duckdb")
if not DB.exists():
    sys.exit(f"Database not found: {DB}")

# Ordered keyword rules: the FIRST matching rule wins (lower-case regular expressions).
# Preliminary and approximate: review the printed 'Other / unclassified' list and extend as needed.
RULES = [
    ("Unknown indication", r"unknown|unspecified|not specified"),
    ("Oncology", r"cancer|carcinoma|leuk|lymphom|myeloma|neoplas|tumou?r|melanoma|sarcoma|metasta|glioma|malignan|blastoma"),
    ("Haematology", r"anaemia|anemia|haemophil|hemophil|neutropen|thrombocytop|sickle|coagulat|iron deficiency|haemoglobin|hemoglobin"),
    ("Infectious disease & vaccines", r"infect|hiv|hepatitis [abc]|covid|sars|tubercul|pneumonia|sepsis|vaccin|immunis|immuniz|herpes|influenza|bacter|fung|viral|malaria"),
    ("Rheumatology & musculoskeletal", r"arthrit|rheumat|spondyl|lupus|gout|osteo|fibromyalg|musculo|bone|scleroderma|systemic sclerosis"),
    ("Dermatology", r"atopic|eczema|psoria|dermat|urticaria|prurigo|hidradenitis|alopecia|vitiligo|acne|rosacea|pemphig|skin"),
    ("Gastroenterology & hepatology", r"crohn|colitis|gastr|reflux|heartburn|dyspepsia|constipation|bowel|ulcer|hepat|liver|nausea|vomit|oesophag|esophag"),
    ("Cardiovascular", r"pulmonary arterial|pulmonary hypertension|hypertens|heart|cardi|atrial|myocardial|thromb|angina|coronary|embol|stroke|anticoag|vascular"),
    ("Respiratory", r"asthma|chronic obstructive|copd|rhinitis|sinusitis|nasal polyp|bronch|cystic fibrosis|pulmonary|cough|emphysema"),
    ("Ophthalmology", r"macular|retina|glaucoma|dry eye|uveitis|ocular|cataract|visual"),
    ("Endocrine & metabolic", r"diabet|obes|weight|glyc|thyroid|lipid|cholesterol|acromegal|growth hormone|metabolic|hormone"),
    ("Neurology", r"epilep|seizure|migraine|multiple sclerosis|parkinson|alzheimer|neuropath|headache|dementia|spastic|neuro"),
    ("Psychiatry & sleep", r"depress|schizophren|bipolar|anxiety|adhd|attention deficit|insomnia|psych|sleep"),
    ("Women's health", r"contracept|pregnan|menopaus|endometrio|infertil|ovulat|birth control|menstrua"),
    ("Renal & urology", r"kidney|renal|urinary|prostat|bladder"),
    ("Pain & anaesthesia", r"pain|analges|anaesth|anesth|sedation"),
    ("Immunology, transplant & allergy", r"transplant|graft|rejection|immunosuppress|allerg|anaphyla|angioedema|immunodeficien"),
]

con = duckdb.connect(str(DB))
con.execute("SET memory_limit='4GB'")
con.execute("SET preserve_insertion_order=false")
con.execute(f"SET temp_directory='{(DB.parent / 'duckdb_tmp').as_posix()}'")
t0 = time.time()
lines = []
transaction_open = False


def say(s=""):
    print(s)
    lines.append(s)


def scalar(sql):
    return con.execute(sql).fetchone()[0]


def show(title, sql, n=40):
    say(f"\n-- {title}")
    rows = con.execute(sql).fetchall()[:n]
    for r in rows:
        say("   " + " | ".join(f"{v:,}" if isinstance(v, int) else str(v) for v in r))


# ---- required inputs
have = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
need = {"clean_case", "clean_reaction", "clean_indication", "signal_pairs", "signal_admin_terms"}
if need - have:
    sys.exit(f"Missing tables: {sorted(need - have)}. Run Stages 1-3 first.")

say("FAERS STAGE 4 - SUPPORT TABLES")
say(time.strftime("Run started: %Y-%m-%d %H:%M:%S"))
con.execute("BEGIN TRANSACTION")
transaction_open = True
try:
    # ---------------- profile_reaction
    say("Building profile_reaction...")
    con.execute("DROP TABLE IF EXISTS profile_reaction")
    con.execute("""
    CREATE TABLE profile_reaction AS
    SELECT r.primaryid, r.pt, r.pt_key,
           s.prr AS prr_all,
           coalesce(s.flagged_default, FALSE) AS prr_flagged,
           (r.pt_key IN (SELECT pt_key FROM signal_admin_terms)) AS is_administrative
    FROM clean_reaction r
    JOIN clean_case c ON c.primaryid = r.primaryid
    LEFT JOIN signal_pairs s ON c.ps_status = 'PS'
         AND s."window" = 'All quarters'
         AND s.ps_ingredient_std = c.ps_ingredient_std AND s.pt_key = r.pt_key""")
    say("Built profile_reaction.")

    # ---------------- ref_indication_area
    say("Building ref_indication_area...")
    con.execute("CREATE OR REPLACE TEMP TABLE _area_rules (rule_order INTEGER, area VARCHAR, pattern VARCHAR)")
    con.executemany("INSERT INTO _area_rules VALUES (?, ?, ?)", [(i, a, p) for i, (a, p) in enumerate(RULES, start=1)])
    con.execute("DROP TABLE IF EXISTS ref_indication_area")
    con.execute("""
    CREATE TABLE ref_indication_area AS
    WITH d AS (SELECT DISTINCT indication_pt, lower(indication_pt) AS k FROM clean_indication WHERE indication_pt IS NOT NULL),
         m AS (SELECT d.indication_pt, min(r.rule_order) AS ro FROM d JOIN _area_rules r ON regexp_matches(d.k, r.pattern) GROUP BY d.indication_pt)
    SELECT d.indication_pt, coalesce(r.area, 'Other / unclassified') AS therapeutic_area
    FROM d LEFT JOIN m ON m.indication_pt = d.indication_pt LEFT JOIN _area_rules r ON r.rule_order = m.ro""")
    say("Built ref_indication_area.")

    # ---------------- case_area (primary-suspect drug's indications; real area > other > unknown)
    say("Building case_area...")
    con.execute("DROP TABLE IF EXISTS case_area")
    con.execute("""
    CREATE TABLE case_area AS
    WITH ind AS (
      SELECT c.primaryid, a.therapeutic_area, count(*) AS n,
             CASE WHEN a.therapeutic_area = 'Unknown indication' THEN 2
                  WHEN a.therapeutic_area = 'Other / unclassified' THEN 1 ELSE 0 END AS prio
      FROM clean_case c
      JOIN clean_indication i ON i.primaryid = c.primaryid AND i.drug_seq_raw = c.ps_drug_seq_raw
      JOIN ref_indication_area a ON a.indication_pt = i.indication_pt
      GROUP BY c.primaryid, a.therapeutic_area),
    ranked AS (
      SELECT primaryid, therapeutic_area,
             row_number() OVER (PARTITION BY primaryid ORDER BY prio, n DESC, therapeutic_area) AS rn,
             count(*) FILTER (WHERE prio = 0) OVER (PARTITION BY primaryid) AS n_real_areas
      FROM ind)
    SELECT c.primaryid,
           coalesce(r.therapeutic_area, 'No indication recorded') AS therapeutic_area,
           coalesce(r.n_real_areas, 0) AS area_count
    FROM clean_case c LEFT JOIN (SELECT * FROM ranked WHERE rn = 1) r ON r.primaryid = c.primaryid""")
    say("Built case_area.")

    # ---------------- checks
    say("Starting validation checks...")
    say("\n" + "=" * 80 + "\nCHECKS\n" + "=" * 80)
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        say(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

    n_src, n_new = scalar("SELECT count(*) FROM clean_reaction"), scalar("SELECT count(*) FROM profile_reaction")
    check("profile_reaction has exactly the rows of clean_reaction", n_src == n_new, f"{n_new:,} vs {n_src:,}")
    d = scalar("SELECT count(*) FROM (SELECT primaryid, pt_key FROM profile_reaction GROUP BY 1, 2 HAVING count(*) > 1)")
    check("profile_reaction: one row per case and reaction", d == 0, f"{d:,} duplicates")
    say("Checking profile_reaction source-value consistency...")
    bad_profile = scalar("""
      SELECT count(*)
      FROM profile_reaction p
      JOIN clean_reaction r USING (primaryid, pt_key)
      JOIN clean_case c USING (primaryid)
      LEFT JOIN signal_pairs s
        ON c.ps_status = 'PS'
       AND s."window" = 'All quarters'
       AND s.ps_ingredient_std = c.ps_ingredient_std
       AND s.pt_key = r.pt_key
      WHERE p.prr_all IS DISTINCT FROM s.prr
         OR p.prr_flagged IS DISTINCT FROM coalesce(s.flagged_default, FALSE)
         OR p.is_administrative IS DISTINCT FROM
            (r.pt_key IN (SELECT pt_key FROM signal_admin_terms))
    """)
    check("profile_reaction PRR, flag, and admin values match their sources", bad_profile == 0, f"{bad_profile:,} mismatches")
    say("Checking that non-PS cases have no PRR...")
    non_ps_prr = scalar("""
      SELECT count(*) FROM profile_reaction p
      JOIN clean_case c USING (primaryid)
      WHERE c.ps_status <> 'PS' AND p.prr_all IS NOT NULL
    """)
    check("profile_reaction does not assign PS signal statistics to fallback/no-PS cases",
          non_ps_prr == 0, f"{non_ps_prr:,} non-PS PRR rows")
    say("Checking PRR coverage...")
    a = scalar("""SELECT count(*) FROM profile_reaction p JOIN clean_case c USING (primaryid)
                  WHERE c.ps_status = 'PS' AND p.prr_all IS NOT NULL""")
    b = scalar("""SELECT coalesce(sum(cases), 0) FROM signal_pairs WHERE "window" = 'All quarters' AND prr IS NOT NULL""")
    check("PRR attached to the right rows (independent count from signal_pairs)", a == b, f"{a:,} vs {b:,}")
    say("Checking indication-area coverage...")
    dup_i = scalar("SELECT count(*) FROM (SELECT indication_pt FROM ref_indication_area GROUP BY 1 HAVING count(*) > 1)")
    miss_i = scalar("""SELECT count(DISTINCT indication_pt) FROM clean_indication WHERE indication_pt IS NOT NULL
                       AND indication_pt NOT IN (SELECT indication_pt FROM ref_indication_area)""")
    expected_i = scalar("SELECT count(DISTINCT indication_pt) FROM clean_indication WHERE indication_pt IS NOT NULL")
    actual_i = scalar("SELECT count(*) FROM ref_indication_area")
    check("ref_indication_area: one row per indication, all indications covered",
          dup_i == 0 and miss_i == 0 and expected_i == actual_i,
          f"rows {actual_i:,}/{expected_i:,}, dup {dup_i}, unmapped {miss_i}")
    ca, ca_u, cc = scalar("SELECT count(*) FROM case_area"), scalar("SELECT count(DISTINCT primaryid) FROM case_area"), scalar("SELECT count(*) FROM clean_case")
    check("case_area: exactly one row per case", ca == ca_u == cc, f"{ca:,} / {ca_u:,} / {cc:,}")
    bad_a = scalar("""SELECT count(*) FROM case_area WHERE therapeutic_area <> 'No indication recorded'
                      AND therapeutic_area NOT IN (SELECT therapeutic_area FROM ref_indication_area)""")
    check("case_area values all come from ref_indication_area (or 'No indication recorded')", bad_a == 0, f"{bad_a:,} unexpected")
    if not all(results):
        raise RuntimeError("validation failed")

    say("Summarizing categories and writing the indication review CSV...")
    show("indication rows by therapeutic area (all drugs)",
         "SELECT a.therapeutic_area, count(*) FROM clean_indication i JOIN ref_indication_area a USING (indication_pt) GROUP BY 1 ORDER BY 2 DESC")
    show("cases by therapeutic area (primary-suspect drug)", "SELECT therapeutic_area, count(*) FROM case_area GROUP BY 1 ORDER BY 2 DESC")
    show("top 40 indications left as 'Other / unclassified' (extend RULES if these matter)",
         "SELECT i.indication_pt, count(*) FROM clean_indication i JOIN ref_indication_area a USING (indication_pt) "
         "WHERE a.therapeutic_area = 'Other / unclassified' GROUP BY 1 ORDER BY 2 DESC LIMIT 40")
    csv_path = (DB.parent / "review_indication_area.csv").as_posix()
    con.execute(f"""COPY (SELECT i.indication_pt, a.therapeutic_area, count(*) AS rows FROM clean_indication i
                          JOIN ref_indication_area a USING (indication_pt) GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 300)
                    TO '{csv_path}' (HEADER, DELIMITER ',')""")
    con.execute("COMMIT")
    transaction_open = False
    con.execute("CHECKPOINT")
    say(f"\nRESULT: ALL CHECKS PASSED | {time.time() - t0:.0f}s | database {DB.stat().st_size / 1e6:,.0f} MB")
except Exception as exc:
    if transaction_open:
        con.execute("ROLLBACK")
        transaction_open = False
        say(f"\nRESULT: FAILED, database transaction rolled back ({exc})")
    else:
        say(f"\nRESULT: TABLES COMMITTED, but a post-commit operation failed ({exc})")
    raise
finally:
    (DB.parent / "stage4_report.txt").write_text("\n".join(lines), encoding="utf-8")
    con.close()
