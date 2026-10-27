# %%
from pathlib import Path

import duckdb
import pandas as pd

project_dir = Path(__file__).resolve().parent
database_path = (
    project_dir
    / "outputs"
    / "profile_24Q3"
    / "profile_24Q3.duckdb"
)

# Read-only mode protects the profiling database from accidental edits.
connection = duckdb.connect(str(database_path), read_only=True)
print(f"Connected read-only to: {database_path}")

# %%
objects_df = connection.execute(
    """
    SELECT table_name, table_type
    FROM information_schema.tables
    WHERE table_schema = 'main'
    ORDER BY table_type, table_name
    """
).df()
print(objects_df.to_string(index=False))

# %%
row_counts = []
for object_name in objects_df["table_name"]:
    safe_name = object_name.replace('"', '""')
    count = connection.execute(
        f'SELECT count(*) FROM "{safe_name}"'
    ).fetchone()[0]
    row_counts.append({"name": object_name, "rows": count})

row_counts_df = pd.DataFrame(row_counts)
print(row_counts_df.to_string(index=False))

# %%
# Change this value to inspect another table, such as "drug" or "reac".
table_name = "demo"
available_names = set(objects_df["table_name"])
if table_name not in available_names:
    raise ValueError(
        f"Unknown table or view {table_name!r}. "
        f"Available: {sorted(available_names)}"
    )

safe_name = table_name.replace('"', '""')
preview_df = connection.execute(
    f'SELECT * FROM "{safe_name}" LIMIT 20'
).df()
print(f"\nFirst {len(preview_df)} rows from {table_name}:")
print(preview_df.to_string(index=False))

# %%
# In VS Code's Interactive Window, run this cell to display the DataFrame.
preview_df
