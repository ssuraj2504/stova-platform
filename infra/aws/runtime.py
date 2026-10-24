"""Host-side release loader. No secrets are accepted in CLI arguments or output."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import boto3

ROOT = Path("/opt/stova")


def free_plan(session):
    state = session.client("freetier", region_name="us-east-1").get_account_plan_state()
    if not isinstance(state, dict):
        raise RuntimeError("The AWS Free account plan could not be verified.")
    expiry = state.get("accountPlanExpirationDate")
    if isinstance(expiry, datetime) and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    credits = state.get("accountPlanRemainingCredits", {})
    if not isinstance(credits, dict):
        raise RuntimeError("The AWS Free account plan could not be verified.")
    try:
        amount = Decimal(str(credits.get("amount", "NaN")))
    except (InvalidOperation, ValueError, TypeError):
        raise RuntimeError("The AWS Free account plan could not be verified.") from None
    if not (state.get("accountPlanType") == "FREE" and state.get("accountPlanStatus") == "ACTIVE" and credits.get("unit") == "USD" and amount.is_finite() and amount > 0 and isinstance(expiry, datetime) and expiry > datetime.now(timezone.utc)):
        raise RuntimeError("The AWS Free account plan could not be verified.")


def command(*args, capture=False):
    return subprocess.run(args, check=True, cwd=ROOT, capture_output=capture, text=capture)


def compose(*args, capture=False):
    return command("docker", "compose", "--env-file", str(ROOT / ".runtime.env"), "-f", str(ROOT / "compose.aws.yaml"), *args, capture=capture)


def settings(session, stack):
    result = session.client("ssm").get_parameter(Name=f"/stova/{stack}/runtime", WithDecryption=True)
    values = json.loads(result["Parameter"]["Value"])
    required = {"POSTGRES_PASSWORD", "JWT_SECRET", "ORIGIN_VERIFY_SECRET", "ALLOWED_ORIGINS"}
    if not required <= values.keys():
        raise RuntimeError("Runtime configuration is incomplete.")
    permitted = required | {"CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_AI_FREE_TIER_CONFIRMED"}
    lines = []
    for key in sorted(permitted):
        value = str(values.get(key, ""))
        if any(character in value for character in ("\r", "\n", "'")):
            raise RuntimeError("Runtime configuration contains an unsupported value.")
        # Single-quoted Compose dotenv entries do not interpolate dollars.
        lines.append(f"{key}='{value}'")
    path = ROOT / ".runtime.env"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def install_guard(region, stack):
    # This is a secondary fail-closed workload guard, not a billing hard cap.
    service = f"[Unit]\nDescription=Check Stova AWS Free account eligibility\n[Service]\nType=oneshot\nExecStart=/opt/stova/tools/bin/python /opt/stova/runtime.py guard --region {region} --stack {stack}\n"
    timer = "[Unit]\nDescription=Check Stova account plan every five minutes\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec=5min\n[Install]\nWantedBy=timers.target\n"
    Path("/etc/systemd/system/stova-free-plan.service").write_text(service)
    Path("/etc/systemd/system/stova-free-plan.timer").write_text(timer)
    command("systemctl", "daemon-reload")
    command("systemctl", "enable", "--now", "stova-free-plan.timer")


def deploy(session, args):
    free_plan(session)
    install_guard(args.region, args.stack)
    settings(session, args.stack)
    s3 = session.client("s3")
    target = ROOT / "releases" / f"{args.release_id}.tar.gz"
    s3.download_file(args.bucket, args.key, str(target))
    with target.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != args.sha256:
        raise RuntimeError("Release checksum did not match.")
    # Keep database volume intact. Tag changes are activated only after image load.
    with target.open("rb") as archive:
        result = subprocess.run(["docker", "load"], stdin=archive, cwd=ROOT, check=True, capture_output=True)
    compose("up", "--detach", "--remove-orphans", "--wait", "--wait-timeout", "240")
    (ROOT / "current-release.json").write_text(json.dumps({"releaseId": args.release_id, "key": args.key, "sha256": args.sha256}))
    for old in sorted((ROOT / "releases").glob("*.tar.gz"), key=lambda file: file.stat().st_mtime, reverse=True)[2:]:
        old.unlink()
    command("docker", "image", "prune", "--force")
    print("Release is healthy.")


def backup(session, args):
    destination = ROOT / "backup.dump"
    with destination.open("wb") as handle:
        subprocess.run(["docker", "compose", "--env-file", str(ROOT / ".runtime.env"), "-f", str(ROOT / "compose.aws.yaml"), "exec", "-T", "db", "pg_dump", "-U", "stova", "-d", "stova", "-Fc"], stdout=handle, check=True, cwd=ROOT)
    os.chmod(destination, 0o600)
    session.client("s3").upload_file(str(destination), args.bucket, args.key, ExtraArgs={"ServerSideEncryption": "AES256"})
    destination.unlink()
    print("Database backup uploaded.")


def main(argv=None, session=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["deploy", "guard", "backup"])
    parser.add_argument("--region", required=True)
    parser.add_argument("--stack", required=True)
    parser.add_argument("--bucket")
    parser.add_argument("--key")
    parser.add_argument("--sha256")
    parser.add_argument("--release-id")
    args = parser.parse_args(argv)
    try:
        session = session or boto3.Session(region_name=args.region)
        if args.action == "guard":
            try:
                free_plan(session)
            except Exception:
                try:
                    if (ROOT / "compose.aws.yaml").exists():
                        compose("down")
                except Exception:
                    print("Containers could not stop gracefully; requesting instance shutdown.", file=sys.stderr)
                finally:
                    # Stop preserves the encrypted database volume. A Compose or
                    # Docker failure must never bypass the instance shutdown.
                    command("shutdown", "-h", "now")
                return 2
        elif args.action == "backup":
            backup(session, args)
        else:
            deploy(session, args)
    except Exception as error:
        # Provider exceptions may contain request payloads; output only their type.
        print(f"Runtime operation failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
