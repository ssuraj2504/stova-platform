"""Offline host-guard tests: no AWS, Docker, systemd, or shutdown is invoked."""
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "runtime.py"
spec = importlib.util.spec_from_file_location("stova_aws_runtime", MODULE)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def eligible(**changes):
    state = {"accountPlanType": "FREE", "accountPlanStatus": "ACTIVE", "accountPlanRemainingCredits": {"amount": 10, "unit": "USD"}, "accountPlanExpirationDate": datetime.now(timezone.utc) + timedelta(days=1)}
    state.update(changes)
    return state


class FakeSession:
    def __init__(self, state):
        self.state = state

    def client(self, name, **kwargs):
        if name != "freetier":
            raise AssertionError("No deployment client may be accessed during these guard tests")
        return self

    def get_account_plan_state(self):
        return self.state


class RuntimeGuardTests(unittest.TestCase):
    def test_documented_positive_finite_free_plan_is_accepted(self):
        runtime.free_plan(FakeSession(eligible()))

    def test_nonfinite_or_nonpositive_credit_amounts_fail_closed(self):
        for amount in [float("inf"), float("-inf"), float("nan"), Decimal("Infinity"), Decimal("NaN"), "Infinity", None, 0, -1, "invalid"]:
            with self.subTest(amount=amount):
                with self.assertRaises(RuntimeError):
                    runtime.free_plan(FakeSession(eligible(accountPlanRemainingCredits={"amount": amount, "unit": "USD"})))

    def test_non_dictionary_credits_and_plan_fail_closed(self):
        for credits in [None, "unexpected", [], 100]:
            with self.subTest(credits=credits):
                with self.assertRaises(RuntimeError):
                    runtime.free_plan(FakeSession(eligible(accountPlanRemainingCredits=credits)))
        for state in [None, [], "unexpected"]:
            with self.assertRaises(RuntimeError):
                runtime.free_plan(FakeSession(state))

    def test_compose_failure_never_prevents_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.aws.yaml").touch()
            with patch.object(runtime, "ROOT", root), patch.object(runtime, "compose", side_effect=subprocess.CalledProcessError(1, ["docker", "compose", "down"])) as compose, patch.object(runtime, "command") as command, redirect_stderr(io.StringIO()):
                result = runtime.main(["guard", "--region", "us-east-1", "--stack", "stova"], session=FakeSession(eligible(accountPlanType="PAID")))
            self.assertEqual(result, 2)
            compose.assert_called_once_with("down")
            command.assert_called_once_with("shutdown", "-h", "now")

    def test_shutdown_occurs_without_a_compose_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(runtime, "ROOT", Path(directory)), patch.object(runtime, "compose") as compose, patch.object(runtime, "command") as command:
                result = runtime.main(["guard", "--region", "us-east-1", "--stack", "stova"], session=FakeSession(None))
            self.assertEqual(result, 2)
            compose.assert_not_called()
            command.assert_called_once_with("shutdown", "-h", "now")

    def test_first_release_installs_timer_before_settings_failure(self):
        events = []

        def invoke(*args, **kwargs):
            events.append(args)

        def fail_settings(*args):
            events.append(("settings",))
            raise RuntimeError("First release settings unavailable")

        args = SimpleNamespace(region="us-east-1", stack="stova")
        with patch.object(runtime, "command", side_effect=invoke), patch.object(runtime.Path, "write_text", return_value=0) as writes, patch.object(runtime, "settings", side_effect=fail_settings), patch.object(runtime, "compose") as compose:
            with self.assertRaisesRegex(RuntimeError, "First release settings unavailable"):
                runtime.deploy(FakeSession(eligible()), args)
        self.assertEqual(writes.call_count, 2)
        self.assertEqual(events, [("systemctl", "daemon-reload"), ("systemctl", "enable", "--now", "stova-free-plan.timer"), ("settings",)])
        compose.assert_not_called()

    def test_ineligible_release_installs_nothing(self):
        with patch.object(runtime, "install_guard") as install, patch.object(runtime, "settings") as settings:
            with self.assertRaises(RuntimeError):
                runtime.deploy(FakeSession(eligible(accountPlanStatus="EXPIRED")), SimpleNamespace(region="us-east-1", stack="stova"))
        install.assert_not_called()
        settings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
