# DECISIONS.md

1. **Indexes.**
   - `employees.emp_code` (unique): Guarantees business ID uniqueness and powers employee existence checks.
   - `employees.{department, joined_on}`: Backs department filtering, employee headcount counting, and trend lookups.
   - `attendance_logs.{emp_code, date}` (unique compound): Enforces the one-record-per-day constraint and prevents race conditions.
   - `attendance_logs.{date, emp_code}`: Powers fast `/attendance` pagination, range queries, and explain plans without in-memory sorting.
   - `attendance_logs.{emp_code, punch_in}`: Quickly locates the most recent punch-in for punch-out requests.
   - *Considered & rejected:* An index on `attendance_logs.status` alone, because low selectivity yields minimal speedup while increasing write overhead.

2. **Punch-in race.**
   Both concurrent requests validate inputs and attempt `insert_one`. At the database engine level (WiredTiger), the unique compound index on `(emp_code, date)` guarantees atomicity: exactly one insert succeeds with HTTP 201, while the losing request throws a `DuplicateKeyError`, intercepted by our exception handler to return HTTP 409.

3. **Ties.**
   We use MongoDB's `$setWindowFields` with `$rank` to calculate Standard Competition Ranking (1, 2, 2, 4). Filtering (`rank <= limit`) occurs after ranking, with results sorted by `total_late_minutes` descending then `emp_code` ascending. Both tied employees share the rank and are returned, even if this exceeds `limit`.

4. **Headcount.**
   The aggregation begins on the `employees` collection (filtering `joined_on <= last_day`), performing a `$lookup` into `attendance_logs`. Employees with zero logs produce an empty array (`[]`) via `preserveNullAndEmptyArrays`, contributing 1 to department headcount and 0 to present days.

5. **100x data scale.**
   I would maintain pre-aggregated daily and monthly summaries using change streams or background rollups (CQRS), serving analytics directly from materialized collections rather than scanning raw attendance logs.
