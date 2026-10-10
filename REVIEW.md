# Starter Code Review

| Area | Defect and concrete symptom | Fix in `app/main.py` |
|---|---|---|
| Readiness | `/health` always returned success even if MongoDB was unavailable. | Ping MongoDB and return 503 on a database error. |
| Indexes | No indexes were created by the starter. | Create idempotent unique business-key indexes and supporting query indexes at startup. |
| Employee creation | Checking for duplicates before inserting is race-prone. | Enforce uniqueness in MongoDB and translate duplicate-key errors to 409. |
| Timestamps | Naive/local timestamps and loosely validated epoch values can shift dates or accept invalid input. | Validate integer epoch milliseconds, truncate to seconds, and convert instants at the API boundary. |
| Rounding | Python `round` can use bankers' rounding. | Use `Decimal` with `ROUND_HALF_UP` for derived hours and reported values. |
| Late calculation | The 10-minute grace boundary and floor-minute rule were not exact. | Apply the strict `> 600` second test and floor elapsed minutes from shift start. |
| Overnight shifts | Shift end can fall on the next calendar day; early-morning punches belong to the prior shift date. | Resolve attendance day and shift boundaries in IST, including overnight shifts. |
| Overtime | The starter counted overtime without enforcing the 30-minute minimum. | Count whole minutes after shift end only when the result is at least 30. |
| Punch-in concurrency | A read-before-insert cannot prevent duplicate simultaneous requests. | Use the unique `(emp_code, date)` index as the final guard and return 409 on duplicate key. |
| Punch-out | The endpoint was absent. | Atomically update only records whose punch-out is still null; concurrent losers receive 409. |
| Regularization | The endpoint and correction audit trail were absent. | Validate changes, recompute derived values, append one history entry, and use an optimistic state check. The check handles both missing and existing history arrays. |
| Employee pagination | `skip = page * page_size` skipped the first page and totals ignored filters. | Use `(page - 1) * page_size`; apply the department filter to rows and count. |
| Attendance listing | Fetching/filtering/sorting/paginating all records in Python does not scale. | Apply filters, sorting, counting and pagination in MongoDB. |
| API shape | Mongo `_id` is internal and instants must be epoch milliseconds. | Exclude `_id` from responses and convert BSON dates to epoch milliseconds. |
| Analytics | The monthly, department, leaderboard and trend endpoints were missing. | Implement aggregation pipelines; department summary starts from employees so no-log employees count, leaderboard uses `$rank`, and trend generates dates plus a window average in MongoDB. |
| Explain | No endpoint proved query plans. | Add `executionStats` explain output for the five named endpoint query shapes. |

The source compiles and every path/method in `openapi.yaml` has a matching route. MongoDB-backed behaviour, concurrency, aggregation output and `IXSCAN`/no-`COLLSCAN` plans still require runtime verification against MongoDB 7.0 and the assignment's large dataset.
