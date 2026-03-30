#!/usr/bin/env python3
"""
Import a verified-vendor SQL dump from S3 (cut-dry-master/by_vendor) into the local dev DB.

Checks if the vendor already exists; if so, prompts to replace (or use --replace / --yes).
Uses the repo's import-dump.sh and PHP for DB checks. Works when run from another repo
by setting CUT_DRY_REPO to the path of the cut-dry checkout.

When the app runs in Docker, set in .env (or env):
  CUT_DRY_DOCKER_RUN            e.g. "docker compose exec cut-dry"
  CUT_DRY_REPO_CONTAINER_PATH   e.g. "/var/local/cut-dry/git"
  CUT_DRY_COMPOSE_PROJECT_NAME  e.g. "cut-and-dry_devcontainer" (match "docker ps" project prefix)

The script will then run PHP and import-dump.sh inside the container; dumps are written
to repo_root/tmp/ so they are visible in the container.

Usage:
  python import-vendor-dump.py <vendor_id> [--replace] [--yes] [--dump-key KEY]
  CUT_DRY_REPO=/path/to/cut-dry python import-vendor-dump.py <vendor_id>  # from another repo

Requires: Python 3, boto3, AWS credentials (env or profile).
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from typing import Optional

try:
    import boto3
except ImportError:
    sys.exit("import-vendor-dump.py requires boto3. Install with: pip install boto3")


def _load_dotenv() -> None:
    """
    Load .env into os.environ (existing env wins).
    Searches the script's directory and each parent up to the filesystem root,
    stopping at the first .env found.
    """
    directory = os.path.dirname(os.path.abspath(__file__))
    while True:
        env_path = os.path.join(directory, '.env')
        if os.path.isfile(env_path):
            with open(env_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, _, value = line.partition('=')
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = value
            return
        parent = os.path.dirname(directory)
        if parent == directory:
            return
        directory = parent


_load_dotenv()

S3_BUCKET = 'cut-dry-data-dumps'
S3_PREFIX = 'cut-dry-master/by_vendor/'


def _has_index_php(path: str) -> bool:
    return os.path.isfile(os.path.join(path, 'public', 'index.php'))


def get_repo_root(cli_repo: Optional[str] = None) -> str:
    """
    Resolve the cut-dry repo root from (in priority order):
      1. --repo CLI flag
      2. CUT_DRY_REPO environment variable
      3. Current working directory
      4. Script ancestry (legacy: script inside the repo)
    """
    candidates: list[tuple[str, str]] = []

    if cli_repo:
        candidates.append((os.path.abspath(os.path.expanduser(cli_repo)), '--repo flag'))

    env_repo = os.environ.get('CUT_DRY_REPO')
    if env_repo:
        candidates.append((os.path.abspath(os.path.expanduser(env_repo)), 'CUT_DRY_REPO env var'))

    candidates.append((os.path.abspath(os.getcwd()), 'current working directory'))

    # Legacy: script inside the repo at scripts/dev_utilities/
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append((os.path.dirname(os.path.dirname(script_dir)), 'script ancestry'))

    for path, source in candidates:
        if _has_index_php(path):
            return path

    sys.exit(
        "Cut-dry repo root not found (missing public/index.php).\n\n"
        "Fix with one of:\n"
        f"  python {os.path.basename(__file__)} --repo ~/Dev/cut-dry -v <vendor_id>\n"
        "  export CUT_DRY_REPO=~/Dev/cut-dry\n"
        "  cd ~/Dev/cut-dry && python /path/to/import-vendor-dump.py -v <vendor_id>\n\n"
        f"Tried: {', '.join(p for p, _ in candidates)}"
    )


def choose_dump_key(client, vendor_id: int, dump_key: Optional[str]) -> str:
    """Return S3 key for the dump: either --dump-key or latest by prefix."""
    prefix = f"{S3_PREFIX}vv-{vendor_id}-"
    if dump_key:
        if not dump_key.startswith(S3_PREFIX):
            dump_key = S3_PREFIX + dump_key.lstrip('/')
        return dump_key
    paginator = client.get_paginator('list_objects_v2')
    keys = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            keys.append(obj['Key'])
    if not keys:
        sys.exit(f"No dumps found in s3://{S3_BUCKET}/{prefix}")
    keys.sort()
    return keys[-1]


def _find_compose_file(repo_root: str) -> Optional[str]:
    """Return path to docker-compose file if found in repo root or .devcontainer."""
    for name in ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml'):
        path = os.path.join(repo_root, name)
        if os.path.isfile(path):
            return path
    devcontainer = os.path.join(repo_root, '.devcontainer')
    if os.path.isdir(devcontainer):
        for name in ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml'):
            path = os.path.join(devcontainer, name)
            if os.path.isfile(path):
                return path
    return None


def _docker_cmd(repo_root: str) -> list[str]:
    """Build docker compose exec prefix with -f so config is found (e.g. in .devcontainer/)."""
    docker_run = os.environ.get('CUT_DRY_DOCKER_RUN')
    if not docker_run:
        return []
    parts = shlex.split(docker_run)
    if len(parts) >= 2 and parts[0] == 'docker' and parts[1] == 'compose':
        extra = []
        compose_file = _find_compose_file(repo_root)
        if compose_file:
            extra = ['-f', compose_file, '--project-directory', os.path.dirname(compose_file)]
        else:
            extra = ['--project-directory', repo_root]
        # Use same project name as running containers (e.g. cut-and-dry_devcontainer)
        project_name = os.environ.get('CUT_DRY_COMPOSE_PROJECT_NAME')
        if project_name:
            extra = extra + ['-p', project_name]
        # Disable Xdebug so PHP doesn't try to connect to a debugger (script runs non-interactively)
        rest = parts[2:]  # e.g. ["exec", "cut-dry"]
        rest = [rest[0], '-e', 'XDEBUG_MODE=off'] + rest[1:]
        return parts[:2] + extra + rest
    return parts


def _php_cmd(repo_root: str) -> list[str]:
    """Build PHP command: either host 'php' or docker exec prefix + container path."""
    docker_run = os.environ.get('CUT_DRY_DOCKER_RUN')
    container_path = os.environ.get('CUT_DRY_REPO_CONTAINER_PATH')
    if docker_run and container_path:
        return [*_docker_cmd(repo_root), 'php', f'{container_path.rstrip("/")}/public/index.php']
    return ['php', os.path.join(repo_root, 'public', 'index.php')]


def get_all_portal_vendor_ids(repo_root: str) -> list[int]:
    """Run PHP CLI printAllPortalVendorIDs; return list of portal vendor IDs."""
    cmd = _php_cmd(repo_root) + ['DataCloneLib/printAllPortalVendorIDs']
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        sys.exit(f"Failed to get portal vendor IDs: {result.stderr or result.stdout}")
    try:
        ids = json.loads((result.stdout or '').strip())
    except json.JSONDecodeError as e:
        sys.exit(f"Invalid JSON from printAllPortalVendorIDs: {e}")
    if not isinstance(ids, list):
        sys.exit(f"printAllPortalVendorIDs did not return a list: {result.stdout!r}")
    return [int(x) for x in ids]


def vendor_exists_in_db(repo_root: str, vendor_id: int) -> bool:
    """True if vendor_id is in the portal vendor list (i.e. already in DB)."""
    all_ids = get_all_portal_vendor_ids(repo_root)
    return vendor_id in all_ids


def run_import(repo_root: str, dump_path: str) -> None:
    """Call import-dump.sh with the given dump file path (host or in-container path)."""
    docker_run = os.environ.get('CUT_DRY_DOCKER_RUN')
    container_path = os.environ.get('CUT_DRY_REPO_CONTAINER_PATH')
    if docker_run and container_path:
        # dump_path must be the path as seen inside the container
        subprocess.run(
            [*_docker_cmd(repo_root), 'bash', f'{container_path.rstrip("/")}/scripts/dev_utilities/import-dump.sh', dump_path],
            check=True,
        )
    else:
        import_script = os.path.join(repo_root, 'scripts', 'dev_utilities', 'import-dump.sh')
        if not os.path.isfile(import_script):
            sys.exit(f"Import script not found: {import_script}")
        subprocess.run([import_script, dump_path], cwd=repo_root, check=True)


def run_elasticsearch_reset(repo_root: str) -> None:
    """Run ElasticsearchIndexResetter/afterDBImport so the app UI/search reflects the new data."""
    cmd = _php_cmd(repo_root) + ['ElasticsearchIndexResetter/afterDBImport']
    result = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
    if result.returncode != 0:
        print(
            "Warning: Elasticsearch index reset failed (app may not show new vendor until you run it):",
            result.stderr or result.stdout,
            file=sys.stderr,
        )
    else:
        print("Elasticsearch/OpenSearch index updated.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a verified-vendor dump from S3 into the local dev DB."
    )
    vendor_group = parser.add_mutually_exclusive_group(required=True)
    vendor_group.add_argument(
        'vendor_id',
        nargs='?',
        type=int,
        help="Verified vendor ID (or set CUT_DRY_DEFAULT_VENDOR_ID for default)",
    )
    vendor_group.add_argument(
        '-v', '--vendor_id',
        type=int,
        metavar='ID',
        dest='vendor_id_opt',
        help="Verified vendor ID (alternative to positional)",
    )
    parser.add_argument(
        '--replace',
        action='store_true',
        help="If vendor exists, replace without prompting",
    )
    parser.add_argument(
        '--yes', '-y',
        action='store_true',
        help="Non-interactive; fail if vendor exists and --replace not set",
    )
    parser.add_argument(
        '--dump-key',
        metavar='KEY',
        help="Specific S3 key under cut-dry-master/by_vendor/ (default: latest for vendor)",
    )
    parser.add_argument(
        '--repo',
        metavar='PATH',
        default=None,
        help="Path to the cut-dry repo root (overrides CUT_DRY_REPO env var)",
    )
    args = parser.parse_args()

    repo_root = get_repo_root(args.repo)
    print(f"Repo root: {repo_root}")

    vendor_id = args.vendor_id if args.vendor_id is not None else args.vendor_id_opt

    client = boto3.client('s3')
    dump_key = choose_dump_key(client, vendor_id, args.dump_key)
    print(f"Using dump: s3://{S3_BUCKET}/{dump_key}")

    # exists = vendor_exists_in_db(repo_root, vendor_id)
    # if exists and not args.replace:
    #     if args.yes:
    #         sys.exit(
    #             "Vendor already exists in DB. Use --replace to replace or run without --yes to prompt."
    #         )
    #     try:
    #         answer = input("Vendor already in DB. Replace? [y/N] ").strip().lower()
    #     except EOFError:
    #         answer = 'n'
    #     if answer not in ('y', 'yes'):
    #         print("Aborted.")
    #         sys.exit(0)

    docker_run = os.environ.get('CUT_DRY_DOCKER_RUN')
    container_path = os.environ.get('CUT_DRY_REPO_CONTAINER_PATH')
    if docker_run and container_path:
        tmp_dir = os.path.join(repo_root, 'tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        dump_basename = os.path.basename(dump_key)
        temp_path = os.path.join(tmp_dir, dump_basename)
        import_path_in_container = f'{container_path.rstrip("/")}/tmp/{dump_basename}'
    else:
        with tempfile.NamedTemporaryFile(suffix='.sql.gz', delete=False) as f:
            temp_path = f.name
        import_path_in_container = temp_path
    try:
        print(f"Downloading to {temp_path} ...")
        client.download_file(S3_BUCKET, dump_key, temp_path)
        run_import(repo_root, import_path_in_container)
    finally:
        if os.path.isfile(temp_path):
            os.remove(temp_path)

    print("Import complete.")
    # print("Updating Elasticsearch/OpenSearch index so the app shows the new data...")
    # run_elasticsearch_reset(repo_root)
    # print(
    #     "Done. Data is in the DB used by the app (inside Docker: mysql/graphp). "
    #     "If you query MySQL from the host, ensure you connect to the container’s MySQL (e.g. port published in compose)."
    # )


if __name__ == '__main__':
    main()
