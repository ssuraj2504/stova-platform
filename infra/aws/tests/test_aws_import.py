"""Offline first-import safety checks; no AWS or private database is accessed."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


runtime = module("stova_aws_import_runtime", ROOT / "infra" / "aws" / "runtime.py")
aws = module("stova_aws_import_deployer", ROOT / "scripts" / "aws_deploy.py")
TABLES = ("users", "auth_sessions", "auth_throttles", "projects", "articles", "writing_accounts", "writing_reservations")


def manifest(**changes):
    value = {
        "archive": "source.dump", "format": "PostgreSQL custom",
        "consistentSnapshot": True, "sha256": "a" * 64, "sizeBytes": 128,
        "tables": {f"public.{table}": 0 for table in TABLES},
        "writingReservedNeurons": 6000,
        "validation": {"pgRestoreList": True, "allTableDataEntriesPresent": True,
                       "fullArchiveDecoded": True, "tableDataEntries": 8},
    }
    value["tables"].update({"public.alembic_version": 1, "public.users": 5, "public.projects": 2, "public.articles": 4,
                            "public.auth_sessions": 3, "public.auth_throttles": 1,
                            "public.writing_accounts": 1, "public.writing_reservations": 20})
    value.update(changes)
    return value


def toc():
    # Schema definitions must never enter the data allowlist.
    lines = ["; PostgreSQL archive TOC", "7; 2615 2200 SCHEMA - public stova",
             "9; 1259 16000 TABLE public users stova"]
    for index, table in enumerate(reversed((*TABLES, "alembic_version")), 100):
        lines.append(f"{index}; 0 16000 TABLE DATA public {table} stova")
    return "\n".join(lines) + "\n"


REVISION_SQL = "SET statement_timeout = 0;\nCOPY public.alembic_version (version_num) FROM stdin;\n0001_initial\n\\.\n"


class ImportManifestTests(unittest.TestCase):
    def test_complete_original_manifest_metadata_is_accepted(self):
        value = manifest()
        checked = runtime.validate_import_manifest(value)
        self.assertEqual(checked["tables"], value["tables"])
        self.assertEqual(checked["writingReservedNeurons"], 6000)

    def test_snapshot_hash_size_and_format_are_strict(self):
        for field, values in {
            "consistentSnapshot": [False, 1, "true", None],
            "sha256": ["a" * 63, "A" * 64, "invalid", None, 123],
            "sizeBytes": [True, False, 0, -1, 4.5, "128", None],
            "format": ["SQL", "pg_dump-custom", None],
            "writingReservedNeurons": [True, -300, 1, 5700, 6000.0, "6000", None],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises((ValueError, RuntimeError)):
                    runtime.validate_import_manifest(manifest(**{field: value}))
        for value in [None, [], "manifest"]:
            with self.assertRaises((ValueError, RuntimeError)):
                runtime.validate_import_manifest(value)

    def test_every_application_table_and_sole_migration_row_is_required(self):
        original = manifest()
        for table in original["tables"]:
            changed = deepcopy(original)
            del changed["tables"][table]
            with self.subTest(missing=table), self.assertRaises((ValueError, RuntimeError)):
                runtime.validate_import_manifest(changed)
        changed = deepcopy(original)
        changed["tables"]["public.unexpected"] = 0
        with self.assertRaises((ValueError, RuntimeError)):
            runtime.validate_import_manifest(changed)
        for count in [0, 2, True, "1"]:
            changed = deepcopy(original)
            changed["tables"]["public.alembic_version"] = count
            with self.subTest(alembic_rows=count), self.assertRaises((ValueError, RuntimeError)):
                runtime.validate_import_manifest(changed)

    def test_table_counts_cannot_be_coerced_or_negative(self):
        for count in [True, False, -1, 0.0, "0", None]:
            changed = manifest()
            changed["tables"]["public.users"] = count
            with self.subTest(count=count), self.assertRaises((ValueError, RuntimeError)):
                runtime.validate_import_manifest(changed)
        for counts in [None, [], "tables"]:
            with self.assertRaises((ValueError, RuntimeError)):
                runtime.validate_import_manifest(manifest(tables=counts))

    def test_archive_validation_metadata_must_be_complete_and_typed(self):
        for key in ("pgRestoreList", "allTableDataEntriesPresent", "fullArchiveDecoded"):
            for value in (False, 1, "true", None):
                changed = manifest()
                changed["validation"][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                    runtime.validate_import_manifest(changed)
        for value in (7, 9, True, "8", None):
            changed = manifest()
            changed["validation"]["tableDataEntries"] = value
            with self.subTest(entry_count=value), self.assertRaises(RuntimeError):
                runtime.validate_import_manifest(changed)


class ImportArchiveTests(unittest.TestCase):
    def test_only_expected_data_is_selected_in_foreign_key_order(self):
        data, revision = runtime.filter_import_toc(toc())
        selected = [line.split(" TABLE DATA ", 1)[1].split()[1] for line in data.splitlines() if " TABLE DATA " in line]
        self.assertEqual(selected, list(TABLES))
        self.assertNotIn("alembic_version", data)
        self.assertIn(" TABLE DATA public alembic_version ", revision)
        self.assertEqual(len([line for line in revision.splitlines() if line and not line.startswith(";")]), 1)
        for output in (data, revision):
            self.assertNotIn(" SCHEMA ", output)
            self.assertNotIn(" BLOBS ", output)
            self.assertNotIn(" TABLE public ", output)

    def test_unsupported_non_table_data_objects_are_refused(self):
        for entry in ["999; 0 0 BLOBS - BLOBS stova", "999; 0 0 BLOB - 100 stova",
                      "999; 0 0 SEQUENCE SET public users_id_seq stova",
                      "999; 0 0 MATERIALIZED VIEW DATA public view stova"]:
            with self.subTest(entry=entry), self.assertRaises(RuntimeError):
                runtime.filter_import_toc(toc() + entry + "\n")

    def test_unknown_duplicate_missing_and_cross_schema_table_data_is_refused(self):
        normal = toc()
        invalid = [normal + "999; 0 999 TABLE DATA public unexpected stova\n",
                   normal + "999; 0 999 TABLE DATA public users stova\n",
                   normal.replace("TABLE DATA public users", "TABLE DATA private users"),
                   "\n".join(line for line in normal.splitlines() if "TABLE DATA public users " not in line),
                   "\n".join(line for line in normal.splitlines() if "TABLE DATA public alembic_version " not in line)]
        for value in invalid:
            with self.subTest(toc=value), self.assertRaises((ValueError, RuntimeError)):
                runtime.filter_import_toc(value)

    def test_single_safe_archive_revision_is_extracted_without_output(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(runtime.archive_revision(REVISION_SQL), "0001_initial")
        self.assertEqual(output.getvalue(), "")

    def test_ambiguous_missing_or_unsafe_archive_revisions_are_refused(self):
        for sql in ["", REVISION_SQL.replace("0001_initial\n", ""),
                    REVISION_SQL.replace("0001_initial\n", "0001_initial\n0002_other\n"),
                    REVISION_SQL.replace("0001_initial", "initial'; DROP TABLE users; --"),
                    REVISION_SQL.replace("public.alembic_version", "public.users"),
                    REVISION_SQL + REVISION_SQL]:
            with self.subTest(sql=sql), self.assertRaises((ValueError, RuntimeError)):
                runtime.archive_revision(sql)


class ImportAssertionTests(unittest.TestCase):
    def test_preflight_locks_all_tables_and_preserves_existing_migration(self):
        sql = runtime.import_preflight_sql("0001_initial")
        self.assertIn("ACCESS EXCLUSIVE", sql.upper())
        self.assertIn("0001_initial", sql)
        for table in (*TABLES, "alembic_version"):
            self.assertIn(table, sql)
        for operation in ("COMMIT;", "TRUNCATE ", "DROP TABLE", "DELETE FROM", "UPDATE ", "INSERT INTO"):
            self.assertNotIn(operation, sql.upper())

    def test_verification_checks_all_manifest_counts_and_shared_budget(self):
        sql = runtime.import_verify_sql(runtime.validate_import_manifest(manifest()))
        for table in (*TABLES, "alembic_version"):
            self.assertIn(table, sql)
        self.assertIn("reserved_neurons", sql)
        self.assertIn("6000", sql)
        self.assertNotIn("COMMIT;", sql.upper())

    def test_revision_cannot_inject_sql(self):
        for revision in ["", "0001'; COMMIT; --", "0001\nCOMMIT", None]:
            with self.subTest(revision=revision), self.assertRaises((ValueError, RuntimeError)):
                runtime.import_preflight_sql(revision)


def plan(**changes):
    value = {"accountPlanType": "FREE", "accountPlanStatus": "ACTIVE", "accountPlanRemainingCredits": {"amount": 10, "unit": "USD"},
             "accountPlanExpirationDate": datetime.now(timezone.utc) + timedelta(days=1)}
    value.update(changes)
    return value


class EligibilityOnlySession:
    def __init__(self, state):
        self.state, self.calls = state, []

    def client(self, name, **kwargs):
        self.calls.append(name)
        if name != "freetier":
            raise AssertionError("Ineligible imports must not access storage, compute or remote commands")
        return self

    def get_account_plan_state(self):
        return self.state


class ImportEligibilityTests(unittest.TestCase):
    def test_deployer_refuses_before_storage_or_remote_commands(self):
        args = aws.parser().parse_args(["import-db", "--dump-file", "must-not-open.dump", "--manifest-file", "must-not-open.json"])
        for state in [plan(accountPlanType="PAID"), plan(accountPlanStatus="EXPIRED"), {}]:
            session = EligibilityOnlySession(state)
            with self.subTest(state=state), patch.object(aws, "remote") as remote, self.assertRaises(aws.Refusal):
                aws.initial_import(session, args)
            self.assertEqual(session.calls, ["freetier"])
            remote.assert_not_called()

    def test_host_refuses_before_download_or_service_stop(self):
        for state in [plan(accountPlanType="PAID"), {}]:
            session = EligibilityOnlySession(state)
            with self.subTest(state=state), patch.object(runtime, "compose") as compose, self.assertRaises(RuntimeError):
                runtime.import_database(session, SimpleNamespace())
            self.assertEqual(session.calls, ["freetier"])
            compose.assert_not_called()


STATE = {"InstanceId": "i-offline-test", "DistributionId": "distribution-offline", "ReleaseBucket": "private-offline-test", "Url": "https://offline.cloudfront.net"}


class ActivationSeparationTests(unittest.TestCase):
    def test_deferred_activation_is_explicit_and_default_is_preserved(self):
        self.assertFalse(aws.parser().parse_args(["deploy"]).defer_activation)
        self.assertTrue(aws.parser().parse_args(["deploy", "--defer-activation"]).defer_activation)

    def test_deploy_can_remain_private_without_changing_default_activation(self):
        stack = {"StackId": "offline-stack", "StackStatus": "CREATE_COMPLETE",
                 "Parameters": [{"ParameterKey": "LatestAmiId", "ParameterValue": "ami-offline"},
                                {"ParameterKey": "ExistingGitHubOidcArn", "ParameterValue": ""},
                                {"ParameterKey": "GitHubRepo", "ParameterValue": ""}],
                 "Outputs": [{"OutputKey": key, "OutputValue": value} for key, value in STATE.items()]}
        for defer in [False, True]:
            args = aws.parser().parse_args(["deploy", *( ["--defer-activation"] if defer else [])])
            session = Mock()
            with self.subTest(defer=defer), ExitStack() as context:
                for name, value in {
                    "require_free_plan": {"eligible": True}, "validate_release_inputs": Path("offline-images.tar.gz"),
                    "stack_details": stack, "prefix_list": "pl-offline", "require_unchanged_infrastructure": None,
                    "read_ignored_env": {}, "runtime_values": {"ORIGIN_VERIFY_SECRET": "OFFLINE_FAKE_ORIGIN_PROOF"},
                    "store_runtime": None, "ensure_instance_running": None, "release": STATE,
                }.items():
                    context.enter_context(patch.object(aws, name, return_value=value))
                activate = context.enter_context(patch.object(aws, "activate_distribution"))
                result = aws.deploy(session, args)
                self.assertEqual(result["InstanceId"], STATE["InstanceId"])
                if defer:
                    activate.assert_not_called()
                else:
                    activate.assert_called_once()

    def test_activate_refuses_before_remote_commands_on_ineligible_plan(self):
        session = EligibilityOnlySession(plan(accountPlanType="PAID"))
        args = aws.parser().parse_args(["activate"])
        with patch.object(aws, "remote") as remote, patch.object(aws, "runtime_values") as values, self.assertRaises(aws.Refusal):
            aws.activate(session, args)
        self.assertEqual(session.calls, ["freetier"])
        remote.assert_not_called()
        values.assert_not_called()

    def test_activate_rechecks_eligibility_after_readiness_before_exposure(self):
        args = aws.parser().parse_args(["activate"])
        stack = {"Outputs": [{"OutputKey": key, "OutputValue": value} for key, value in STATE.items()]}
        session = Mock()
        session.client.return_value.get_parameter.return_value = {"Parameter": {"Value": json.dumps({"ORIGIN_VERIFY_SECRET": "a" * 64})}}
        with patch.object(aws, "require_free_plan", side_effect=[{"eligible": True}, aws.Refusal("Eligibility changed")]) as gate, \
             patch.object(aws, "stack_details", return_value=stack), patch.object(aws, "remote") as remote, \
             patch.object(aws, "activate_distribution") as activate, self.assertRaises(aws.Refusal):
            aws.activate(session, args)
        self.assertEqual(gate.call_count, 2)
        self.assertIn("initial-import-receipt.json", " ".join(remote.call_args.args[2]))
        self.assertIn("readyz", " ".join(remote.call_args.args[2]))
        activate.assert_not_called()

    def test_activate_never_generates_an_origin_secret_when_existing_secret_is_invalid(self):
        args = aws.parser().parse_args(["activate"])
        stack = {"Outputs": [{"OutputKey": key, "OutputValue": value} for key, value in STATE.items()]}
        for value in [{}, {"ORIGIN_VERIFY_SECRET": "short"}, {"ORIGIN_VERIFY_SECRET": None}, []]:
            session = Mock()
            session.client.return_value.get_parameter.return_value = {"Parameter": {"Value": json.dumps(value)}}
            with self.subTest(value=value), patch.object(aws, "require_free_plan", return_value={"eligible": True}), \
                 patch.object(aws, "stack_details", return_value=stack), patch.object(aws, "remote"), \
                 patch.object(aws, "runtime_values", side_effect=AssertionError("Must not generate replacement secrets")), \
                 patch.object(aws, "activate_distribution") as activate, self.assertRaises(aws.Refusal):
                aws.activate(session, args)
            activate.assert_not_called()


class MemoryS3:
    def __init__(self, objects=None):
        self.objects = objects or {}
        self.downloads, self.uploads, self.deletions = [], [], []

    def head_object(self, *, Bucket, Key):
        return {"ContentLength": len(self.objects[Key])}

    def download_file(self, bucket, key, destination):
        self.downloads.append((bucket, key))
        Path(destination).write_bytes(self.objects[key])

    def upload_file(self, source, bucket, key, **kwargs):
        self.uploads.append((source, bucket, key, kwargs))

    def delete_objects(self, **kwargs):
        self.deletions.append(kwargs)


class ImportSession(EligibilityOnlySession):
    def __init__(self, s3):
        super().__init__(plan())
        self.s3 = s3

    def client(self, name, **kwargs):
        if name == "s3":
            self.calls.append(name)
            return self.s3
        return super().client(name, **kwargs)


class ImportWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stova-offline-import-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        (self.folder / ".runtime.env").write_text("OFFLINE_TEST_ONLY=true\n")
        (self.folder / "compose.aws.yaml").write_text("# Offline fixture only\n")
        self.archive_bytes = b"PGDMP" + b"OFFLINE SYNTHETIC ARCHIVE; NO PERSONAL DATA\n" * 3
        self.metadata = manifest(sha256=hashlib.sha256(self.archive_bytes).hexdigest(), sizeBytes=len(self.archive_bytes))
        self.key = "imports/offline-fixture/source.dump"
        self.metadata_key = "imports/offline-fixture/manifest.json"
        self.refresh_objects()
        self.session = ImportSession(self.s3)
        self.calls = []
        self.stage_failure = None
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(runtime, "ROOT", self.folder))
        self.private = self.context.enter_context(patch.object(runtime, "private_compose", side_effect=self.private_compose))

    def refresh_objects(self):
        metadata_bytes = json.dumps(self.metadata).encode()
        self.s3 = MemoryS3({self.key: self.archive_bytes, self.metadata_key: metadata_bytes})
        self.args = SimpleNamespace(region="us-east-1", stack="stova", bucket="offline-private-bucket", key=self.key,
                                    manifest_key=self.metadata_key, sha256=hashlib.sha256(self.archive_bytes).hexdigest(),
                                    manifest_sha256=hashlib.sha256(metadata_bytes).hexdigest())

    def private_compose(self, *args, output=None):
        self.calls.append(args)
        data = b""
        if "pg_restore" in args and "--list" in args:
            data = toc().encode()
        elif "pg_restore" in args and any("revision.list" in value for value in args):
            data = REVISION_SQL.encode()
        elif output is not None:
            output.write(b"-- Synthetic restore SQL never executed by these tests\n")
        if "psql" in args:
            transaction = args.count("--file") == 3
            if self.stage_failure == ("restore" if transaction else "preflight"):
                raise subprocess.CalledProcessError(1, ["psql"], output=b"PRIVATE_DATABASE_VALUE", stderr=b"PRIVATE_PASSWORD_HASH")
        return subprocess.CompletedProcess(args, 0, stdout=data, stderr=b"")

    def invoke(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return runtime.import_database(self.session, self.args)

    def test_success_uses_one_transaction_for_recheck_restore_and_verification(self):
        self.invoke()
        transactions = [args for args in self.calls if "psql" in args and args.count("--file") == 3]
        self.assertEqual(len(transactions), 1)
        transaction = transactions[0]
        self.assertIn("--single-transaction", transaction)
        self.assertIn("ON_ERROR_STOP=1", transaction)
        paths = [transaction[index + 1] for index, value in enumerate(transaction) if value == "--file"]
        self.assertEqual([Path(path).name for path in paths], ["preflight.sql", "restore.sql", "verify.sql"])
        stop = next(index for index, args in enumerate(self.calls) if args[0] == "stop")
        restore = self.calls.index(transaction)
        resume = next(index for index, args in enumerate(self.calls) if args[0] == "up")
        self.assertLess(stop, restore)
        self.assertLess(restore, resume)
        self.assertEqual(self.session.calls.count("freetier"), 2)
        receipt = json.loads((self.folder / "initial-import-receipt.json").read_text())
        self.assertEqual(receipt["tables"], self.metadata["tables"])
        self.assertEqual(receipt["sha256"], self.args.sha256)
        self.assertEqual(receipt["manifestSha256"], self.args.manifest_sha256)
        self.assertEqual(receipt["alembicRevision"], "0001_initial")
        self.assertFalse((self.folder / ".initial-database-import.lock").exists())
        self.assertEqual([path for path in self.folder.glob("initial-import-*") if path.is_dir()], [])
        self.assertTrue(any(args[:5] == ("exec", "-T", "db", "rm", "-f") for args in self.calls))
        self.assertFalse(any("--disable-triggers" in args for args in self.calls))

    def test_hash_mismatch_never_reaches_docker_or_api_stop(self):
        for field in ["sha256", "manifest_sha256"]:
            self.calls.clear()
            old = getattr(self.args, field)
            setattr(self.args, field, "f" * 64)
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.invoke()
            setattr(self.args, field, old)
            self.assertEqual(self.calls, [])
            self.assertFalse((self.folder / ".initial-database-import.lock").exists())

    def test_remote_size_guards_run_before_large_or_unexpected_download(self):
        original = self.s3.head_object
        for oversized_manifest in (True, False):
            self.s3.downloads.clear()

            def size(*, Bucket, Key):
                if oversized_manifest and Key == self.metadata_key:
                    return {"ContentLength": 65537}
                if not oversized_manifest and Key == self.key:
                    return {"ContentLength": len(self.archive_bytes) + 1}
                return original(Bucket=Bucket, Key=Key)

            with self.subTest(oversized_manifest=oversized_manifest), patch.object(self.s3, "head_object", side_effect=size), self.assertRaises(RuntimeError):
                self.invoke()
            expected = [] if oversized_manifest else [(self.args.bucket, self.metadata_key)]
            self.assertEqual(self.s3.downloads, expected)
            self.assertEqual(self.calls, [])

    def test_changed_plan_after_preflight_prevents_api_stop_and_restore(self):
        with patch.object(runtime, "free_plan", side_effect=[None, RuntimeError("Plan no longer verified")]), self.assertRaises(RuntimeError):
            self.invoke()
        self.assertTrue(any("psql" in args for args in self.calls))
        self.assertFalse(any(args[0] in ("stop", "up") or args.count("--file") == 3 for args in self.calls))

    def test_manifest_revision_mismatch_refuses_before_database_checks_or_service_stop(self):
        self.metadata["alembicRevision"] = "0002_other"
        self.refresh_objects()
        self.session = ImportSession(self.s3)
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assertFalse(any("psql" in args or args[0] in ("stop", "up") for args in self.calls))

    def test_populated_destination_preflight_refuses_without_stopping_api(self):
        self.stage_failure = "preflight"
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke()
        self.assertFalse(any(args[0] in ("stop", "up") for args in self.calls))
        self.assertFalse((self.folder / "initial-import-receipt.json").exists())

    def test_failed_transaction_never_resumes_api_or_prints_private_error_context(self):
        self.stage_failure = "restore"
        arguments = ["import-db", "--region", self.args.region, "--stack", self.args.stack, "--bucket", self.args.bucket,
                     "--key", self.args.key, "--sha256", self.args.sha256, "--manifest-key", self.args.manifest_key,
                     "--manifest-sha256", self.args.manifest_sha256]
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = runtime.main(arguments, session=self.session)
        self.assertEqual(result, 1)
        self.assertIn("CalledProcessError", output.getvalue())
        self.assertNotIn("PRIVATE_", output.getvalue())
        self.assertTrue(any(args[0] == "stop" for args in self.calls))
        self.assertFalse(any(args[0] == "up" for args in self.calls))
        self.assertFalse((self.folder / "initial-import-receipt.json").exists())
        self.assertFalse((self.folder / ".initial-database-import.lock").exists())

    def test_existing_import_lock_refuses_without_touching_other_import(self):
        lock = self.folder / ".initial-database-import.lock"
        lock.write_text("OTHER_OFFLINE_IMPORT")
        with self.assertRaises(FileExistsError):
            self.invoke()
        self.assertEqual(lock.read_text(), "OTHER_OFFLINE_IMPORT")
        self.assertEqual(self.calls, [])
        self.assertNotIn("s3", self.session.calls)


class ImportUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stova-offline-upload-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.archive = self.folder / "source.dump"
        self.archive.write_bytes(b"PGDMP" + b"OFFLINE_SYNTHETIC_DATA" * 5)
        self.metadata = self.folder / "source.manifest.json"
        self.value = manifest(sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest(), sizeBytes=self.archive.stat().st_size)
        self.metadata.write_text(json.dumps(self.value))
        self.args = aws.parser().parse_args(["import-db", "--dump-file", str(self.archive), "--manifest-file", str(self.metadata)])
        self.s3 = MemoryS3()
        self.session = ImportSession(self.s3)
        self.stack = {"StackStatus": "CREATE_COMPLETE", "Outputs": [{"OutputKey": key, "OutputValue": value} for key, value in STATE.items()]}

    def test_source_hash_mismatch_refuses_before_upload_or_remote(self):
        self.archive.write_bytes(b"PGDMP" + b"MODIFIED_OFFLINE_SYNTHETIC_DATA" * 5)
        with patch.object(aws, "remote") as remote, self.assertRaises(aws.Refusal):
            aws.initial_import(self.session, self.args)
        self.assertEqual(self.s3.uploads, [])
        remote.assert_not_called()

    def test_success_uses_scoped_encrypted_objects_and_pins_both_hashes(self):
        with patch.object(aws, "stack_details", return_value=self.stack), patch.object(aws, "remote") as remote:
            result = aws.initial_import(self.session, self.args)
        self.assertTrue(result["imported"])
        self.assertEqual(self.session.calls.count("freetier"), 2)
        self.assertEqual(len(self.s3.uploads), 3)
        _, bucket, key, options = self.s3.uploads[0]
        self.assertEqual(bucket, STATE["ReleaseBucket"])
        self.assertRegex(key, r"^imports/[a-f0-9]{32}/source\.dump$")
        self.assertEqual(self.s3.uploads[1][2], key.rsplit("/", 1)[0] + "/manifest.json")
        self.assertEqual(options, {"ExtraArgs": {"ServerSideEncryption": "AES256"}})
        self.assertTrue(all(upload[3] == options for upload in self.s3.uploads))
        command = shlex.split(remote.call_args.args[2][-1])
        self.assertIn(self.value["sha256"], command)
        self.assertIn(hashlib.sha256(self.metadata.read_bytes()).hexdigest(), command)
        self.assertNotIn("POSTGRES_PASSWORD", " ".join(command))
        deleted = self.s3.deletions[0]["Delete"]["Objects"]
        self.assertEqual({item["Key"] for item in deleted}, {upload[2] for upload in self.s3.uploads})

    def test_failed_remote_import_preserves_private_transfer_objects_for_review(self):
        with patch.object(aws, "stack_details", return_value=self.stack), patch.object(aws, "remote", side_effect=aws.Refusal("Offline restore rejected")), self.assertRaises(aws.Refusal):
            aws.initial_import(self.session, self.args)
        self.assertEqual(len(self.s3.uploads), 3)
        self.assertEqual(self.s3.deletions, [])

    def test_plan_recheck_failure_prevents_all_uploads(self):
        with patch.object(aws, "require_free_plan", side_effect=[{"eligible": True}, aws.Refusal("Plan changed")]), \
             patch.object(aws, "stack_details", return_value=self.stack), patch.object(aws, "remote") as remote, self.assertRaises(aws.Refusal):
            aws.initial_import(self.session, self.args)
        self.assertEqual(self.s3.uploads, [])
        remote.assert_not_called()


class PrivateRestoreOutputTests(unittest.TestCase):
    def test_docker_restore_never_inherits_private_stdout_or_stderr(self):
        with patch.object(runtime.subprocess, "run") as run:
            runtime.private_compose("exec", "-T", "db", "pg_restore", "--list", "offline.dump")
        self.assertIs(run.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
