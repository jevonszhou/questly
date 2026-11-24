#!/usr/bin/env bash
set -euo pipefail

backup_dir="${1:-./backups}"
db_name="${MONGO_DB:-questly}"
mkdir -p "$backup_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$backup_dir/questly-${db_name}-${stamp}.archive.gz"

docker compose exec -T mongo mongodump \
  --db "$db_name" --archive --gzip > "$archive"
test -s "$archive"
printf 'Backup created: %s\n' "$archive"
