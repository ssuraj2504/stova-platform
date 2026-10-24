"""Offline tests: AWS clients are fakes and no network credentials are used."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

MODULE = Path(__file__).resolve().parents[3] / "scripts" / "aws_deploy.py"
spec = importlib.util.spec_from_file_location("stova_aws_deploy", MODULE)
aws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aws)
NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def active(**changes):
    value = {"accountPlanType": "FREE", "accountPlanStatus": "ACTIVE", "accountPlanRemainingCredits": {"amount": 10.5, "unit": "USD"}, "accountPlanExpirationDate": NOW + timedelta(days=5)}
    value.update(changes)
    return value


class FakeSession:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def client(self, name, **kwargs):
        self.calls.append(name)
        if name != "freetier":
            raise AssertionError(f"Unexpected non-eligibility AWS client: {name}")
        return self

    def get_account_plan_state(self):
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


class FreeAccountPlanTests(unittest.TestCase):
    def test_documented_active_free_plan_is_eligible(self):
        self.assertTrue(aws.assess_plan(active(), NOW)["eligible"])

    def test_paid_unknown_and_inactive_accounts_refused(self):
        for state in [active(accountPlanType="PAID"), active(accountPlanType="UNKNOWN"), active(accountPlanStatus="EXPIRED"), active(accountPlanStatus="NOT_STARTED"), {}, None]:
            with self.subTest(state=state):
                self.assertFalse(aws.assess_plan(state, NOW)["eligible"])

    def test_credit_values_fail_closed(self):
        for amount in [0, -1, None, "not-a-number", float("nan"), float("inf")]:
            with self.subTest(amount=amount):
                self.assertFalse(aws.assess_plan(active(accountPlanRemainingCredits={"amount": amount, "unit": "USD"}), NOW)["eligible"])
        for credits in [{"amount": 10, "unit": "EUR"}, "unexpected", None]:
            self.assertFalse(aws.assess_plan(active(accountPlanRemainingCredits=credits), NOW)["eligible"])

    def test_expiry_must_be_actual_future_datetime(self):
        for expiry in [None, NOW, NOW - timedelta(seconds=1), "2099-01-01"]:
            self.assertFalse(aws.assess_plan(active(accountPlanExpirationDate=expiry), NOW)["eligible"])
        self.assertTrue(aws.assess_plan(active(accountPlanExpirationDate=(NOW + timedelta(days=1)).replace(tzinfo=None)), NOW)["eligible"])

    def test_default_command_only_calls_read_only_eligibility_api(self):
        state = active(accountPlanExpirationDate=datetime.now(timezone.utc) + timedelta(days=1))
        session = FakeSession(state)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(aws.main([], session=session), 0)
        self.assertEqual(session.calls, ["freetier"])
        self.assertIn('"eligible": true', output.getvalue())

    def test_unknown_account_read_failure_refuses(self):
        session = FakeSession(RuntimeError("Secret-bearing exception must not be echoed"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(aws.main([], session=session), 2)
        self.assertNotIn("Secret-bearing", output.getvalue())

    def test_deploy_gate_precedes_env_read_and_all_mutating_clients(self):
        args = aws.parser().parse_args(["deploy", "--env-file", ".env.must-not-be-read"])
        session = FakeSession(active(accountPlanType="PAID"))
        with patch.object(aws, "read_ignored_env", side_effect=AssertionError("Environment file read before eligibility")):
            with self.assertRaises(aws.Refusal):
                aws.deploy(session, args)
        self.assertEqual(session.calls, ["freetier"])

    def test_release_refuses_before_any_storage_or_remote_operation(self):
        session = FakeSession(active(accountPlanStatus="EXPIRED"))
        with self.assertRaises(aws.Refusal):
            aws.release(session, aws.parser().parse_args(["release"]))
        self.assertEqual(session.calls, ["freetier"])

    def test_no_paid_plan_override_flag(self):
        options = {flag for action in aws.parser()._actions for flag in action.option_strings}
        self.assertFalse({"--force", "--allow-paid", "--skip-eligibility"} & options)


TEMPLATE = {"Resources": {"Instance": {"Type": "AWS::EC2::Instance", "Properties": {"ImageId": {"Ref": "LatestAmiId"}}}}}
PARAMETERS = [{"ParameterKey": key, "ParameterValue": value} for key, value in {
    "CloudFrontPrefixListId": "pl-123", "LatestAmiId": "ami-pinned", "GitHubRepo": "", "ExistingGitHubOidcArn": ""
}.items()]
STATE = {"InstanceId": "i-local-test", "DistributionId": "distribution-test", "ReleaseBucket": "private-test-bucket", "Url": "https://test.cloudfront.net"}


def stack_fixture():
    return {"StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/stova/identity-one",
            "StackName": "stova", "StackStatus": "CREATE_COMPLETE", "Parameters": deepcopy(PARAMETERS),
            "Outputs": [{"OutputKey": key, "OutputValue": value} for key, value in STATE.items()]}


class Clients:
    def __init__(self, **clients):
        self.clients = clients
        self.calls = []

    def client(self, name, **kwargs):
        self.calls.append(name)
        if name not in self.clients:
            raise AssertionError(f"Unexpected AWS client: {name}")
        return self.clients[name]


class InfrastructureFreezeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stova-deploy-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        (self.folder / "template.json").write_text(json.dumps(TEMPLATE))
        self.archive = self.folder / "images.tar.gz"
        self.archive.write_bytes(b"local test release fixture")
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.gate = self.context.enter_context(patch.object(aws, "require_free_plan", return_value={"eligible": True}))
        self.env = self.context.enter_context(patch.object(aws, "read_ignored_env", return_value={}))
        self.settings = self.context.enter_context(patch.object(aws, "runtime_values", return_value={"ORIGIN_VERIFY_SECRET": "local-test-proof"}))
        self.store = self.context.enter_context(patch.object(aws, "store_runtime"))
        self.release = self.context.enter_context(patch.object(aws, "release", return_value=STATE))
        self.activate = self.context.enter_context(patch.object(aws, "activate_distribution"))
        self.resume = self.context.enter_context(patch.object(aws, "ensure_instance_running"))
        self.context.enter_context(patch.object(aws, "prefix_list", return_value="pl-123"))
        self.context.enter_context(patch.object(aws, "AWS", self.folder))
        self.cfn = Mock()
        self.cfn.get_template.return_value = {"TemplateBody": json.dumps(TEMPLATE, indent=4)}
        self.ssm = Mock()
        self.ssm.get_parameter.return_value = {"Parameter": {"Value": "ami-pinned"}}
        self.session = Clients(cloudformation=self.cfn, ssm=self.ssm)
        self.args = aws.parser().parse_args(["deploy", "--env-file", ".env.test.local", "--archive", str(self.archive)])

    def test_unchanged_existing_stack_skips_all_cloudformation_writes(self):
        current = stack_fixture()
        current["Parameters"].reverse()
        with patch.object(aws, "stack_details", return_value=current):
            self.assertEqual(aws.deploy(self.session, self.args), STATE)
        self.cfn.update_stack.assert_not_called()
        self.cfn.create_stack.assert_not_called()
        self.cfn.get_waiter.assert_not_called()
        self.resume.assert_called_once_with(self.session, STATE["InstanceId"])
        self.store.assert_called_once()
        self.release.assert_called_once()
        self.activate.assert_called_once()

    def test_changed_template_refuses_before_runtime_reads_or_writes(self):
        changed = deepcopy(TEMPLATE)
        changed["Resources"]["Instance"]["Properties"]["InstanceType"] = "t3.small"
        (self.folder / "template.json").write_text(json.dumps(changed))
        with patch.object(aws, "stack_details", return_value=stack_fixture()):
            with self.assertRaisesRegex(aws.Refusal, "No automatic update or replacement"):
                aws.deploy(self.session, self.args)
        self.env.assert_not_called()
        self.settings.assert_not_called()
        self.store.assert_not_called()
        self.resume.assert_not_called()
        self.release.assert_not_called()
        self.cfn.update_stack.assert_not_called()
        self.cfn.create_stack.assert_not_called()

    def test_changed_parameters_refuse_even_when_template_matches(self):
        self.args.github_repo = "new-owner/new-repository"
        with patch.object(aws, "stack_details", return_value=stack_fixture()):
            with self.assertRaisesRegex(aws.Refusal, "Infrastructure differs"):
                aws.deploy(self.session, self.args)
        self.cfn.update_stack.assert_not_called()
        self.store.assert_not_called()
        self.release.assert_not_called()

    def test_unverifiable_existing_template_refuses(self):
        self.cfn.get_template.return_value = {"TemplateBody": "not-json"}
        with patch.object(aws, "stack_details", return_value=stack_fixture()):
            with self.assertRaisesRegex(aws.Refusal, "could not be verified"):
                aws.deploy(self.session, self.args)
        self.store.assert_not_called()
        self.resume.assert_not_called()

    def test_initial_stack_creation_remains_supported(self):
        with patch.object(aws, "stack_details", side_effect=[None, stack_fixture()]):
            self.assertEqual(aws.deploy(self.session, self.args), STATE)
        self.cfn.create_stack.assert_called_once()
        self.cfn.update_stack.assert_not_called()
        self.cfn.get_waiter.assert_called_once_with("stack_create_complete")
        self.store.assert_called_once()
        self.release.assert_called_once()


class InstanceRecoveryTests(unittest.TestCase):
    def test_paid_plan_cannot_start_or_even_inspect_a_recovery_instance(self):
        session = FakeSession(active(accountPlanType="PAID"))
        with self.assertRaises(aws.Refusal):
            aws.ensure_instance_running(session, "i-test")
        self.assertEqual(session.calls, ["freetier"])

    def test_stopped_host_is_started_only_after_a_fresh_free_plan_check(self):
        events = []
        ec2 = Mock()
        ec2.describe_instances.return_value = {"Reservations": [{"Instances": [{"State": {"Name": "stopped"}}]}]}
        ec2.start_instances.side_effect = lambda **kwargs: events.append("start")
        session = Clients(ec2=ec2)
        with patch.object(aws, "require_free_plan", side_effect=lambda _session: events.append("free-check")):
            aws.ensure_instance_running(session, "i-test")
        self.assertEqual(events, ["free-check", "free-check", "start"])
        ec2.start_instances.assert_called_once_with(InstanceIds=["i-test"])
        self.assertEqual([call.args[0] for call in ec2.get_waiter.call_args_list], ["instance_running", "instance_status_ok"])

    def test_unknown_or_terminated_instance_is_not_replaced(self):
        for state in ("terminated", "shutting-down", "stopping", "unexpected"):
            with self.subTest(state=state):
                ec2 = Mock()
                ec2.describe_instances.return_value = {"Reservations": [{"Instances": [{"State": {"Name": state}}]}]}
                with patch.object(aws, "require_free_plan"):
                    with self.assertRaises(aws.Refusal):
                        aws.ensure_instance_running(Clients(ec2=ec2), "i-test")
                ec2.start_instances.assert_not_called()

    def test_eligibility_loss_during_inspection_prevents_start(self):
        ec2 = Mock()
        ec2.describe_instances.return_value = {"Reservations": [{"Instances": [{"State": {"Name": "stopped"}}]}]}
        with patch.object(aws, "require_free_plan", side_effect=[{"eligible": True}, aws.Refusal("Free eligibility changed")]):
            with self.assertRaisesRegex(aws.Refusal, "eligibility changed"):
                aws.ensure_instance_running(Clients(ec2=ec2), "i-test")
        ec2.start_instances.assert_not_called()


class BackupEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stova-backup-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.dump = self.folder / "stova-test.dump"
        # A local custom-format marker fixture; production evidence is written
        # only after the remote pg_dump command has completed successfully.
        self.content = b"PGDMP" + bytes(range(128))
        self.dump.write_bytes(self.content)
        self.stack = stack_fixture()

    def evidence(self):
        return aws.write_backup_evidence(self.dump, self.stack)

    def test_evidence_matches_stack_identity_checksum_and_file_size(self):
        sidecar = self.evidence()
        path, evidence = aws.read_backup_evidence(str(self.dump), "stova")
        self.assertEqual(path, self.dump.resolve())
        self.assertEqual(evidence["stackId"], self.stack["StackId"])
        self.assertEqual(evidence["sizeBytes"], len(self.content))
        self.assertEqual(evidence["sha256"], aws.dump_fingerprint(self.dump)["sha256"])
        self.assertEqual(sidecar, self.dump.with_name(self.dump.name + ".json"))

    def test_dump_content_and_size_tampering_are_rejected(self):
        self.evidence()
        self.dump.write_bytes(self.content[:-1] + b"changed")
        with self.assertRaisesRegex(aws.Refusal, "size or checksum"):
            aws.read_backup_evidence(str(self.dump), "stova")
        self.dump.write_bytes(self.content[:-1] + b"x")
        with self.assertRaisesRegex(aws.Refusal, "size or checksum"):
            aws.read_backup_evidence(str(self.dump), "stova")

    def test_missing_invalid_or_wrong_stack_evidence_is_rejected(self):
        with self.assertRaises(aws.Refusal):
            aws.read_backup_evidence(str(self.dump), "stova")
        sidecar = self.evidence()
        with self.assertRaisesRegex(aws.Refusal, "different stack"):
            aws.read_backup_evidence(str(self.dump), "another-stack")
        sidecar.write_text("not json")
        with self.assertRaises(aws.Refusal):
            aws.read_backup_evidence(str(self.dump), "stova")

    def test_non_pg_dump_files_do_not_receive_evidence(self):
        self.dump.write_bytes(b"ordinary non-database content" * 3)
        with self.assertRaisesRegex(aws.Refusal, "custom-format"):
            self.evidence()
        self.assertFalse(aws.backup_sidecar(self.dump).exists())

    def test_backup_freshness_is_left_for_explicit_owner_review(self):
        sidecar = self.evidence()
        metadata = json.loads(sidecar.read_text())
        metadata["createdAt"] = "2000-01-01T00:00:00+00:00"
        sidecar.write_text(json.dumps(metadata))
        _, evidence = aws.read_backup_evidence(str(self.dump), "stova")
        self.assertEqual(evidence["createdAt"], metadata["createdAt"])

    def test_fresh_backup_writes_evidence_after_successful_remote_export(self):
        s3 = Mock()
        s3.download_file.side_effect = lambda _bucket, _key, target: Path(target).write_bytes(self.content)
        args = aws.parser().parse_args(["backup", "--backup-dir", str(self.folder)])
        with patch.object(aws, "stack_details", return_value=self.stack), patch.object(aws, "remote") as remote:
            state, path = aws.backup(Clients(s3=s3), args)
        self.assertEqual(state, STATE)
        remote.assert_called_once()
        self.assertTrue(aws.backup_sidecar(path).is_file())
        _, evidence = aws.read_backup_evidence(str(path), "stova")
        self.assertEqual(evidence["stackId"], self.stack["StackId"])

    def test_offline_destroy_uses_verified_backup_without_contacting_stopped_host(self):
        self.evidence()
        args = aws.parser().parse_args(["destroy", "--backup-file", str(self.dump)])
        s3, cfn, ssm = Mock(), Mock(), Mock()
        s3.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": "releases/old.tar.gz"}]}]
        session = Clients(s3=s3, cloudformation=cfn, ssm=ssm)
        with patch.object(aws, "stack_details", return_value=self.stack), patch.object(aws, "backup", side_effect=AssertionError("A stopped host must not be contacted")), patch.object(aws, "remote", side_effect=AssertionError("No SSM command is permitted")), patch.object(aws, "require_free_plan", side_effect=AssertionError("Teardown must not require paid recovery")), patch.object(aws, "activate_distribution") as activate:
            result = aws.destroy(session, args)
        self.assertTrue(result["deleted"])
        self.assertTrue(result["usedExistingBackup"])
        self.assertTrue(self.dump.exists())
        activate.assert_called_once_with(session, STATE, None, enabled=False)
        cfn.delete_stack.assert_called_once_with(StackName="stova")
        s3.delete_objects.assert_called_once()
        self.assertNotIn("ec2", session.calls)

    def test_recreated_stack_with_same_name_refuses_before_any_mutation(self):
        self.evidence()
        args = aws.parser().parse_args(["destroy", "--backup-file", str(self.dump)])
        current = deepcopy(self.stack)
        current["StackId"] = current["StackId"].replace("identity-one", "identity-two")
        session = Clients()
        with patch.object(aws, "stack_details", return_value=current), patch.object(aws, "activate_distribution") as activate, patch.object(aws, "remote") as remote:
            with self.assertRaisesRegex(aws.Refusal, "different stack identity"):
                aws.destroy(session, args)
        activate.assert_not_called()
        remote.assert_not_called()
        self.assertEqual(session.calls, [])

    def test_corrupt_backup_is_refused_before_any_aws_client_or_mutation(self):
        self.evidence()
        self.dump.write_bytes(self.content + b"corruption")
        args = aws.parser().parse_args(["destroy", "--backup-file", str(self.dump)])
        session = Clients()
        with patch.object(aws, "stack_details") as describe, patch.object(aws, "activate_distribution") as activate:
            with self.assertRaises(aws.Refusal):
                aws.destroy(session, args)
        describe.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(session.calls, [])

    def test_destroy_without_backup_file_still_requires_fresh_live_backup(self):
        args = aws.parser().parse_args(["destroy", "--backup-dir", str(self.folder)])
        with patch.object(aws, "backup", side_effect=aws.Refusal("Live database unavailable")) as backup, patch.object(aws, "activate_distribution") as activate:
            with self.assertRaisesRegex(aws.Refusal, "Live database unavailable"):
                aws.destroy(Clients(), args)
        backup.assert_called_once()
        activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
