#!/bin/sh
# Nightly encrypted-at-rest-friendly backup: PostgreSQL dump + raw snapshot directory listing.
# Restore: gunzip -c backups/prospector-YYYYMMDD.sql.gz | psql -h db -U prospector prospector
set -eu
STAMP=$(date -u +%Y%m%d-%H%M)
KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}
mkdir -p /backups
pg_dump -h db -U prospector -d prospector --no-owner | gzip -9 > "/backups/prospector-${STAMP}.sql.gz"
tar -czf "/backups/snapshots-${STAMP}.tar.gz" -C /data . 2>/dev/null || true
find /backups -type f -mtime +"${KEEP_DAYS}" -delete
echo "backup ${STAMP} done; $(du -sh /backups | cut -f1) retained"
