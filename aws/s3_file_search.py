#!/usr/bin/env python3
"""
Search for files in a vendor-specific S3 bucket, inspect CSV/TSV columns,
and search column values across file versions.

Commands:
  list     List files under a path (with optional filename filter)
  columns  Read one file and return its column names
  search   Search column values across matching files and their versions (supports multiple criteria with --combine and/or)

Usage examples:

  # List all files under a path
  python s3_file_search.py list \\
    --bucket cut-dry-vendor-integration \\
    --path prod/some-vendor/inbound/

  # List files matching a filename
  python s3_file_search.py list \\
    --bucket cut-dry-vendor-integration \\
    --path prod/some-vendor/inbound/ \\
    --filename catalog

  # Show columns of a specific file
  python s3_file_search.py columns \\
    --bucket cut-dry-vendor-integration \\
    --key prod/some-vendor/inbound/catalog.csv

  # Show columns of the first matching file
  python s3_file_search.py columns \\
    --bucket cut-dry-vendor-integration \\
    --path prod/some-vendor/inbound/ \\
    --filename catalog.csv

  # Search a column value across matching files and their versions
  python s3_file_search.py search \\
    --bucket cut-dry-vendor-integration \\
    --path prod/some-vendor/inbound/ \\
    --filename catalog \\
    --column sku \\
    --value "ABC-123" \\
    --start 2026-02-26 \\
    --end 2026-03-05

  # Search with JSON output (pipe-friendly)
  python s3_file_search.py search \\
    --bucket cut-dry-vendor-integration \\
    --path prod/some-vendor/inbound/ \\
    --filename catalog \\
    --column sku \\
    --value "ABC-123" \\
    --start 2026-02-26 \\
    --end 2026-03-05 \\
    --output json

  # Multiple columns, AND: row must match all criteria
  python s3_file_search.py search \\
    --bucket cut-dry-vendor-integration \\
    --path prod/some-vendor/inbound/ \\
    --filename catalog \\
    --column sku --value "ABC" --column status --value "active" --combine and \\
    --start 2026-02-26 --end 2026-03-05

  # Same column, multiple values, OR: row matches if any value matches
  python s3_file_search.py search \\
    --bucket cut-dry-vendor-integration \\
    --path prod/some-vendor/inbound/ \\
    --filename catalog \\
    --column status --value "active" --column status --value "pending" --combine or \\
    --start 2026-02-26 --end 2026-03-05

  Timestamps are shown in a unified form: UTC plus your local zone (default Asia/Colombo,
  same offset as AWS UTC+05:30). Override with --tz ZONE or env S3_FILE_SEARCH_TZ.
  --tz / --profile may appear before or after the subcommand (e.g. list ... --tz Asia/Colombo).

  # Original file existence check (versions of a single file or multi-file timeframe filter)
  python s3_file_search.py exists \\
    --bucket cut-dry-vendor-integration \\
    --path prod/some-vendor/inbound/ \\
    --filename catalog.csv \\
    --start 2026-02-26 \\
    --end 2026-03-05
"""

import argparse
import csv
import io
import itertools
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-2"
LARGE_RESULT_WARNING = 100

# IANA zone for the second column in timestamps (matches AWS console when set to your region).
# Override with env S3_FILE_SEARCH_TZ or CLI --tz (e.g. Asia/Colombo, America/New_York).
_DEFAULT_DISPLAY_TZ = "Asia/Colombo"
_display_tz: ZoneInfo | None = None

