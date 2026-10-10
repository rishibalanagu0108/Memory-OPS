# Backup and deletion-aware recovery

Status: **automation implemented and tested with synthetic data; production restore requires an
operator-approved isolated Neon branch**.

## Controls

Neon continuous point-in-time restore is the database backup baseline. The configured history window
must cover at least 24 hours and the declared five-minute RPO. Neon documents AES-256 encryption at
rest and mandatory TLS in transit. The machine-readable control is
[`../../deploy/recovery-policy.json`](../../deploy/recovery-policy.json).

PITR alone is insufficient for forgetting: restoring an old point can restore content deleted after
that point. Export the completed, content-free deletion ledger at least every five minutes to a
separate versioned encrypted operator store. The export contains only tenant, memory, version,
generation, reason, and completion identifiers—never memory content.

```bash
uv run --env-file .env.production python scripts/operations/recovery_drill.py \
  export-ledger --output /secure-operator-store/deletion-ledger.json
```

Use `DATABASE_URL_UNPOOLED`; `pg_dump`, recovery, and administrative session work must never use the
pooled URL.

## Restore drill

1. Declare an incident and stop serving the target environment. Preserve the pre-restore branch for
   rollback. Use Time Travel Assist to identify the recovery point.
2. Create an expiring `restore-drill-*` branch from the chosen historical point. Use synthetic or
   approved production-like data only; do not change the checked-out staging credentials.
3. Pull the drill branch's direct connection into an isolated secret environment. Set
   `NEON_BRANCH=restore-drill-*` and `MEMORY_OPS_RESTORE_OFFLINE=1`.
4. Retrieve the latest deletion ledger whose export is newer than the recovery point. Run:

```bash
uv run --env-file /secure/drill.env python scripts/operations/recovery_drill.py \
  replay-restore \
  --branch restore-drill-YYYYMMDD \
  --confirm-isolated-branch restore-drill-YYYYMMDD \
  --ledger /secure/deletion-ledger.json \
  --evidence /secure/recovery-evidence.json
```

5. Do not open traffic unless evidence reports status `pass`, deletion completeness `1.0`, zero
   resurrection, and `served_before_deletion_replay: false`. Run migrations and the complete
   compatibility/security suites before cutover.
6. After approval, cut over according to the provider restore procedure. Retain the automatic
   pre-restore backup until validation ends, then delete temporary branches according to policy.

The script refuses pooled connections, non-TLS URLs, branch names outside `restore-drill-*`, missing
offline mode, or confirmation mismatches. Replaying the same ledger is idempotent.

Provider references:

- [Neon security overview](https://neon.com/docs/security/security-overview)
- [Neon backup strategies](https://neon.com/docs/postgres/backup-restore/backups)
- [Neon instant restore](https://neon.com/docs/postgres/backup-restore/branch-restore)
