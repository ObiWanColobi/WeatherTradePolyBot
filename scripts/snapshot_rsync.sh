#!/usr/bin/env bash
# Pull new bucket snapshot parquet files from VPS to local archive.
# Verifies sha256 of each transferred file before declaring success.
#
# Run from local machine on a daily cron AFTER snapshot_rollup.py
# completes on the VPS (rollup runs 00:00 UTC, schedule rsync 00:15 UTC).
#
# Env vars required:
#   SNAPSHOT_VPS_HOST           — e.g. user@my-vps.example.com
#   SNAPSHOT_VPS_PATH           — e.g. /home/bot/TestCode1/snapshot_parquet
#   SNAPSHOT_LOCAL_ARCHIVE      — e.g. /f/CodeProjects/TestCode1/snapshot_parquet_local

set -euo pipefail

: "${SNAPSHOT_VPS_HOST:?SNAPSHOT_VPS_HOST must be set}"
: "${SNAPSHOT_VPS_PATH:?SNAPSHOT_VPS_PATH must be set}"
: "${SNAPSHOT_LOCAL_ARCHIVE:?SNAPSHOT_LOCAL_ARCHIVE must be set}"

mkdir -p "$SNAPSHOT_LOCAL_ARCHIVE"

echo "[rsync] pulling new parquet files from $SNAPSHOT_VPS_HOST:$SNAPSHOT_VPS_PATH"
rsync -av --ignore-existing --include='*.parquet' --exclude='*' \
    "$SNAPSHOT_VPS_HOST:$SNAPSHOT_VPS_PATH/" \
    "$SNAPSHOT_LOCAL_ARCHIVE/"

echo "[rsync] verifying sha256 of newly arrived files"
# For each parquet file present locally, compute sha256 and compare to remote
for local_file in "$SNAPSHOT_LOCAL_ARCHIVE"/*.parquet; do
    [ -e "$local_file" ] || continue
    fname=$(basename "$local_file")
    local_sha=$(sha256sum "$local_file" | awk '{print $1}')
    remote_sha=$(ssh "$SNAPSHOT_VPS_HOST" "sha256sum $SNAPSHOT_VPS_PATH/$fname 2>/dev/null | awk '{print \$1}'" || echo "MISSING")
    if [ "$remote_sha" = "MISSING" ]; then
        echo "[rsync] $fname: VPS no longer has file (probably cleaned up); local is authoritative"
        continue
    fi
    if [ "$local_sha" = "$remote_sha" ]; then
        echo "[rsync] $fname: VERIFIED ($local_sha)"
        # Touch a sidecar marker so cleanup script knows this date is safely backed up
        touch "$SNAPSHOT_LOCAL_ARCHIVE/.verified.$fname"
    else
        echo "[rsync] $fname: MISMATCH local=$local_sha remote=$remote_sha — NOT marking verified"
        exit 1
    fi
done

echo "[rsync] complete"