KNOWN_BUCKETS = [
    "cut-dry-vendor-integration",
    "cut-dry-vendor-integration-pa",
    "cut-dry-vendor-reporting",
    "cut-dry-assets",
    "cut-dry-fsa",
    "cut-dry-manufacturer",
    "cut-dry-node-logs",
    "codify-logs",
    "fsa-assets",
    "fsa-categorization-tool",
    "ordering-supplies-images-1",
    "prdeli-pa",
    "mister-produce-pa",
    "voice-ordering",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_datetime(value: str, end_of_day: bool = False) -> datetime:
    date_only_fmt = "%Y-%m-%d"
    for fmt in ("%Y-%m-%d %H:%M:%S", date_only_fmt, "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            # If only a date was given and this is an end boundary, use 23:59:59
            if end_of_day and fmt == date_only_fmt:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Invalid datetime format: '{value}'. Use YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'"
    )


def parse_datetime_start(value: str) -> datetime:
    return parse_datetime(value, end_of_day=False)


def parse_datetime_end(value: str) -> datetime:
    return parse_datetime(value, end_of_day=True)


def init_display_tz(zone_name: str | None) -> ZoneInfo:
    """Resolve and store the timezone used for unified UTC | local display."""
    global _display_tz
    name = (zone_name or os.environ.get("S3_FILE_SEARCH_TZ") or _DEFAULT_DISPLAY_TZ).strip()
    try:
        _display_tz = ZoneInfo(name)
    except Exception:
        print(
            f"Warning: invalid timezone {name!r}; using {_DEFAULT_DISPLAY_TZ!r}.",
            file=sys.stderr,
        )
        _display_tz = ZoneInfo(_DEFAULT_DISPLAY_TZ)
    return _display_tz


def get_display_tz() -> ZoneInfo:
    if _display_tz is None:
        return init_display_tz(None)
    return _display_tz


def ensure_aware_utc(dt: datetime) -> datetime:
    """S3 LastModified is UTC; treat naive datetimes as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _offset_label_local(local_dt: datetime) -> str:
    """e.g. UTC+05:30 (same style as AWS console)."""
    s = local_dt.strftime("%z")
    if not s or s in ("+0000", "-0000"):
        return "UTC"
    sign, hh, mm = s[0], s[1:3], s[3:5]
    return f"UTC{sign}{hh}:{mm}"


def format_datetime_unified(dt: datetime) -> str:
    """
    One line: canonical UTC + local wall time with offset (like AWS "UTC+05:30").
    If display timezone is UTC, only the UTC part is shown.
    """
    utc = ensure_aware_utc(dt)
    utc_part = utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    tz = get_display_tz()
    local = utc.astimezone(tz)
    if local.utcoffset() == timedelta(0):
        return utc_part
    off = _offset_label_local(local)
    local_part = local.strftime("%Y-%m-%d %H:%M:%S")
    return f"{utc_part} | {local_part} ({off})"


def format_datetime_unified_short_header() -> str:
    """Column title reflecting current display zone."""
    try:
        return f"LastModified (UTC | {get_display_tz().key})"
    except Exception:
        return "LastModified (UTC | local)"


# ---------------------------------------------------------------------------
# Read-only guard — this script NEVER modifies, deletes, or writes any data.
# ---------------------------------------------------------------------------

# S3 write operations — blocked unconditionally.
_S3_WRITE_OPERATIONS = {
    "PutObject", "DeleteObject", "DeleteObjects",
    "CopyObject", "CreateMultipartUpload", "UploadPart",
    "CompleteMultipartUpload", "AbortMultipartUpload",
    "PutBucketAcl", "PutObjectAcl", "PutBucketPolicy",
    "PutBucketVersioning", "PutBucketLogging", "PutBucketLifecycle",
    "PutBucketReplication", "PutBucketCors", "PutBucketWebsite",
    "DeleteBucket", "CreateBucket", "RestoreObject",
    "DeleteObjectVersion",
}

# CloudTrail write operations — blocked unconditionally.
_CLOUDTRAIL_WRITE_OPERATIONS = {
    "CreateTrail", "UpdateTrail", "DeleteTrail",
    "StartLogging", "StopLogging",
    "PutEventSelectors", "PutInsightSelectors",
    "AddTags", "RemoveTags",
}


def _make_write_blocker(service: str, blocked: set[str]):
    """Return a before-call hook that raises on any write operation for the given service."""
    def _block(event_name: str, **_kwargs):
        operation = event_name.rsplit(".", 1)[-1]
        if operation in blocked:
            raise PermissionError(
                f"Blocked mutating {service} operation '{operation}'. "
                "This script is read-only and never modifies or deletes data."
            )
    return _block


def get_s3_client(profile: str | None = None):
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    client = session.client("s3", region_name=REGION)
    client.meta.events.register("before-call.s3.*", _make_write_blocker("S3", _S3_WRITE_OPERATIONS))
    return client


def get_cloudtrail_client(profile: str | None = None):
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    client = session.client("cloudtrail", region_name=REGION)
    client.meta.events.register(
        "before-call.cloudtrail.*",
        _make_write_blocker("CloudTrail", _CLOUDTRAIL_WRITE_OPERATIONS),
    )
    return client


def format_size(size_bytes: int) -> str:
    value: float = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def print_separator(char: str = "-", width: int = 80):
    print(char * width)


def warn(msg: str):
    print(c(f"\n⚠️  WARNING: {msg}\n", _C.YELLOW, _C.BOLD), file=sys.stderr)


# ---------------------------------------------------------------------------
# Terminal colour helpers — auto-disabled when output is not a TTY
# ---------------------------------------------------------------------------

class _C:
    """ANSI escape codes."""
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[31m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    CYAN   = "\033[36m"


# Evaluated once at import time; True when at least one stream is interactive.
_COLOR_ENABLED = sys.stdout.isatty() or sys.stderr.isatty()


def c(text: str, *codes: str) -> str:
    """Wrap *text* in ANSI escape codes if a TTY is detected, else return plain text."""
    if not _COLOR_ENABLED:
        return str(text)
    return "".join(codes) + str(text) + _C.RESET


# Filename date patterns — e.g. Catalog_Mar092026.csv, inventory_2026-03-09.csv
_FILENAME_DATE_PATTERNS = [
    # MonDDYYYY  e.g. Mar092026
    ("%b%d%Y", r"([A-Za-z]{3}\d{2}\d{4})"),
    # YYYY-MM-DD
    ("%Y-%m-%d", r"(\d{4}-\d{2}-\d{2})"),
    # YYYYMMDD
    ("%Y%m%d", r"(\d{8})"),
    # MM-DD-YYYY
    ("%m-%d-%Y", r"(\d{2}-\d{2}-\d{4})"),
]


def extract_filename_date(key: str) -> datetime | None:
    """Try to extract a date from the filename portion of an S3 key."""
    import re
    filename = key.rsplit("/", 1)[-1]
    for fmt, pattern in _FILENAME_DATE_PATTERNS:
        for m in re.finditer(pattern, filename):
            try:
                dt = datetime.strptime(m.group(1), fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def filter_by_filename_date(
    objects: list[dict], start: datetime, end: datetime
) -> list[dict]:
    """Filter objects whose filename-encoded date falls within [start, end]."""
    result = []
    for obj in objects:
        dt = extract_filename_date(obj["Key"])
        if dt and start.date() <= dt.date() <= end.date():
            result.append(obj)
    return result


# ---------------------------------------------------------------------------
# S3 primitives
# ---------------------------------------------------------------------------

def list_matching_objects(
    client,
    bucket: str,
    prefix: str,
    filename_filter: str = "",
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict]:
    paginator = client.get_paginator("list_objects_v2")
    matches = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            filename = key.rsplit("/", 1)[-1]
            if not filename:
                continue
            if filename_filter and filename_filter not in filename:
                continue

            if start is not None and end is not None:
                in_lastmod = start <= obj["LastModified"] <= end
                fdate = extract_filename_date(key)
                in_filename = fdate is not None and start.date() <= fdate.date() <= end.date()
                if not in_lastmod and not in_filename:
                    continue

            matches.append(obj)

    return matches


def list_versions_in_timeframe(
    client,
    bucket: str,
    key: str,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Return versions of a specific object whose LastModified falls within [start, end]."""
    paginator = client.get_paginator("list_object_versions")
    versions = []
    for page in paginator.paginate(Bucket=bucket, Prefix=key):
        for ver in page.get("Versions", []):
            if ver["Key"] != key:
                continue
            modified = ver["LastModified"]
            if start <= modified <= end:
                versions.append(ver)
    versions.sort(key=lambda v: v["LastModified"], reverse=True)
    return versions


def list_all_versions(client, bucket: str, key: str) -> list[dict]:
    """Return all versions of a specific object, newest first."""
    paginator = client.get_paginator("list_object_versions")
    versions = []
    for page in paginator.paginate(Bucket=bucket, Prefix=key):
        for ver in page.get("Versions", []):
            if ver["Key"] == key:
                versions.append(ver)
    versions.sort(key=lambda v: v["LastModified"], reverse=True)
    return versions


def list_delete_markers(client, bucket: str, prefix: str, filename_filter: str = "") -> list[dict]:
    """
    Return delete markers whose Key contains filename_filter under prefix, newest first.
    Each item: Key, VersionId, LastModified, IsLatest.
    """
    paginator = client.get_paginator("list_object_versions")
    markers = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for dm in page.get("DeleteMarkers", []):
            key = dm["Key"]
            if filename_filter and filename_filter.lower() not in key.lower():
                continue
            if not key.startswith(prefix):
                continue
            markers.append(dm)
    markers.sort(key=lambda m: m["LastModified"], reverse=True)
    return markers


def lookup_cloudtrail_deletes(
    profile: str | None,
    bucket: str,
    keys: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, list[dict]]:
    """
    Query CloudTrail LookupEvents for DeleteObject / DeleteObjects events
    covering the given keys.  Returns a dict keyed by object key → list of events.
    CloudTrail retains 90 days in the default trail; older events need Athena/S3 export.
    """
    try:
        ct = get_cloudtrail_client(profile)
    except Exception:
        return {}

    key_set = set(keys)
    results: dict[str, list[dict]] = {k: [] for k in key_set}

    try:
        paginator = ct.get_paginator("lookup_events")
        pages = paginator.paginate(
            LookupAttributes=[{"AttributeKey": "ResourceName", "AttributeValue": bucket}],
            StartTime=start,
            EndTime=end,
        )
        for page in pages:
            for event in page.get("Events", []):
                name = event.get("EventName", "")
                if name not in ("DeleteObject", "DeleteObjects"):
                    continue
                try:
                    ct_detail = json.loads(event.get("CloudTrailEvent", "{}"))
                except json.JSONDecodeError:
                    ct_detail = {}

                req = ct_detail.get("requestParameters", {}) or {}
                # DeleteObject: key is in requestParameters.key
                # DeleteObjects: keys are in requestParameters.delete.objects[].key
                affected: list[str] = []
                if name == "DeleteObject":
                    k = req.get("key") or req.get("Key")
                    if k:
                        affected.append(k)
                elif name == "DeleteObjects":
                    for obj in (req.get("delete", {}) or {}).get("objects", []):
                        k = obj.get("key") or obj.get("Key")
                        if k:
                            affected.append(k)

                identity = ct_detail.get("userIdentity", {}) or {}
                actor = (
                    identity.get("arn")
                    or identity.get("userName")
                    or identity.get("principalId")
                    or identity.get("type", "unknown")
                )
                source_ip = ct_detail.get("sourceIPAddress", "")
                user_agent = ct_detail.get("userAgent", "")
                event_time = event.get("EventTime")
                event_id = event.get("EventId", "")

                for k in affected:
                    if k in key_set:
                        results[k].append({
                            "event_id": event_id,
                            "event_name": name,
                            "event_time": event_time,
                            "actor": actor,
                            "source_ip": source_ip,
                            "user_agent": user_agent,
                        })
    except Exception as e:
        print(f"  (CloudTrail lookup failed: {e})", file=sys.stderr)

    return results


def filter_objects_in_timeframe(
    objects: list[dict], start: datetime, end: datetime
) -> list[dict]:
    return [obj for obj in objects if start <= obj["LastModified"] <= end]


def download_object(client, bucket: str, key: str, version_id: str | None = None) -> bytes:
    """Download an S3 object (optionally a specific version) and return raw bytes."""
    kwargs: dict = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    response = client.get_object(**kwargs)
    return response["Body"].read()


# ---------------------------------------------------------------------------
# CSV / TSV helpers
# ---------------------------------------------------------------------------

def detect_delimiter(sample: str) -> str:
    """Sniff delimiter; default to comma."""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t|;")
        return dialect.delimiter
    except csv.Error:
        return ","


def read_columns(raw: bytes, encoding: str = "utf-8-sig") -> list[str]:
    """Return the header row of a CSV/TSV file."""
    text = raw.decode(encoding, errors="replace")
    sample = text[:4096]
    delimiter = detect_delimiter(sample)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        headers = next(reader)
        return [h.strip() for h in headers]
    except StopIteration:
        return []


def search_column_in_file(
    raw: bytes,
    column: str,
    value: str,
    encoding: str = "utf-8-sig",
    case_sensitive: bool = False,
) -> list[dict]:
    """
    Return all rows where *column* contains *value*.
    Each result is a dict of the full row.
    """
    text = raw.decode(encoding, errors="replace")
    sample = text[:4096]
    delimiter = detect_delimiter(sample)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

    # Normalise header lookup (strip whitespace)
    reader.fieldnames = [f.strip() for f in (reader.fieldnames or [])]

    col_lower = column.lower()
    matched_col = None
    for field in (reader.fieldnames or []):
        if field.lower() == col_lower:
            matched_col = field
            break

    if matched_col is None:
        return []

    needle = value if case_sensitive else value.lower()
    hits = []
    for row in reader:
        cell = row.get(matched_col, "") or ""
        haystack = cell if case_sensitive else cell.lower()
        if needle in haystack:
            hits.append(dict(row))
    return hits


def search_columns_in_file(
    raw: bytes,
    criteria: list[tuple[str, str]],
    combine: str,
    encoding: str = "utf-8-sig",
    case_sensitive: bool = False,
) -> list[dict]:
    """
    Return rows that match all (combine=and) or any (combine=or) of the
    (column, value) criteria. Each criterion is a substring match in that column.
    """
    if not criteria:
        return []

    text = raw.decode(encoding, errors="replace")
    sample = text[:4096]
    delimiter = detect_delimiter(sample)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    reader.fieldnames = [f.strip() for f in (reader.fieldnames or [])]
    fieldnames = reader.fieldnames or []

    # Resolve each (column, value) to (matched_header, needle)
    resolved: list[tuple[str, str]] = []
    for column, value in criteria:
        col_lower = column.lower()
        matched_col = None
        for field in fieldnames:
            if field.lower() == col_lower:
                matched_col = field
                break
        if matched_col is None:
            return []
        needle = value if case_sensitive else value.lower()
        resolved.append((matched_col, needle))

    hits = []
    for row in reader:
        matches = []
        for matched_col, needle in resolved:
            cell = row.get(matched_col, "") or ""
            haystack = cell if case_sensitive else cell.lower()
            matches.append(needle in haystack)
        if combine == "and" and all(matches):
            hits.append(dict(row))
        elif combine == "or" and any(matches):
            hits.append(dict(row))
    return hits


def stream_search_columns(
    client,
    bucket: str,
    key: str,
    version_id: str | None,
    criteria: list[tuple[str, str]],
    combine: str,
    encoding: str = "utf-8-sig",
    case_sensitive: bool = False,
) -> list[dict]:
    """
    Stream an S3 object and search for matching rows without loading the
    entire file into memory.  Uses boto3 StreamingBody.iter_lines() so only
    one line at a time is buffered — safe for files of any size.
    """
    kwargs: dict = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    response = client.get_object(**kwargs)
    body = response["Body"]

    try:
        lines = body.iter_lines()

        # Peek at the first few lines to sniff the delimiter, then replay them.
        sample_lines: list[bytes] = list(itertools.islice(lines, 8))
        if not sample_lines:
            return []

        sample = "\n".join(ln.decode(encoding, errors="replace") for ln in sample_lines)
        delimiter = detect_delimiter(sample[:4096])

        def _text_rows():
            for ln in sample_lines:
                yield ln.decode(encoding, errors="replace")
            for ln in lines:
                yield ln.decode(encoding, errors="replace")

        reader = csv.DictReader(_text_rows(), delimiter=delimiter)
        reader.fieldnames = [f.strip() for f in (reader.fieldnames or [])]
        fieldnames = reader.fieldnames or []

        resolved: list[tuple[str, str]] = []
        for column, value in criteria:
            col_lower = column.lower()
            matched_col = next((f for f in fieldnames if f.lower() == col_lower), None)
            if matched_col is None:
                return []
            needle = value if case_sensitive else value.lower()
            resolved.append((matched_col, needle))

        hits: list[dict] = []
        for row in reader:
            row_matches = []
            for matched_col, needle in resolved:
                cell = row.get(matched_col, "") or ""
                haystack = cell if case_sensitive else cell.lower()
                row_matches.append(needle in haystack)
            if combine == "and" and all(row_matches):
                hits.append(dict(row))
            elif combine == "or" and any(row_matches):
                hits.append(dict(row))
        return hits
    finally:
        body.close()


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def _strip_empty(row: dict) -> dict:
    """Return a copy of row with empty/whitespace-only values removed."""
    return {k: v for k, v in row.items() if v and str(v).strip()}


def print_hits_text(all_hits: list[dict], criteria: list[tuple[str, str]], combine: str):
    """Original tabular text output. criteria is a list of (column, value) pairs."""
    seen: dict[tuple, list[dict]] = {}
    for hit in all_hits:
        k = (hit["key"], hit["version_id"], hit["last_modified"], hit["is_latest"])
        seen.setdefault(k, []).append(hit)

    for k, hlist in seen.items():
        key, vid, mod, latest = k
        rows = [h["row"] for h in hlist]
        print_separator("-")
        print(f"{c('File     :', _C.BOLD)} {c(key, _C.CYAN)}")
        print(f"{c('Version  :', _C.BOLD)} {vid}  |  Modified: {mod}  |  Latest: {latest or 'No'}")
        if len(criteria) > 1:
            crit_str = ", ".join(f"{col}={v!r}" for col, v in criteria)
            print(f"{c('Criteria :', _C.BOLD)} {crit_str}  (combine={combine})")
        print(f"{c('Matches  :', _C.BOLD)} {c(str(len(rows)), _C.GREEN, _C.BOLD)}")
        print()

        if rows:
            col_names = list(rows[0].keys())
            col_w = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows[:20])) for c in col_names}
            header = "  ".join(c.ljust(col_w[c]) for c in col_names)
            print("  " + header)
            print("  " + "  ".join("-" * col_w[c] for c in col_names))
            for row in rows[:20]:
                line = "  ".join(str(row.get(c, "")).ljust(col_w[c]) for c in col_names)
                print("  " + line)
            if len(rows) > 20:
                print(f"  ... and {len(rows) - 20} more row(s) not shown.")
        print()

    print_separator("=")
    print(c(f"Done. {len(all_hits)} total row(s) matched.", _C.GREEN, _C.BOLD))


