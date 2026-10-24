# Backend acceptance tests

These tests require real PostgreSQL. They refuse to create or drop tables unless the selected database name ends in `_test`. Use a dedicated test database; the fixture recreates the application's tables for each test. Do not run separate copies of this suite against the same database concurrently.

Create the disposable database first; the fixture creates tables, not the database itself. Install the backend's `test` extras and export `TEST_DATABASE_URL` into the test process environment. The fixture does not load a `.env` file. For example, use your dedicated test database credentials in one of these shell forms:

```sh
export TEST_DATABASE_URL='postgresql+asyncpg://test_user:test_password@127.0.0.1:5432/stova_test'
```

```powershell
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://test_user:test_password@127.0.0.1:5432/stova_test'
```

Then run from `backend/`:

```text
python -m pytest -q
```

Provider calls use an injected `httpx.MockTransport`. No live inference, external credentials, or network fallback is permitted. Tests cover authenticated ownership, CSRF and sessions, public publication boundaries, ten-article publishing, concurrent PostgreSQL reservations, UTC rollover, provider failures, generation/edit races, and stream disconnect cleanup.
