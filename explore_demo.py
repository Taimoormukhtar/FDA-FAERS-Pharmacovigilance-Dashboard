# %%
from pathlib import Path

import pandas as pd

try:
    project_dir = Path(__file__).resolve().parent
except NameError:
    project_dir = Path.cwd()

raw_data_dir = project_dir / "raw data"
demo_file = (
    raw_data_dir
    / "faers_ascii_2024q3"
    / "ASCII"
    / "DEMO24Q3.txt"
)
output_dir = project_dir / "outputs"

# %%
df = pd.read_csv(
    demo_file,
    sep="$",
    dtype="string",
    keep_default_na=False,
    nrows=10,
)

print(f"Preview loaded: {demo_file.name}")
print(f"Preview rows: {len(df)}")
print(f"Columns: {len(df.columns)}")
display_columns = [
    "primaryid",
    "caseid",
    "event_dt",
    "age",
    "sex",
    "reporter_country",
]
print("\nDEMO preview:")
print(df[display_columns].to_string(index=False))

# %%
quarter_dirs = sorted(
    (
        folder
        for folder in raw_data_dir.iterdir()
        if folder.is_dir() and (folder / "ASCII").is_dir()
    ),
    key=lambda folder: folder.name.lower(),
)

processed_tables = 0
for quarter_dir in quarter_dirs:
    text_files = sorted((quarter_dir / "ASCII").glob("*.txt"))
    if not text_files:
        print(f"\nSkipped {quarter_dir.name}: no .txt tables found.")
        continue

    quarter_output_dir = output_dir / quarter_dir.name
    quarter_output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{quarter_dir.name}:")

    for text_file in text_files:
        if text_file.resolve() == demo_file.resolve():
            table_preview = df
        else:
            table_preview = pd.read_csv(
                text_file,
                sep="$",
                dtype="string",
                keep_default_na=False,
                nrows=10,
            )

        preview_file = quarter_output_dir / f"{text_file.stem}_preview.csv"
        table_preview.to_csv(
            preview_file,
            index=False,
            encoding="utf-8-sig",
        )
        processed_tables += 1
        print(
            f"  {text_file.name}: "
            f"{len(table_preview)} preview rows, "
            f"{len(table_preview.columns)} columns"
        )

print(
    f"\nFinished: {processed_tables} table previews saved "
    f"under {output_dir}"
)
