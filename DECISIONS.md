# DECISIONS.md

1. **Indexes.** Unique on `employees.emp_code` and on logs `(emp_code, date)`, so identity is enforced by the database. Also on logs: `(date desc, emp_code)` for the list sort and month-range analytics, `(status, date desc, emp_code)` for status filters, `(emp_code, punch_in desc)` to find the record to close. Employees: `(department, emp_code)`, `(department, joined_on)`, `(joined_on)`. Rejected: an index on `late_minutes`; the date range already narrows the scan enough.

2. **Punch-in race.** Both requests compute the same date and call `insert_one` with no check first. The unique `(emp_code, date)` index lets exactly one insert win; the other raises `DuplicateKeyError` and I return 409.

3. **Ties.** `$setWindowFields` with `$rank` gives tied employees the same rank and skips the next. I filter `rank <= limit` afterwards, so a tie at the cutoff returns every tied employee, even beyond `limit`, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from employees who joined by month end and `$lookup`s their logs, so someone with none gets an empty array but still counts once.

5. **100x data.** Keep a per-department daily rollup, updated on every write, so trend and summary read small documents instead of scanning logs.
