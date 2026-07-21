# Bug #631: Notion health check schema drift

The Notion health job checked only API connectivity, so a database missing
properties such as `Due At` or `Reminder Scheduled At` continued reporting
healthy until a later scheduled job hit an opaque Notion 400.

The regression tests live in `tests/unit/test_notion_schema_health.py`. They
cover complete schemas, missing properties, type mismatches, and the scheduler
alert that names the concrete schema failures.
