"""Opt-in transaction proof using two new, synthetic-only PostgreSQL databases.

STOVA_IMPORT_POSTGRES_TEST=1 enables this suite. It never reads the local app DB.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("stova_import_pg_runtime", ROOT / "infra/aws/runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


@unittest.skipUnless(os.environ.get("STOVA_IMPORT_POSTGRES_TEST") == "1", "Opt in to isolated synthetic PostgreSQL tests.")
class ImportPostgresTests(unittest.TestCase):
    @classmethod
    def docker(cls, *arguments, content=None, check=True):
        return subprocess.run(["docker", "exec", "-i", cls.container, *arguments], input=content, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)

    @classmethod
    def sql(cls, database, sql, *, transactional=False, check=True):
        arguments = ["psql", "-U", "stova", "-d", database, "--no-psqlrc", "-qAt", "--set", "ON_ERROR_STOP=1"]
        if transactional:
            arguments.append("--single-transaction")
        return cls.docker(*arguments, "--file=-", content=sql.encode() if isinstance(sql, str) else sql, check=check)

    @classmethod
    def setUpClass(cls):
        cls.container = os.environ.get("STOVA_IMPORT_TEST_CONTAINER", "stova-platform-postgres")
        token = secrets.token_hex(6)
        cls.source, cls.destination = f"stova_import_{token}_source_test", f"stova_import_{token}_destination_test"
        cls.created = []
        cls.addClassCleanup(cls.cleanup)
        for database in (cls.source, cls.destination):
            if not re.fullmatch(r"stova_import_[a-f0-9]+_(source|destination)_test", database):
                raise RuntimeError("Refusing an unsafe test database name.")
            cls.docker("createdb", "-U", "stova", database)
            cls.created.append(database)
        asyncio.run(cls.create_synthetic_schema())
        cls.archive = cls.docker("pg_dump", "-U", "stova", "-d", cls.source, "--format=custom").stdout
        toc = cls.docker("pg_restore", "--list", content=cls.archive).stdout.decode()
        data_toc, revision_toc = runtime.filter_import_toc(toc)
        cls.temporary = tempfile.TemporaryDirectory(prefix="stova-synthetic-import-")
        cls.addClassCleanup(cls.temporary.cleanup)
        work = Path(cls.temporary.name)
        cls.remote = "/tmp/stova-synthetic-import-" + token
        cls.docker("mkdir", "-m", "700", cls.remote)
        for name, value in (("source.dump", cls.archive), ("data.list", data_toc.encode()), ("revision.list", revision_toc.encode())):
            path = work / name
            path.write_bytes(value)
            subprocess.run(["docker", "cp", str(path), cls.container + ":" + cls.remote + "/" + name], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        options = ("--data-only", "--no-owner", "--no-privileges", "--file=-")
        cls.restore = cls.docker("pg_restore", *options, "--use-list=" + cls.remote + "/data.list", cls.remote + "/source.dump").stdout
        revision_sql = cls.docker("pg_restore", *options, "--use-list=" + cls.remote + "/revision.list", cls.remote + "/source.dump").stdout.decode()
        cls.revision = runtime.archive_revision(revision_sql)
        cls.manifest = {"format": "PostgreSQL custom", "consistentSnapshot": True,
                        "sha256": hashlib.sha256(cls.archive).hexdigest(), "sizeBytes": len(cls.archive),
                        "tables": {"public." + table: 1 for table in (*runtime.IMPORT_TABLES, "alembic_version")},
                        "writingReservedNeurons": 300, "validation": {"pgRestoreList": True, "allTableDataEntriesPresent": True, "fullArchiveDecoded": True, "tableDataEntries": 8}}

    @classmethod
    async def create_synthetic_schema(cls):
        from sqlalchemy import insert, text
        from sqlalchemy.ext.asyncio import create_async_engine
        from app.models import Base, User, AuthSession, AuthThrottle, Project, Article, WritingAccount, WritingReservation
        now = datetime.now(timezone.utc)
        for database in (cls.source, cls.destination):
            engine = create_async_engine(f"postgresql+asyncpg://stova:stova_dev_only@127.0.0.1:55432/{database}")
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                    await connection.execute(text("CREATE TABLE public.alembic_version (version_num varchar(32) PRIMARY KEY)"))
                    await connection.execute(text("INSERT INTO public.alembic_version VALUES ('0001_initial')"))
                    if database == cls.source:
                        await connection.execute(insert(User).values(id="synthetic-user", email="synthetic@example.invalid", name="Synthetic", password_hash="NOT_A_REAL_PASSWORD_HASH", created_at=now))
                        await connection.execute(insert(AuthSession).values(id="synthetic-session", user_id="synthetic-user", expires_at=now + timedelta(days=1), revoked=False))
                        await connection.execute(insert(AuthThrottle).values(scope="a" * 64, count=1, window_started_at=now))
                        await connection.execute(insert(Project).values(id="synthetic-project", owner_id="synthetic-user", name="Synthetic publication", color="#123456", logo=None, audience="", tone="Clear", status="published", created_at=now, updated_at=now))
                        await connection.execute(insert(Article).values(id="synthetic-article", project_id="synthetic-project", title="Synthetic article", slug="synthetic-article", content="Only synthetic test content.", status="published", word_count=4, position=0, updated_at=now, tag="", constraints="", drop_cap=False))
                        await connection.execute(insert(WritingAccount).values(account_scope="cf:" + "a" * 64))
                        await connection.execute(insert(WritingReservation).values(id="synthetic-reservation", account_scope="cf:" + "a" * 64, utc_day=now.date().isoformat(), adjacent_day=None, reserved_neurons=300, created_at=now))
            finally:
                await engine.dispose()

    @classmethod
    def cleanup(cls):
        if hasattr(cls, "remote"):
            cls.docker("rm", "-f", "--", *(cls.remote + "/" + name for name in ("source.dump", "data.list", "revision.list")), check=False)
            cls.docker("rmdir", "--", cls.remote, check=False)
        for database in getattr(cls, "created", []):
            cls.docker("dropdb", "-U", "stova", "--force", database, check=False)

    def setUp(self):
        self.sql(self.destination, "TRUNCATE " + ", ".join("public." + name for name in runtime.IMPORT_TABLES) + " CASCADE;")

    def transfer(self, manifest=None, revision=None, restore=None):
        sql = runtime.import_preflight_sql(revision or self.revision).encode() + (restore if restore is not None else self.restore) + runtime.import_verify_sql(manifest or self.manifest).encode()
        return self.sql(self.destination, sql, transactional=True, check=False)

    def counts(self):
        sql = "\n".join("SELECT count(*) FROM public." + table + ";" for table in (*runtime.IMPORT_TABLES, "alembic_version"))
        return [int(value) for value in self.sql(self.destination, sql).stdout.splitlines()]

    def test_verified_transfer_preserves_all_tables_and_published_status(self):
        self.assertEqual(self.transfer().returncode, 0)
        self.assertEqual(self.counts(), [1] * 8)
        self.assertEqual(self.sql(self.destination, "SELECT status FROM public.articles;").stdout.strip(), b"published")

    def test_count_mismatch_rolls_back_every_table(self):
        wrong = deepcopy(self.manifest)
        wrong["tables"]["public.users"] = 2
        self.assertNotEqual(self.transfer(wrong).returncode, 0)
        self.assertEqual(self.counts(), [0] * 7 + [1])

    def test_nonempty_destination_is_not_overwritten(self):
        self.assertEqual(self.transfer().returncode, 0)
        self.assertNotEqual(self.transfer().returncode, 0)
        self.assertEqual(self.counts(), [1] * 8)

    def test_revision_mismatch_leaves_destination_empty(self):
        self.assertNotEqual(self.transfer(revision="different_revision").returncode, 0)
        self.assertEqual(self.counts(), [0] * 7 + [1])

    def test_foreign_keys_remain_active_and_failure_rolls_back(self):
        invalid = re.sub(rb"(COPY public\.projects \([^\n]*\) FROM stdin;\n)([^\n]*)", lambda match: match[1] + match[2].replace(b"synthetic-user", b"missing-user"), self.restore, count=1)
        self.assertNotEqual(invalid, self.restore)
        self.assertNotEqual(self.transfer(restore=invalid).returncode, 0)
        self.assertEqual(self.counts(), [0] * 7 + [1])


if __name__ == "__main__":
    unittest.main()