def print_hits_json(
    all_hits: list[dict],
    query: dict,
    strip_empty: bool = True,
):
    """
    Emit a clean JSON document to stdout.

    Structure:
    {
      "query": { ...search params... },
      "total_matches": N,
      "results": [
        {
          "file": "...",
          "version_id": "...",
          "modified": "...",
          "is_latest": true/false,
          "match_count": N,
          "rows": [ { ...row fields (non-empty only if strip_empty)... }, ... ]
        },
        ...
      ]
    }
    """
    # Group by file+version
    groups: dict[tuple, list[dict]] = {}
    for hit in all_hits:
        k = (hit["key"], hit["version_id"], hit["last_modified"], hit["is_latest"])
        groups.setdefault(k, []).append(hit)

    results = []
    for k, hlist in groups.items():
        key, vid, mod, latest = k
        rows = [h["row"] for h in hlist]
        h0 = hlist[0]
        processed_rows = [_strip_empty(r) if strip_empty else r for r in rows]
        entry = {
            "file": key,
            "version_id": vid,
            "modified": mod,
            "is_latest": bool(latest),
            "match_count": len(rows),
            "rows": processed_rows,
        }
        if h0.get("last_modified_utc"):
            entry["modified_utc"] = h0["last_modified_utc"]
        if h0.get("last_modified_local"):
            entry["modified_local"] = h0["last_modified_local"]
        results.append(entry)

    output = {
        "query": query,
        "total_matches": len(all_hits),
        "results": results,
    }

    print(json.dumps(output, indent=2, default=str))


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_list(args, client):
    """List files under a path with an optional filename filter."""
    bucket = args.bucket
    prefix = args.path.rstrip("/") + "/" if args.path else ""
    filename_filter = args.filename or ""

    print(f"Bucket   : {bucket}")
    print(f"Path     : {prefix or '(root)'}")
    if filename_filter:
        print(f"Filename : {filename_filter}")
    print_separator("=")

    try:
        objects = list_matching_objects(client, bucket, prefix, filename_filter)
    except ClientError as e:
        print(c(f"ERROR: {e}", _C.RED, _C.BOLD), file=sys.stderr)
        sys.exit(1)

    if not objects:
        print(c("\nNo objects found.", _C.YELLOW))
        return

    total = len(objects)

    if total > LARGE_RESULT_WARNING:
        warn(
            f"{total} files found — this is a large result set. "
            "Consider narrowing your --path or --filename filter."
        )

    mod_w = 70
    mod_header = format_datetime_unified_short_header()
    mod_w = max(mod_w, len(mod_header))
    print(f"\nFound {c(str(total), _C.BOLD)} object(s):\n")
    print(f"{'#':<6} {mod_header:<{mod_w}} {'Size':<12} Key")
    print_separator()
    for i, obj in enumerate(
        sorted(objects, key=lambda o: o["LastModified"], reverse=True), 1
    ):
        mod = format_datetime_unified(obj["LastModified"])
        size = format_size(obj["Size"])
        print(f"{i:<6} {mod:<{mod_w}} {size:<12} {c(obj['Key'], _C.CYAN)}")
    print_separator()
    print(f"Total: {c(str(total), _C.BOLD)}")


