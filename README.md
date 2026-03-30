# scripts

A collection of utility scripts for AWS S3 inspection and local data tooling.

---

## Requirements

- Python 3.10+
- `boto3` — AWS SDK (`pip install boto3`)
- AWS credentials configured (env vars, `~/.aws/credentials`, or an IAM role)
- `openpyxl` — only needed by `compare_columns.py` when reading/writing XLSX files (`pip install openpyxl`)

---

## Scripts

### `aws/s3_file_search.py`

Search and inspect CSV/TSV files stored in S3 buckets. Supports listing files, reading column headers, and searching column values across file versions — all without downloading entire files into memory (streams large files line-by-line).

**Subcommands**

| Command | Description |
|---------|-------------|
| `list` | List files under an S3 path |
| `columns` | Read and print column names from a file |
| `search` | Search a column value across matching files and their versions |
| `exists` | Check file existence / versions within a timeframe |
| `deleted` | Show delete markers and look up who deleted files via CloudTrail |

**Common flags**

| Flag | Description |
|------|-------------|
| `--bucket`, `-b` | S3 bucket name (default: `cut-dry-vendor-integration`) |
| `--path`, `-p` | S3 prefix / path |
| `--filename`, `-f` | Filename filter (substring match) |
| `--start` | Start of timeframe (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`). Defaults to yesterday. |
| `--end` | End of timeframe. Defaults to today. |
| `--profile` | AWS profile name |
| `--tz` | IANA timezone for timestamps (default: `Asia/Colombo`, override with env `S3_FILE_SEARCH_TZ`) |

**Usage examples**

```bash
# List all files under a path
python aws/s3_file_search.py list \
  --bucket cut-dry-vendor-integration \
  --path prod/some-vendor/inbound/

# List files matching a filename fragment
python aws/s3_file_search.py list \
  --bucket cut-dry-vendor-integration \
  --path prod/some-vendor/inbound/ \
  --filename catalog

# Show column names of a file
python aws/s3_file_search.py columns \
  --bucket cut-dry-vendor-integration \
  --path prod/some-vendor/inbound/ \
  --filename catalog.csv

# Search a column value across file versions (defaults: yesterday → today)
python aws/s3_file_search.py search \
  --bucket cut-dry-vendor-integration \
  --path prod/some-vendor/inbound/ \
  --filename catalog \
  --column sku --value "ABC-123"

# Search with an explicit date range
python aws/s3_file_search.py search \
  --path prod/some-vendor/inbound/ --filename catalog \
  --column sku --value "ABC-123" \
  --start 2026-02-26 --end 2026-03-05

# Multiple criteria — AND: all must match
python aws/s3_file_search.py search \
  --path prod/some-vendor/inbound/ --filename catalog \
  --column sku --value "ABC" \
  --column status --value "active" \
  --combine and

# Multiple criteria — OR: any match
python aws/s3_file_search.py search \
  --path prod/some-vendor/inbound/ --filename catalog \
  --column status --value "active" \
  --column status --value "pending" \
  --combine or

# Output as JSON (progress goes to stderr, JSON to stdout)
python aws/s3_file_search.py search \
  --path prod/some-vendor/inbound/ --filename catalog \
  --column sku --value "ABC-123" --output json

# Check file existence / versions in a timeframe
python aws/s3_file_search.py exists \
  --bucket cut-dry-vendor-integration \
  --path prod/some-vendor/inbound/ \
  --filename catalog.csv \
  --start 2026-02-26 --end 2026-03-05

# Show delete markers (and CloudTrail lookup for who deleted them)
python aws/s3_file_search.py deleted \
  --bucket cut-dry-vendor-integration \
  --path prod/some-vendor/inbound/ \
  --filename catalog.csv
```

**Notes**
- The script is **read-only** — all S3 and CloudTrail write operations are blocked unconditionally.
- Timestamps are shown in both UTC and local time (`UTC | local`).
- Interrupt with `Ctrl+C` at any time; it exits cleanly without a traceback.

---

### `aws/import-vendor-dump.py`

Import a verified-vendor SQL dump from S3 (`cut-dry-data-dumps/cut-dry-master/by_vendor/`) into the local development database. Works on the host or inside a Docker/Dev Container setup.

**Usage**

```bash
# Run from inside the cut-dry repo
python aws/import-vendor-dump.py <vendor_id>

# Point at the repo explicitly (from anywhere)
python aws/import-vendor-dump.py -v <vendor_id> --repo ~/Dev/cut-dry

# Or set the env var once so you never have to pass --repo
export CUT_DRY_REPO=~/Dev/cut-dry
python aws/import-vendor-dump.py -v <vendor_id>

# Skip the "vendor exists" prompt and replace automatically
python aws/import-vendor-dump.py -v <vendor_id> --replace

# Non-interactive (CI / scripted use)
python aws/import-vendor-dump.py -v <vendor_id> --replace --yes

# Use a specific dump file instead of the latest
python aws/import-vendor-dump.py -v <vendor_id> --dump-key vv-123-2026-03-01.sql.gz
```

**Docker / Dev Container setup**

Set these in `.env` (or your shell environment) when the app runs inside Docker:

```env
CUT_DRY_DOCKER_RUN=docker compose exec cut-dry
CUT_DRY_REPO_CONTAINER_PATH=/var/local/cut-dry/git
CUT_DRY_COMPOSE_PROJECT_NAME=cut-and-dry_devcontainer
```

The script downloads the dump into `repo_root/tmp/` (visible to the container) and runs `import-dump.sh` inside the container.

---

### `tools/compare_columns.py`

Compare specific columns between two CSV or XLSX files. Supports positional and key-based row matching, row filtering, and saving a diff report.

**Usage**

```bash
# Discover column names before building a mapping
python tools/compare_columns.py file1.csv file2.xlsx \
  --map x:x --list-columns

# Positional comparison (row 0 vs row 0, row 1 vs row 1, ...)
python tools/compare_columns.py file1.csv file2.xlsx \
  --map "sku:SKU" "name:product_name"

# Key-based comparison (match rows by a shared key — handles reordering)
python tools/compare_columns.py file1.csv file2.xlsx \
  --map "sku:SKU" "name:product_name" "price:unit_price" \
  --key1 sku --key2 SKU

# Same key column name in both files
python tools/compare_columns.py file1.csv file2.csv \
  --map "price:price" "qty:qty" \
  --key sku

# Ignore case and surrounding whitespace when comparing
python tools/compare_columns.py file1.csv file2.csv \
  --map "name:Name" --normalize

# Show only mismatched rows
python tools/compare_columns.py file1.csv file2.xlsx \
  --map "sku:SKU" "price:Price" --key1 sku --key2 SKU \
  --mismatches-only

# Save full diff report to CSV or XLSX
python tools/compare_columns.py file1.csv file2.xlsx \
  --map "sku:SKU" "name:product_name" --key1 sku --key2 SKU \
  --output diff_report.xlsx

# Filter rows before comparing
python tools/compare_columns.py order_guide.csv vendor.xlsx \
  --map "item_no:Item Code*" --key1 item_no --key2 "Item Code*" \
  --filter1 "Section~produce" \
  --filter2 "cust_no=40007"
```

**Filter expression syntax**

| Expression | Meaning |
|------------|---------|
| `col=value` | Exact match (case-insensitive with `--normalize`) |
| `col!=value` | Not equal |
| `col~value` | Contains (always case-insensitive) |
| `col!~value` | Does not contain |
| `col^value` | Starts with |
| `col$value` | Ends with |

Multiple `--filter1` / `--filter2` expressions are **AND**ed together.

---

## Colour output

All scripts emit ANSI colours when writing to a terminal and automatically strip them when output is piped or redirected.

| Colour | Meaning |
|--------|---------|
| Red bold | Errors |
| Yellow | Warnings / no results |
| Green | Matches / success |
| Cyan | File keys / progress |
| Dim | Skipped / no match per version |
| Bold | Counts and summaries |
