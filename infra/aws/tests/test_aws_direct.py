"""Direct HTTPS checks with fake SDK/process/DNS/TLS operations only."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[3]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


runtime = module("stova_direct_runtime", ROOT / "infra/aws/runtime.py")
aws = module("stova_direct_deployer", ROOT / "scripts/aws_deploy.py")
STATE = {"HostingMode": "direct", "InstanceId": "i-offline", "ReleaseBucket": "private-offline", "PublicAddress": "8.8.8.8", "Hostname": "stova.8.8.8.8.sslip.io", "Url": "https://stova.8.8.8.8.sslip.io"}
VALUES = {"HOSTING_MODE": "direct", "PUBLIC_HOSTNAME": STATE["Hostname"], "WEB_BIND": "127.0.0.1", "ALLOWED_ORIGINS": STATE["Url"], "ORIGIN_VERIFY_SECRET": "a" * 64}


class DirectOwnerTests(unittest.TestCase):
    def test_default_remains_cloudfront_and_direct_is_explicit(self):
        self.assertEqual(aws.parser().parse_args(["deploy"]).hosting, "cloudfront")
        self.assertEqual(aws.parser().parse_args(["deploy", "--hosting", "direct"]).hosting, "direct")

    def test_direct_deployment_does_not_call_cloudfront_or_resolve_its_prefix(self):
        stack = {"StackId": "offline-stack", "StackStatus": "CREATE_COMPLETE", "Parameters": [{"ParameterKey": "LatestAmiId", "ParameterValue": "ami-offline"}, {"ParameterKey": "ExistingGitHubOidcArn", "ParameterValue": ""}], "Outputs": [{"OutputKey": key, "OutputValue": value} for key, value in STATE.items()]}
        args = aws.parser().parse_args(["deploy", "--hosting", "direct", "--defer-activation"])
        session = Mock()
        with ExitStack() as context:
            for name, value in {"require_free_plan": {"eligible": True}, "validate_release_inputs": Path("offline.tar.gz"), "stack_details": stack, "require_unchanged_infrastructure": None, "read_ignored_env": {}, "runtime_values": {"ORIGIN_VERIFY_SECRET": "a" * 64}, "ensure_instance_running": None, "release": STATE}.items():
                context.enter_context(patch.object(aws, name, return_value=value))
            context.enter_context(patch.object(aws, "prefix_list", side_effect=AssertionError("CloudFront prefix lookup must not run")))
            context.enter_context(patch.object(aws, "activate_distribution", side_effect=AssertionError("CloudFront must not run")))
            store = context.enter_context(patch.object(aws, "store_runtime"))
            direct_activate = context.enter_context(patch.object(aws, "activate_direct_host"))
            result = aws.deploy(session, args)
        self.assertTrue(result["activationDeferred"])
        direct_activate.assert_not_called()
        values = store.call_args.args[2]
        self.assertEqual(values["WEB_BIND"], "127.0.0.1")
        self.assertEqual(values["HOSTING_MODE"], "direct")
        self.assertEqual(values["PUBLIC_HOSTNAME"], STATE["Hostname"])
        self.assertEqual([call.args[0] for call in session.client.call_args_list], ["cloudformation"])

    def test_direct_activation_keeps_secret_reads_on_host_and_requires_import(self):
        stack = {"Outputs": [{"OutputKey": key, "OutputValue": value} for key, value in STATE.items()]}
        session = Mock()
        session.client.side_effect = AssertionError("No owner CloudFront/secret client is needed")
        with patch.object(aws, "require_free_plan") as gate, patch.object(aws, "stack_details", return_value=stack), patch.object(aws, "remote") as remote:
            result = aws.activate(session, aws.parser().parse_args(["activate"]))
        self.assertTrue(result["activated"])
        self.assertEqual(gate.call_count, 2)
        command = " ".join(remote.call_args.args[2])
        self.assertIn("--require-import", command)
        self.assertIn("runtime.py activate", command)
        self.assertNotIn("a" * 64, command)

    def test_output_hostname_and_bind_are_owner_derived_and_strict(self):
        values = {}
        aws.set_hosting_values(values, STATE)
        self.assertEqual(values["WEB_BIND"], "127.0.0.1")
        for changes in [{"PublicAddress": "127.0.0.1"}, {"Hostname": "attacker.example"}, {"Url": "http://" + STATE["Hostname"]}, {"HostingMode": "unknown"}]:
            with self.subTest(changes=changes), self.assertRaises(aws.Refusal):
                aws.set_hosting_values({}, {**STATE, **changes})

    def test_direct_teardown_never_calls_cloudfront_or_requires_paid_recovery(self):
        stack = {"StackId": "offline-stack-id", "Outputs": [{"OutputKey": key, "OutputValue": value} for key, value in STATE.items()]}
        s3, cloudformation, ssm = Mock(), Mock(), Mock()
        s3.get_paginator.return_value.paginate.return_value = []
        session = Mock()
        session.client.side_effect = lambda name: {"s3": s3, "cloudformation": cloudformation, "ssm": ssm}[name]
        args = aws.parser().parse_args(["destroy", "--backup-file", "offline.dump"])
        with patch.object(aws, "read_backup_evidence", return_value=(Path("offline.dump"), {"stackId": "offline-stack-id"})), patch.object(aws, "stack_details", return_value=stack), patch.object(aws, "require_free_plan", side_effect=AssertionError("Cleanup must not resume paid workloads")), patch.object(aws, "activate_distribution", side_effect=AssertionError("CloudFront must not run")):
            self.assertTrue(aws.destroy(session, args)["deleted"])
        cloudformation.delete_stack.assert_called_once_with(StackName="stova")


class DirectRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stova-direct-offline-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "initial-import-receipt.json").write_text("{}")
        self.args = SimpleNamespace(stack="stova", require_import=True)
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(runtime, "ROOT", self.root))
        self.gate = self.context.enter_context(patch.object(runtime, "free_plan"))
        self.context.enter_context(patch.object(runtime, "settings", return_value=dict(VALUES)))
        self.process = self.context.enter_context(patch.object(runtime, "command"))
        self.local_ready = self.context.enter_context(patch.object(runtime, "private_compose"))
        self.dns = self.context.enter_context(patch.object(runtime, "verify_direct_dns"))
        self.configure = self.context.enter_context(patch.object(runtime, "configure_direct_tls"))
        self.tls = self.context.enter_context(patch.object(runtime, "trusted_tls_ready"))

    def invoke(self):
        with redirect_stdout(io.StringIO()):
            runtime.activate_direct(Mock(), self.args)

    def test_activation_checks_dns_before_issuance_and_trusted_tls_after_start(self):
        events = []
        self.dns.side_effect = lambda *args: events.append("dns")
        self.configure.side_effect = lambda *args: events.append("configure")
        self.process.side_effect = lambda *args, **kwargs: events.append("start")
        self.tls.side_effect = lambda *args: events.append("trusted_tls")
        self.invoke()
        self.assertEqual(events, ["dns", "configure", "start", "trusted_tls"])
        self.assertEqual(self.gate.call_count, 2)
        self.process.assert_called_once_with("systemctl", "enable", "--now", "stova-caddy", capture=True)
        self.local_ready.assert_called_once()

    def test_ineligible_activation_does_not_read_settings_or_start_tls(self):
        self.gate.side_effect = RuntimeError("Not a verified Free plan")
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.local_ready.assert_not_called()
        self.dns.assert_not_called()
        self.configure.assert_not_called()
        self.process.assert_not_called()

    def test_missing_import_receipt_and_bad_dns_never_start_caddy(self):
        (self.root / "initial-import-receipt.json").unlink()
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.process.assert_not_called()
        (self.root / "initial-import-receipt.json").write_text("{}")
        self.dns.side_effect = OSError("No matching DNS answer")
        with self.assertRaises(runtime.RuntimeRefusal):
            self.invoke()
        self.configure.assert_not_called()
        self.process.assert_not_called()

    def test_untrusted_or_unready_tls_disables_service_without_fallback(self):
        self.tls.side_effect = OSError("Untrusted test certificate")
        with patch.object(runtime, "TLS_READY_SECONDS", 0), self.assertRaises(runtime.RuntimeRefusal):
            self.invoke()
        self.assertEqual([call.args for call in self.process.call_args_list], [("systemctl", "enable", "--now", "stova-caddy"), ("systemctl", "disable", "--now", "stova-caddy")])

    def test_config_has_no_literal_secret_and_preserves_request_cancellation(self):
        config = runtime.render_caddyfile()
        self.assertIn("admin off", config)
        self.assertIn("https://acme-v02.api.letsencrypt.org/directory", config)
        self.assertIn("127.0.0.1:8080", config)
        self.assertIn("X-Stova-Client-IP {remote_host}", config)
        self.assertIn("X-Stova-Origin {env.ORIGIN_VERIFY_SECRET}", config)
        self.assertIn("flush_interval 1ms", config)
        self.assertNotIn("flush_interval -1", config)
        self.assertNotIn("a" * 64, config)
        self.assertNotIn("zerossl", config.lower())
        self.assertNotIn("tls internal", config)

    def test_hosting_rejects_nonpublic_ip_nonloopback_bind_and_wrong_origin(self):
        self.assertEqual(runtime.hosting_values(VALUES), ("direct", STATE["Hostname"], "8.8.8.8"))
        for changes in [{"PUBLIC_HOSTNAME": "stova.127.0.0.1.sslip.io"}, {"PUBLIC_HOSTNAME": "stova.999.8.8.8.sslip.io"}, {"WEB_BIND": "0.0.0.0"}, {"ALLOWED_ORIGINS": "https://wrong.example"}]:
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                runtime.hosting_values({**VALUES, **changes})


class DirectDNSAndReleaseTests(unittest.TestCase):
    def test_dns_checks_local_and_two_independent_a_and_aaaa_answers(self):
        def response(request, **kwargs):
            data = {"Status": 0, "Answer": [{"type": 1, "data": "8.8.8.8"}]} if "type=A" in request.full_url and "type=AAAA" not in request.full_url else {"Status": 0, "Answer": []}
            result = Mock()
            result.__enter__ = Mock(return_value=result)
            result.__exit__ = Mock(return_value=False)
            result.read.return_value = json.dumps(data).encode()
            return result
        with patch.object(runtime.socket, "getaddrinfo", return_value=[(None, None, None, None, ("8.8.8.8", 443))]), patch.object(runtime.urllib.request, "urlopen", side_effect=response) as query:
            runtime.verify_direct_dns(STATE["Hostname"], "8.8.8.8")
        urls = [call.args[0].full_url for call in query.call_args_list]
        self.assertEqual(len(urls), 4)
        self.assertTrue(any("cloudflare-dns.com" in url for url in urls))
        self.assertTrue(any("dns.google" in url for url in urls))

    def test_mismatched_local_dns_never_contacts_external_resolver_or_ca(self):
        with patch.object(runtime.socket, "getaddrinfo", return_value=[(None, None, None, None, ("1.1.1.1", 443))]), patch.object(runtime.urllib.request, "urlopen") as query, self.assertRaises(RuntimeError):
            runtime.verify_direct_dns(STATE["Hostname"], "8.8.8.8")
        query.assert_not_called()

    def test_release_preserves_active_caddy_without_starting_an_inactive_service(self):
        for active in (False, True):
            with self.subTest(active=active), tempfile.TemporaryDirectory() as directory, ExitStack() as context:
                root = Path(directory)
                (root / "releases").mkdir()
                archive = b"OFFLINE SYNTHETIC IMAGE ARCHIVE"
                session = Mock()
                session.client.return_value.download_file.side_effect = lambda bucket, key, target: Path(target).write_bytes(archive)
                args = SimpleNamespace(region="us-east-1", stack="stova", release_id="test", bucket="offline", key="releases/test", sha256=hashlib.sha256(archive).hexdigest())
                context.enter_context(patch.object(runtime, "ROOT", root))
                context.enter_context(patch.object(runtime, "free_plan"))
                context.enter_context(patch.object(runtime, "install_guard"))
                context.enter_context(patch.object(runtime, "settings", return_value=dict(VALUES)))
                context.enter_context(patch.object(runtime, "caddy_active", return_value=active))
                context.enter_context(patch.object(runtime.subprocess, "run"))
                context.enter_context(patch.object(runtime, "compose"))
                config = context.enter_context(patch.object(runtime, "configure_direct_tls"))
                command = context.enter_context(patch.object(runtime, "command"))
                with redirect_stdout(io.StringIO()):
                    runtime.deploy(session, args)
                self.assertEqual(config.call_count, int(active))
                calls = [call.args for call in command.call_args_list]
                self.assertNotIn(("systemctl", "enable", "--now", "stova-caddy"), calls)
                self.assertEqual(("systemctl", "restart", "stova-caddy") in calls, active)

    def test_caddy_and_compose_stop_failures_cannot_bypass_guard_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.aws.yaml").touch()
            def command(*args, **kwargs):
                if args[0] == "systemctl":
                    raise subprocess.CalledProcessError(1, args)
            with patch.object(runtime, "ROOT", root), patch.object(runtime.Path, "is_file", return_value=True), patch.object(runtime, "free_plan", side_effect=RuntimeError("Unverified plan")), patch.object(runtime, "compose", side_effect=RuntimeError("Docker failed")), patch.object(runtime, "command", side_effect=command) as invoke:
                with redirect_stderr(io.StringIO()):
                    result = runtime.main(["guard", "--region", "us-east-1", "--stack", "stova"], session=Mock())
            self.assertEqual(result, 2)
            self.assertEqual(invoke.call_args.args, ("shutdown", "-h", "now"))


if __name__ == "__main__":
    unittest.main()