def cmd_columns(args, client):
    """Read one file (by --key or first match of --filename) and print its columns."""
    bucket = args.bucket

    if args.key:
        key = args.key
    elif args.filename:
        prefix = args.path.rstrip("/") + "/" if args.path else ""
        try:
            matches = list_matching_objects(client, bucket, prefix, args.filename)
        except ClientError as e:
            print(c(f"ERROR: {e}", _C.RED, _C.BOLD), file=sys.stderr)
            sys.exit(1)
        if not matches:
            print(c(f"No objects found matching '{args.filename}' under '{prefix}'.", _C.YELLOW))
            sys.exit(1)
        if len(matches) > 1:
            print(
                f"Multiple files match '{args.filename}' — using the most recently modified one.\n"
                f"Use --key to target a specific file.\n"
            )
        matches.sort(key=lambda o: o["LastModified"], reverse=True)
        key = matches[0]["Key"]
    else:
        print(c("ERROR: Provide --key or --filename to identify the file.", _C.RED, _C.BOLD), file=sys.stderr)
        sys.exit(1)

    print(f"Bucket : {bucket}")
    print(f"Key    : {key}")
    print_separator("=")

    try:
        # Fetch only the first 16 KB — more than enough to read the header row.
        response = client.get_object(Bucket=bucket, Key=key, Range="bytes=0-16383")
        raw = response["Body"].read()
    except ClientError as e:
        print(c(f"ERROR downloading file: {e}", _C.RED, _C.BOLD), file=sys.stderr)
        sys.exit(1)

    columns = read_columns(raw)

    if not columns:
        print(c("Could not read columns — file may be empty or not CSV/TSV.", _C.YELLOW))
        sys.exit(1)

    print(f"\nFound {c(str(len(columns)), _C.BOLD)} column(s):\n")
    for i, col in enumerate(columns, 1):
        print(f"  {i:>3}.  {col}")
    print()


