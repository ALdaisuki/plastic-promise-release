# Task 3 Report — P1-3: Daemon Bug Fix + scan_scheduler_health Activation

**Status:** COMPLETE
**Date:** 2026-07-03
**File:** `daemons/maintenance_daemon.py`

## Changes Made

### Change A: SQL column name fix (line 961-963)

Fixed wrong column names in the trust score anomaly query within `scan_innovation_opportunities()`.
The `trust_scores` table uses `target`, `trust`, `last_updated` -- not `target_id`, `trust_score`, `updated_at`.

**Before:**
```sql
SELECT target_id, trust_score FROM trust_scores
WHERE trust_score < 0.5 ORDER BY updated_at DESC LIMIT 5
```

**After:**
```sql
SELECT target, trust FROM trust_scores
WHERE trust < 0.5 ORDER BY last_updated DESC LIMIT 5
```

Impact: The trust_decline innovation proposal previously failed silently inside the `except Exception: pass` block, so no low-trust agents were ever flagged. Now it correctly reads from the `trust_scores` table.

### Change B: scan_scheduler_health activation (after line 1274)

Added hourly scheduler self-audit scanner as Priority B+, inserted between the existing Priority B scanner loop and Priority C heartbeat monitor.

- Lazy-initializes an `AdaptiveThrottle` with `base=3600` (hourly)
- Follows the same throttle/on_hit/on_empty pattern as all other scanners
- Runs when `tick % max(1, throttle.current // 10) == 0`
- Import `scan_scheduler_health` was already present at line 45

## Verification

- `scan_scheduler_health` import confirmed functional: module exists and resolves correctly
- SQL column names match the actual `trust_scores` table schema (target, trust, last_updated)
- New scanner block follows the identical pattern used by all 6 existing discovery scanners
- Throttle initialized lazily with config key `scan_scheduler_health`, base=3600s

## Self-Review

Both changes are isolated, single-file, and follow existing conventions in the daemon. No new imports needed. No signature changes to any function. Both bugs (silent SQL failure, missing scheduler health scan) were latent -- the SQL error was swallowed by `except Exception: pass`, and the scanner import existed but was never wired into the main loop.
