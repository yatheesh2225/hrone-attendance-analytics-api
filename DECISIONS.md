# Design decisions

1. **Indexes.** I create a unique index on `employees.emp_code` and a unique compound index on `(attendance_logs.emp_code, attendance_logs.date)` to enforce business-key uniqueness under concurrency. Supporting indexes cover employee department ordering, attendance date ordering, employee/date filtering, status filtering and late-minute aggregation. I considered indexing every individual field but rejected it because redundant indexes add write and storage overhead without helping the tested query shapes.

2. **Simultaneous punch-ins.** Both requests may initially observe no matching record, but the unique compound index makes only one insert succeed. The winner returns `201`; MongoDB raises a duplicate-key error for the other request, which becomes `409`. A pre-check alone is not a concurrency control.

3. **Leaderboard ties.** The database ranks by total late minutes using competition ranking. Equal totals share a rank and the next rank is skipped (1, 2, 2, 4). I filter `rank <= limit` after ranking, so a tie at the cutoff can return more rows than `limit`; rows are then ordered by late minutes and `emp_code`.

4. **No-log employees.** Department headcount starts from the employee collection and counts everyone joined by month-end, even when the attendance lookup finds no logs. Later joiners are excluded; missing attendance is not fabricated as an `ABSENT` record.

5. **100x data.** I would validate plans and workload with production-like data, add compound indexes only for measured query shapes, and consider monthly summary materialization plus background refresh while keeping the current raw-log pipeline as the correctness reference.