def cmd_search(args, client):
    """
    Search a column's values across all matching files and their versions
    within the given timeframe.
    """
    bucket = args.bucket
    prefix = args.path.rstrip("/") + "/" if args.path else ""
    filename_filter = args.filename
    criteria = list(zip(args.column, args.value))
    combine = args.combine
    start = args.start
    end = args.end
    output_fmt = getattr(args, "output", "text")

    # Progress/info goes to stderr when JSON mode is active so stdout stays clean
    info = sys.stderr if output_fmt == "json" else sys.stdout

    print(f"Bucket   : {bucket}", file=info)
    print(f"Path     : {prefix or '(root)'}", file=info)
    print(f"Filename : {filename_filter}", file=info)
    if len(criteria) == 1:
        print(f"Column : {criteria[0][0]}", file=info)
        print(f"Value  : {criteria[0][1]}", file=info)
    else:
        crit_str = ", ".join(f"{col}={v!r}" for col, v in criteria)
        print(f"Criteria : {crit_str}  (combine={combine})", file=info)
    print(f"From   : {format_datetime_unified(start)}", file=info)
    print(f"To     : {format_datetime_unified(end)}", file=info)
    print("=" * 80, file=info)

    # 1. Find matching files
    try:
        matches = list_matching_objects(client, bucket, prefix, filename_filter, start=start, end=end)
    except ClientError as e:
        print(c(f"ERROR: {e}", _C.RED, _C.BOLD), file=sys.stderr)
        sys.exit(1)

    if not matches:
        print(c(f"\nNo objects found matching '{filename_filter}' under '{prefix}' in the specified timeframe.", _C.YELLOW), file=info)
        if output_fmt == "json":
            print(json.dumps({"query": _build_query(args), "total_matches": 0, "results": []}, indent=2))
        sys.exit(0)

    total_files = len(matches)
    print(f"\nFound {c(str(total_files), _C.BOLD)} object(s) matching '{filename_filter}'.", file=info)

    if total_files > LARGE_RESULT_WARNING:
        warn(f"{total_files} files found — consider narrowing --path or --filename.")

    # 2. For each file, collect versions in timeframe
    all_hits: list[dict] = []

    for obj in matches:
        key = obj["Key"]
        print(f"\n  {c('Checking versions:', _C.CYAN)} {key}", file=info)

        try:
            versions = list_versions_in_timeframe(client, bucket, key, start, end)
        except ClientError:
            versions = []
            if start <= obj["LastModified"] <= end:
                versions = [{"Key": key, "VersionId": None,
                             "LastModified": obj["LastModified"],
                             "Size": obj["Size"], "IsLatest": True}]

        if not versions:
            print(c("    No versions in timeframe.", _C.DIM), file=info)
            continue

        n_crit = len(criteria)
        crit_desc = f"searching {n_crit} criterion/criteria (combine={combine})"
        print(f"    {c(str(len(versions)), _C.BOLD)} version(s) in timeframe — {crit_desc}...", file=info)

        for ver in versions:
            vid = ver.get("VersionId")
            try:
                hits = stream_search_columns(
                    client, bucket, key, vid,
                    criteria, combine,
                    case_sensitive=args.case_sensitive,
                )
            except ClientError as e:
                print(c(f"    ERROR downloading version {vid}: {e}", _C.RED, _C.BOLD), file=sys.stderr)
                continue
            if hits:
                lm = ver["LastModified"]
                mod = format_datetime_unified(lm)
                utc_iso = ensure_aware_utc(lm).strftime("%Y-%m-%dT%H:%M:%SZ")
                local_iso = ensure_aware_utc(lm).astimezone(get_display_tz()).isoformat(
                    timespec="seconds"
                )
                is_latest = "Yes" if ver.get("IsLatest") else ""
                print(c(f"    ✓ {len(hits)} match(es)  version={vid or 'N/A'}  modified={mod}  latest={is_latest}", _C.GREEN), file=info)
                for row in hits:
                    all_hits.append({
                        "key": key,
                        "version_id": vid or "N/A",
                        "last_modified": mod,
                        "last_modified_utc": utc_iso,
                        "last_modified_local": local_iso,
                        "is_latest": is_latest,
                        "row": row,
                    })
            else:
                print(c(f"    ✗ No matches  version={vid or 'N/A'}", _C.DIM), file=info)

    # 3. Output
    print(file=info)
    print("=" * 80, file=info)

    if not all_hits:
        crit_desc = ", ".join(f"{col}={v!r}" for col, v in criteria)
        msg = f"\nNo rows matched criteria ({crit_desc}, combine={combine}) in any version within the timeframe."
        print(c(msg, _C.YELLOW), file=info)
        if output_fmt == "json":
            print(json.dumps({"query": _build_query(args), "total_matches": 0, "results": []}, indent=2))
        sys.exit(0)

    print(c(f"\nTotal matching rows across all files/versions: {len(all_hits)}\n", _C.GREEN, _C.BOLD), file=info)

    if output_fmt == "json":
        print_hits_json(all_hits, query=_build_query(args))
    else:
        print_hits_text(all_hits, criteria=criteria, combine=combine)


