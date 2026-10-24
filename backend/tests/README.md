# Backend acceptance tests

These tests require real PostgreSQL. They refuse to create or drop tables unless the selected database name ends in `_test`. Use a dedicated test database; the fixture recreates the application's tables for each test. Do not run separate copies of this suite against the same database concurrently.

Set `TEST_DATABASE_URL` to a PostgreSQL URL using the `asyncpg` driver, install the backend's `test` extras, and run from `backend/`:

```text
python -m pytest -q
```

Provider calls use an injected `httpx.MockTransport`. No live inference, external credentials, or network fallback is permitted. Tests cover authenticated ownership, CSRF and sessions, public publication boundaries, ten-article publishing, concurrent PostgreSQL reservations, UTC rollover, provider failures, generation/edit races, and stream disconnect cleanup.
