#!/usr/bin/env bash
set -euo pipefail

archive="${1:?Usage: tools/restore-check-mongo.sh <archive.gz>}"
target_db="${2:-questly_restore_check}"
if [[ ! -s "$archive" ]]; then
  echo "Backup archive not found or empty: $archive" >&2
  exit 1
fi

# Restore into a separate empty database; this never drops the live database.
docker compose exec -T mongo mongorestore \
  --archive --gzip --stopOnError \
  --nsFrom='questly.*' --nsTo="${target_db}.*" < "$archive"
docker compose exec -T mongo mongosh "$target_db" --quiet \
  --eval 'printjson({users: db.users.countDocuments(), transactions: db.transactions.countDocuments(), pets: db.pets.countDocuments(), petEvents: db.pet_events.countDocuments()})'