def _build_query(args) -> dict:
    """Build a serialisable query summary from search args."""
    query = {
        "bucket": args.bucket,
        "path": args.path,
        "filename": args.filename,
        "criteria": [{"column": c, "value": v} for c, v in zip(args.column, args.value)],
        "combine": args.combine,
        "start": format_datetime_unified(args.start),
        "end": format_datetime_unified(args.end),
        "display_timezone": get_display_tz().key,
        "case_sensitive": args.case_sensitive,
    }
    if len(args.column) == 1:
        query["column"] = args.column[0]
        query["value"] = args.value[0]
    return query


def cmd_exists(args, client):
    """
    Original behaviour: check file existence / versions within a timeframe.
    Single match → list versions. Multiple matches → filter by LastModified.
    """
    bucket = args.bucket
    prefix = args.path.rstrip("/") + "/" if args.path else ""
    filename_filter = args.filename
    start = args.start
    end = args.end

    print(f"Bucket   : {bucket}")
    print(f"Path     : {prefix or '(root)'}")
    print(f"Filename : {filename_filter}")
    print(f"From   : {format_datetime_unified(start)}")
    print(f"To     : {format_datetime_unified(end)}")
    print_separator("=")

    try:
        matches = list_matching_objects(client, bucket, prefix, filename_filter, start=start, end=end)
    except ClientError as e:
        print(c(f"ERROR: {e}", _C.RED, _C.BOLD), file=sys.stderr)
        sys.exit(1)

    if not matches:
        print(c(f"\nNo objects found matching '{filename_filter}' under '{prefix}' in the specified timeframe.", _C.YELLOW))
        sys.exit(0)

    total = len(matches)
    print(f"\nFound {c(str(total), _C.BOLD)} object(s) matching '{filename_filter}'.\n")

    if total > LARGE_RESULT_WARNING:
        warn(
            f"{total} files found — this is a large result set. "
            "Consider narrowing your --path or --filename filter."
        )

    if total == 1:
        obj = matches[0]
        key = obj["Key"]
        print(f"Single file found: {c(key, _C.CYAN)}")
        print(
            f"Current LastModified: {format_datetime_unified(obj['LastModified'])}  "
            f"Size: {format_size(obj['Size'])}"
        )
        print_separator()
        print(c("Checking versions in timeframe...\n", _C.CYAN))

        try:
            versions = list_versions_in_timeframe(client, bucket, key, start, end)
        except ClientError as e:
            print(c(f"ERROR listing versions: {e}", _C.RED, _C.BOLD), file=sys.stderr)
            print("(Bucket versioning may not be enabled.)")
            sys.exit(1)

        if not versions:
            print(c(f"No versions found for '{key}' in the specified timeframe.", _C.YELLOW))
        else:
            mod_w = 70
            mod_h = format_datetime_unified_short_header()
            mod_w = max(mod_w, len(mod_h))
            print(f"{'#':<4} {'Version ID':<35} {mod_h:<{mod_w}} {'Size':<12} {'Latest?'}")
            print_separator()
            for i, ver in enumerate(versions, 1):
                vid = ver["VersionId"]
                mod = format_datetime_unified(ver["LastModified"])
                size = format_size(ver["Size"])
                latest = c("Yes", _C.GREEN) if ver.get("IsLatest") else ""
                print(f"{i:<4} {vid:<35} {mod:<{mod_w}} {size:<12} {latest}")
            print_separator()
            print(f"Total versions in timeframe: {c(str(len(versions)), _C.BOLD)}")

    else:
        print("Multiple files match — filtering by LastModified in timeframe...\n")
        filtered = filter_objects_in_timeframe(matches, start, end)

        if not filtered:
            # Fallback: these files are dated-per-filename (one object per day, not versioned).
            # Try matching by the date encoded in the filename (e.g. Catalog_Mar092026.csv).
            filename_matched = filter_by_filename_date(matches, start, end)
            if filename_matched:
                print(
                    c("No files matched by LastModified", _C.YELLOW) +
                    ", but found file(s) whose filename-encoded date falls within the timeframe:\n"
                )
                mod_w = 70
                mod_h = format_datetime_unified_short_header()
                mod_w = max(mod_w, len(mod_h))
                print(f"{'#':<4} {mod_h:<{mod_w}} {'Size':<12} Key")
                print_separator()
                for i, obj in enumerate(
                    sorted(filename_matched, key=lambda o: o["LastModified"], reverse=True), 1
                ):
                    mod = format_datetime_unified(obj["LastModified"])
                    size = format_size(obj["Size"])
                    print(f"{i:<4} {mod:<{mod_w}} {size:<12} {c(obj['Key'], _C.CYAN)}")
                print_separator()
                print(f"Files matched by filename date: {c(str(len(filename_matched)), _C.BOLD)} / {total} total")
            else:
                print(c("No matching files fall within the specified timeframe.", _C.YELLOW))
        else:
            mod_w = 70
            mod_h = format_datetime_unified_short_header()
            mod_w = max(mod_w, len(mod_h))
            print(f"{'#':<4} {mod_h:<{mod_w}} {'Size':<12} Key")
            print_separator()
            for i, obj in enumerate(
                sorted(filtered, key=lambda o: o["LastModified"], reverse=True), 1
            ):
                mod = format_datetime_unified(obj["LastModified"])
                size = format_size(obj["Size"])
                print(f"{i:<4} {mod:<{mod_w}} {size:<12} {obj['Key']}")
            print_separator()
            print(f"Files in timeframe: {len(filtered)} / {total} total matches")


def cmd_deleted(args, client):
    """
    Show delete markers for files matching --filename under --path, and optionally
    look up CloudTrail to show who deleted them and when.
    """
    bucket = args.bucket
    prefix = args.path.rstrip("/") + "/" if args.path else ""
    filename_filter = args.filename or ""
    no_cloudtrail = args.no_cloudtrail

    print(f"Bucket   : {bucket}")
    print(f"Path     : {prefix or '(root)'}")
    if filename_filter:
        print(f"Filename : {filename_filter}")
    print_separator("=")

    try:
        markers = list_delete_markers(client, bucket, prefix, filename_filter)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchBucket",):
            print(c(f"ERROR: Bucket '{bucket}' not found.", _C.RED, _C.BOLD), file=sys.stderr)
        else:
            print(c(f"ERROR listing versions: {e}", _C.RED, _C.BOLD), file=sys.stderr)
            print(
                "(Bucket may not have versioning enabled — delete markers require versioning.)",
                file=sys.stderr,
            )
        sys.exit(1)

    if not markers:
        print(c("\nNo delete markers found.", _C.YELLOW))
        return

    print(f"\nFound {c(str(len(markers)), _C.BOLD)} delete marker(s):\n")
    mod_h = format_datetime_unified_short_header()
    mod_w = max(70, len(mod_h))
    print(f"{'#':<4} {'Version ID':<35} {mod_h:<{mod_w}} {'Latest?':<8} Key")
    print_separator()
    for i, dm in enumerate(markers, 1):
        vid = dm.get("VersionId", "N/A")
        mod = format_datetime_unified(dm["LastModified"])
        latest = c("Yes", _C.GREEN) if dm.get("IsLatest") else ""
        print(f"{i:<4} {vid:<35} {mod:<{mod_w}} {latest:<8} {c(dm['Key'], _C.CYAN)}")
    print_separator()

    if no_cloudtrail:
        print(
            "\nTip: re-run without --no-cloudtrail to also look up who deleted each file in CloudTrail."
        )
        return

    # --- CloudTrail lookup ---
    unique_keys = list({dm["Key"] for dm in markers})
    # Widen the window slightly around the marker timestamps
    all_times = [dm["LastModified"] for dm in markers]
    ct_start = min(all_times) - timedelta(minutes=5)
    ct_end   = max(all_times) + timedelta(minutes=5)

    print(f"\n{c('Looking up CloudTrail events', _C.CYAN)} ({format_datetime_unified(ct_start)} → {format_datetime_unified(ct_end)})...")
    ct_events = lookup_cloudtrail_deletes(args.profile, bucket, unique_keys, ct_start, ct_end)

    any_found = any(evts for evts in ct_events.values())
    if not any_found:
        print(c(
            "\nNo CloudTrail DeleteObject events found for these keys in that window.\n"
            "This can mean:\n"
            "  • The deletion happened > 90 days ago (CloudTrail default retention).\n"
            "  • The bucket is in a different region (script uses REGION=" + REGION + ").\n"
            "  • CloudTrail is not enabled for this account/bucket.\n"
            "  • The deletion was done via a bulk lifecycle rule (not a user API call).",
            _C.YELLOW,
        ))
        return

    print()
    print_separator("=")
    print(c("CloudTrail deletion events:", _C.BOLD))
    print_separator("=")
    for key, evts in ct_events.items():
        if not evts:
            continue
        print(f"\n{c('Key:', _C.BOLD)} {c(key, _C.CYAN)}")
        for ev in evts:
            et = ev["event_time"]
            ts = format_datetime_unified(et) if isinstance(et, datetime) else str(et)
            print(f"  {c('Event     :', _C.BOLD)} {ev['event_name']}  ({ev['event_id']})")
            print(f"  {c('When      :', _C.BOLD)} {ts}")
            print(f"  {c('Actor     :', _C.BOLD)} {ev['actor']}")
            print(f"  {c('Source IP :', _C.BOLD)} {ev['source_ip']}")
            print(f"  {c('User-Agent:', _C.BOLD)} {ev['user_agent']}")
            print()
    print_separator("=")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # Shared on main *and* each subcommand so --profile / --tz work before OR after
    # the subcommand (e.g. `list -b BUCKET --tz Asia/Colombo`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", default=None, help="AWS profile name")
    common.add_argument(
        "--tz",
        metavar="ZONE",
        default=None,
        help=(
            "IANA timezone for the local part of unified timestamps "
            f"(default: env S3_FILE_SEARCH_TZ or {_DEFAULT_DISPLAY_TZ}). "
            "Example: Asia/Colombo, America/New_York"
        ),
    )

    parser = argparse.ArgumentParser(
        description="Search vendor S3 buckets, inspect CSV/TSV columns, and search column values.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Known buckets:\n  " + "\n  ".join(KNOWN_BUCKETS),
        parents=[common],
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ── list ──────────────────────────────────────────────────────────────────
    p_list = sub.add_parser("list", help="List files under a path", parents=[common])
    p_list.add_argument("--bucket", "-b", default="cut-dry-vendor-integration", help="S3 bucket name (default: cut-dry-vendor-integration)")
    p_list.add_argument("--path", "-p", default="", help="S3 prefix/path")
    p_list.add_argument("--filename", "-f", default="", help="Optional filename filter (substring match)")

    # ── columns ───────────────────────────────────────────────────────────────
    p_cols = sub.add_parser(
        "columns", help="Read a file and return its column names", parents=[common]
    )
    p_cols.add_argument("--bucket", "-b", default="cut-dry-vendor-integration", help="S3 bucket name (default: cut-dry-vendor-integration)")
    p_cols.add_argument("--path", "-p", default="", help="S3 prefix/path (used with --filename)")
    p_cols.add_argument("--key", "-k", default=None, help="Full S3 key of the file")
    p_cols.add_argument("--filename", "-f", default=None, help="Filename filter to find the file (substring match)")

    # ── search ────────────────────────────────────────────────────────────────
    p_search = sub.add_parser(
        "search",
        help="Search column values across matching files and their versions (supports multiple criteria with --combine and/or)",
        parents=[common],
    )
    p_search.add_argument("--bucket", "-b", default="cut-dry-vendor-integration", help="S3 bucket name (default: cut-dry-vendor-integration)")
    p_search.add_argument("--path", "-p", default="", help="S3 prefix/path")
    p_search.add_argument("--filename", "-f", required=True, help="Filename filter (substring match)")
    p_search.add_argument("--column", "-c", action="append", required=True, help="Column name to search in (repeat for multiple criteria)")
    p_search.add_argument("--value", "-v", action="append", required=True, help="Value to search for, substring match (repeat for each --column)")
    p_search.add_argument(
        "--combine", choices=["and", "or"], default="and",
        help="How to combine multiple criteria: all must match (and) or any match (or) (default: and)",
    )
    p_search.add_argument(
        "--start", required=False, default=None, type=parse_datetime_start,
        help="Start of timeframe (YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'). Defaults to yesterday.",
    )
    p_search.add_argument(
        "--end", required=False, default=None, type=parse_datetime_end,
        help="End of timeframe (YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'). Defaults to today.",
    )
    p_search.add_argument(
        "--case-sensitive", action="store_true", default=False,
        help="Make the value search case-sensitive (default: case-insensitive)",
    )
    p_search.add_argument(
        "--output", "-o", choices=["text", "json"], default="text",
        help="Output format: 'text' (default) or 'json' (stdout only, progress goes to stderr)",
    )

    # ── exists ────────────────────────────────────────────────────────────────
    p_exists = sub.add_parser(
        "exists",
        help="Check file existence / versions within a timeframe (original behaviour)",
        parents=[common],
    )
    p_exists.add_argument("--bucket", "-b", required=True, help="S3 bucket name")
    p_exists.add_argument("--path", "-p", default="", help="S3 prefix/path")
    p_exists.add_argument("--filename", "-f", required=True, help="Filename filter (substring match)")
    p_exists.add_argument(
        "--start", required=False, default=None, type=parse_datetime_start,
        help="Start of timeframe. Defaults to yesterday.",
    )
    p_exists.add_argument(
        "--end", required=False, default=None, type=parse_datetime_end,
        help="End of timeframe. Defaults to today.",
    )

    # ── deleted ───────────────────────────────────────────────────────────────
    p_deleted = sub.add_parser(
        "deleted",
        help="Show delete markers for matching files and look up who deleted them in CloudTrail",
        parents=[common],
    )
    p_deleted.add_argument("--bucket", "-b", required=True, help="S3 bucket name")
    p_deleted.add_argument("--path", "-p", default="", help="S3 prefix/path")
    p_deleted.add_argument("--filename", "-f", default="", help="Filename filter (substring match)")
    p_deleted.add_argument(
        "--no-cloudtrail",
        action="store_true",
        help="Skip CloudTrail lookup (just show delete markers from S3 versioning)",
    )

    return parser


def main():
    parser = build_parser()

    # Backward-compat: if first arg is not a subcommand (and not a help/version
    # flag), inject "exists" so the old --bucket / --filename style still works.
    import sys as _sys
    known_commands = {"list", "columns", "search", "exists", "deleted"}
    help_flags = {"-h", "--help", "--version"}
    first_arg = _sys.argv[1] if len(_sys.argv) > 1 else None
    first_positional = next(
        (a for a in _sys.argv[1:] if not a.startswith("-")), None
    )
    if first_arg not in help_flags and first_positional not in known_commands:
        _sys.argv.insert(1, "exists")

    args = parser.parse_args()

    init_display_tz(args.tz)

    # Apply default timeframe for commands that use --start / --end
    if args.command in ("search", "exists"):
        now_utc = datetime.now(timezone.utc)
        if args.end is None:
            args.end = now_utc.replace(hour=23, minute=59, second=59, microsecond=0)
        if args.start is None:
            args.start = (now_utc - timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

    # Validate date ordering for commands that use timeframes
    if args.command in ("search", "exists"):
        if args.start > args.end:
            parser.error("--start must be before --end")

    # Validate search criteria: same number of --column and --value
    if args.command == "search" and len(args.column) != len(args.value):
        parser.error("--column and --value must be specified the same number of times")

    client = get_s3_client(args.profile)

    dispatch = {
        "list": cmd_list,
        "columns": cmd_columns,
        "search": cmd_search,
        "exists": cmd_exists,
        "deleted": cmd_deleted,
    }
    try:
        dispatch[args.command](args, client)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()