"""Read-only AWS plan by default; credit-gated deployment is always explicit.

No AWS calls occur when this module is imported. No secret values are printed.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import time
import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
AWS = ROOT / "infra" / "aws"
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"


class Refusal(RuntimeError):
    pass


def assess_plan(state, now=None):
    """Parse only the documented GetAccountPlanState fields; unknown fails closed."""
    state = state if isinstance(state, dict) else {}
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    expiry = state.get("accountPlanExpirationDate")
    if isinstance(expiry, datetime) and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    credit = state.get("accountPlanRemainingCredits") or {}
    credit = credit if isinstance(credit, dict) else {}
    try:
        amount = Decimal(str(credit.get("amount", "NaN")))
    except (InvalidOperation, ValueError, TypeError):
        amount = Decimal("NaN")
    reasons = []
    if state.get("accountPlanType") != "FREE":
        reasons.append("The account is not documented as an AWS Free account plan.")
    if state.get("accountPlanStatus") != "ACTIVE":
        reasons.append("The Free account plan is not active.")
    if credit.get("unit") != "USD" or not amount.is_finite() or amount <= 0:
        reasons.append("Positive remaining USD credits could not be verified.")
    if not isinstance(expiry, datetime) or expiry <= now:
        reasons.append("A future Free account expiration could not be verified.")
    return {"eligible": not reasons, "accountPlanType": state.get("accountPlanType", "UNKNOWN"), "accountPlanStatus": state.get("accountPlanStatus", "UNKNOWN"), "remainingCreditsUSD": str(amount) if amount.is_finite() and credit.get("unit") == "USD" else None, "expiresAt": expiry.isoformat() if isinstance(expiry, datetime) else None, "reasons": reasons, "notice": "Credit-backed and time-limited. This is not a permanent-free deployment or a billing hard cap. Do not upgrade this account to a paid plan while these resources remain."}


def account_plan(session):
    try:
        return assess_plan(session.client("freetier", region_name="us-east-1").get_account_plan_state())
    except Exception as error:
        code = error.response.get("Error", {}).get("Code", "Unavailable") if isinstance(error, ClientError) else type(error).__name__
        return {"eligible": False, "reasons": [f"AWS Free account eligibility could not be verified ({code})."], "notice": "No deployment is permitted while eligibility is unknown."}


def require_free_plan(session):
    plan = account_plan(session)
    if not plan["eligible"]:
        raise Refusal(" ".join(plan["reasons"]))
    return plan


def stack_details(session, name):
    try:
        return session.client("cloudformation").describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as error:
        if error.response["Error"]["Code"] == "ValidationError" and "does not exist" in error.response["Error"].get("Message", ""):
            return None
        raise


def outputs(stack):
    if not stack:
        raise Refusal("The stack does not exist. Run the explicit initial deploy first.")
    return {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}


def validate_stack(name):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,31}", name):
        raise Refusal("Use a stack name of 1–32 letters, digits, or hyphens, starting with a letter.")


def canonical_template(value):
    """Compare JSON templates structurally, ignoring serialization whitespace."""
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        if not isinstance(parsed, dict) or not isinstance(parsed.get("Resources"), dict) or not parsed["Resources"]:
            raise ValueError("Missing resources")
        return json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise Refusal("The existing infrastructure template could not be verified. Back up the database and review infrastructure changes separately.") from None


def require_unchanged_infrastructure(session, stack, requested_template, requested_parameters):
    """Runtime deployment must never turn into an infrastructure replacement."""
    current = session.client("cloudformation").get_template(StackName=stack["StackId"], TemplateStage="Original")
    old_parameters = {item["ParameterKey"]: item.get("ParameterValue", "") for item in stack.get("Parameters", [])}
    new_parameters = {item["ParameterKey"]: item.get("ParameterValue", "") for item in requested_parameters}
    if canonical_template(current.get("TemplateBody")) != canonical_template(requested_template) or old_parameters != new_parameters:
        raise Refusal("Infrastructure differs from the existing stack. No automatic update or replacement is permitted. Download and review a current database backup, then review the infrastructure change and data migration separately. Use release for application-only changes.")


def ensure_instance_running(session, instance_id):
    """Owner deploy may resume a stopped host, but never on paid/unknown plans."""
    require_free_plan(session)
    ec2 = session.client("ec2")
    reservations = ec2.describe_instances(InstanceIds=[instance_id]).get("Reservations", [])
    instances = [item for reservation in reservations for item in reservation.get("Instances", [])]
    if len(instances) != 1:
        raise Refusal("The existing instance could not be verified. No replacement was attempted; review the database backup and infrastructure separately.")
    state = instances[0].get("State", {}).get("Name")
    if state == "running":
        return
    if state == "stopped":
        require_free_plan(session)
        ec2.start_instances(InstanceIds=[instance_id])
    elif state != "pending":
        raise Refusal("The existing instance is not running, stopped, or pending. Wait for it to settle and review its state; no replacement was attempted.")
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    ec2.get_waiter("instance_status_ok").wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 10, "MaxAttempts": 60})


def read_ignored_env(path):
    if not path:
        raise Refusal("Initial/configuration deploy requires an explicit --env-file pointing to an ignored .env.aws.local file.")
    path = Path(path).resolve()
    if not path.name.startswith(".env") or not path.is_file() or path.stat().st_size > 16384:
        raise Refusal("Use a small ignored .env file for owner runtime settings.")
    ignored = subprocess.run(["git", "check-ignore", "--quiet", "--", str(path)], cwd=ROOT, capture_output=True)
    if ignored.returncode != 0:
        raise Refusal("The supplied environment file must be ignored by this Git repository.")
    values = {}
    allowed = {"POSTGRES_PASSWORD", "JWT_SECRET", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_AI_FREE_TIER_CONFIRMED"}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise Refusal("The environment file contains an invalid line.")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in allowed:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if any(character in value for character in ("\r", "\n", "'")):
            raise Refusal("A runtime setting contains an unsupported value.")
        values[key] = value
    return values


def runtime_values(session, stack, explicit):
    ssm = session.client("ssm")
    previous = {}
    try:
        previous = json.loads(ssm.get_parameter(Name=f"/stova/{stack}/runtime", WithDecryption=True)["Parameter"]["Value"])
    except ClientError as error:
        if error.response["Error"]["Code"] != "ParameterNotFound":
            raise
    values = dict(previous)
    for key, value in explicit.items():
        if key == "POSTGRES_PASSWORD" and previous.get(key) and value and value != previous[key]:
            raise Refusal("Database password rotation needs a separate database migration; the existing password was preserved.")
        if value or key.startswith("CLOUDFLARE_"):
            values[key] = value
    values.setdefault("POSTGRES_PASSWORD", secrets.token_urlsafe(36))
    values.setdefault("JWT_SECRET", secrets.token_urlsafe(48))
    values.setdefault("ORIGIN_VERIFY_SECRET", secrets.token_urlsafe(48))
    for key, minimum in (("POSTGRES_PASSWORD", 20), ("JWT_SECRET", 32), ("ORIGIN_VERIFY_SECRET", 32)):
        if not re.fullmatch(rf"[A-Za-z0-9_-]{{{minimum},128}}", values[key]):
            raise Refusal(f"{key} must use URL-safe characters and meet the documented minimum length.")
    values.setdefault("CLOUDFLARE_ACCOUNT_ID", "")
    values.setdefault("CLOUDFLARE_API_TOKEN", "")
    values.setdefault("CLOUDFLARE_AI_FREE_TIER_CONFIRMED", "false")
    if values["CLOUDFLARE_AI_FREE_TIER_CONFIRMED"] == "true" and not (re.fullmatch(r"[a-fA-F0-9]{32}", values["CLOUDFLARE_ACCOUNT_ID"]) and re.fullmatch(r"[A-Za-z0-9_-]{20,2048}", values["CLOUDFLARE_API_TOKEN"])):
        raise Refusal("Cloudflare Free confirmation requires a valid account ID and API token, or leave the managed connection unconfigured.")
    return values


def store_runtime(session, stack, values):
    payload = json.dumps(values, separators=(",", ":"))
    if len(payload.encode()) > 4096:
        raise Refusal("Runtime settings exceed the Standard SSM parameter limit.")
    session.client("ssm").put_parameter(Name=f"/stova/{stack}/runtime", Description="Stova runtime secrets; do not print", Value=payload, Type="SecureString", Tier="Standard", Overwrite=True)


def prefix_list(session):
    result = session.client("ec2").describe_managed_prefix_lists(Filters=[{"Name": "prefix-list-name", "Values": ["com.amazonaws.global.cloudfront.origin-facing"]}])
    lists = result.get("PrefixLists", [])
    if len(lists) != 1:
        raise Refusal("The CloudFront origin-facing prefix list could not be resolved in this region.")
    return lists[0]["PrefixListId"]


def github_provider(session, repo):
    if not repo:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise Refusal("GitHub repository must be owner/name.")
    matches = [item["Arn"] for item in session.client("iam").list_open_id_connect_providers().get("OpenIDConnectProviderList", []) if item["Arn"].endswith("/token.actions.githubusercontent.com")]
    return matches[0] if matches else ""


def upload_assets(session, bucket):
    for filename in ("runtime.py", "compose.aws.yaml"):
        session.client("s3").upload_file(str(AWS / filename), bucket, f"bootstrap/{filename}", ExtraArgs={"ServerSideEncryption": "AES256"})


def validate_release_inputs(args):
    archive = Path(args.archive).resolve() if args.archive else None
    if not archive or not archive.is_file() or archive.stat().st_size == 0:
        raise Refusal("Supply --archive with a non-empty gzip-compressed docker-save release containing all three images.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.release_id):
        raise Refusal("Use a simple release ID, such as a Git commit SHA.")
    return archive


def remote(session, instance, commands, comment):
    ssm = session.client("ssm")
    response = ssm.send_command(InstanceIds=[instance], DocumentName="AWS-RunShellScript", Comment=comment, TimeoutSeconds=600, Parameters={"commands": commands, "executionTimeout": ["1800"]})
    command_id = response["Command"]["CommandId"]
    for _ in range(360):
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance)
            if result["Status"] == "Success":
                return
            if result["Status"] in ("Cancelled", "TimedOut", "Failed", "Cancelling"):
                raise Refusal(f"Remote operation did not succeed ({result['Status']}); inspect Systems Manager locally. Secret-bearing output was not printed.")
        except ClientError as error:
            if error.response["Error"]["Code"] != "InvocationDoesNotExist":
                raise
        time.sleep(5)
    raise Refusal("Remote operation timed out; inspect Systems Manager before retrying.")


def release(session, args, gate=True):
    if gate:
        require_free_plan(session)
    validate_stack(args.stack)
    archive = validate_release_inputs(args)
    state = outputs(stack_details(session, args.stack))
    upload_assets(session, state["ReleaseBucket"])
    key = f"releases/{args.release_id}/images.tar.gz"
    with archive.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    session.client("s3").upload_file(str(archive), state["ReleaseBucket"], key, ExtraArgs={"ServerSideEncryption": "AES256"})
    # All command arguments are public identifiers/checksums. Runtime secrets are
    # fetched directly on the instance from the scoped encrypted SSM parameter.
    download = "import boto3; s=boto3.client('s3',region_name=" + repr(args.region) + "); " + "; ".join(f"s.download_file({state['ReleaseBucket']!r}, 'bootstrap/{filename}', '/opt/stova/{filename}')" for filename in ("runtime.py", "compose.aws.yaml"))
    cmd = " ".join(shlex.quote(value) for value in ("/opt/stova/tools/bin/python", "/opt/stova/runtime.py", "deploy", "--region", args.region, "--stack", args.stack, "--bucket", state["ReleaseBucket"], "--key", key, "--sha256", digest, "--release-id", args.release_id))
    remote(session, state["InstanceId"], ["set -eu", "for n in $(seq 1 120); do test -f /opt/stova/bootstrap-ready && break; sleep 5; done", "test -f /opt/stova/bootstrap-ready", "/opt/stova/tools/bin/python -c " + shlex.quote(download), cmd], f"Stova release {args.release_id}")
    return state


def activate_distribution(session, state, secret, enabled=True):
    client = session.client("cloudfront")
    result = client.get_distribution_config(Id=state["DistributionId"])
    config = result["DistributionConfig"]
    config["Enabled"] = enabled
    if enabled:
        instance = session.client("ec2").describe_instances(InstanceIds=[state["InstanceId"]])["Reservations"][0]["Instances"][0]
        hostname = instance.get("PublicDnsName")
        if not hostname:
            raise Refusal("The instance has no public origin hostname. Start it and wait for readiness before activation.")
        for origin in config["Origins"]["Items"]:
            if origin["Id"] == "stova-origin":
                origin["DomainName"] = hostname
    if secret:
        # The origin verification secret is sent to CloudFront through the SDK,
        # never embedded in a template, user data, output, or command line.
        for origin in config["Origins"]["Items"]:
            if origin["Id"] == "stova-origin":
                origin["CustomHeaders"] = {"Quantity": 1, "Items": [{"HeaderName": "X-Stova-Origin", "HeaderValue": secret}]}
    client.update_distribution(Id=state["DistributionId"], IfMatch=result["ETag"], DistributionConfig=config)
    client.get_waiter("distribution_deployed").wait(Id=state["DistributionId"], WaiterConfig={"Delay": 30, "MaxAttempts": 80})


def deploy(session, args):
    # This gate must precede environment-file reads and every mutating SDK call.
    require_free_plan(session)
    validate_stack(args.stack)
    validate_release_inputs(args)
    existing = stack_details(session, args.stack)
    if existing and existing["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"):
        raise Refusal("The existing stack is not ready for runtime deployment. Inspect CloudFormation before continuing; no infrastructure update was attempted.")
    ami = next((item["ParameterValue"] for item in existing.get("Parameters", []) if item["ParameterKey"] == "LatestAmiId"), None) if existing else None
    if existing and not ami:
        raise Refusal("The existing AMI parameter could not be verified. Back up the database and review the infrastructure separately; no replacement was attempted.")
    if not ami:
        ami = session.client("ssm").get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]
    old_parameters = {item["ParameterKey"]: item.get("ParameterValue", "") for item in existing.get("Parameters", [])} if existing else {}
    repo = args.github_repo or old_parameters.get("GitHubRepo", "")
    # If this stack owns the provider, preserve the original empty parameter:
    # treating it as pre-existing would delete the provider on the next update.
    oidc = old_parameters["ExistingGitHubOidcArn"] if "ExistingGitHubOidcArn" in old_parameters else github_provider(session, repo)
    parameters = [{"ParameterKey": "CloudFrontPrefixListId", "ParameterValue": prefix_list(session)}, {"ParameterKey": "LatestAmiId", "ParameterValue": ami}, {"ParameterKey": "GitHubRepo", "ParameterValue": repo}, {"ParameterKey": "ExistingGitHubOidcArn", "ParameterValue": oidc}]
    template = (AWS / "template.json").read_text()
    client = session.client("cloudformation")
    if existing:
        require_unchanged_infrastructure(session, existing, template, parameters)
    else:
        canonical_template(template)
    # Only read runtime secrets once any existing infrastructure has passed the
    # immutable comparison. Existing stacks never call update_stack.
    explicit = read_ignored_env(args.env_file)
    values = runtime_values(session, args.stack, explicit)
    if not existing:
        require_free_plan(session)
        client.create_stack(StackName=args.stack, TemplateBody=template, Parameters=parameters, Capabilities=["CAPABILITY_IAM"], Tags=[{"Key": "Application", "Value": "Stova"}, {"Key": "DeploymentPolicy", "Value": "ActiveFreeAccountCreditsOnly"}])
        client.get_waiter("stack_create_complete").wait(StackName=args.stack, WaiterConfig={"Delay": 15, "MaxAttempts": 240})
    state = outputs(existing or stack_details(session, args.stack))
    require_free_plan(session)
    values["ALLOWED_ORIGINS"] = state["Url"]
    store_runtime(session, args.stack, values)
    ensure_instance_running(session, state["InstanceId"])
    release(session, args, gate=False)
    if not getattr(args, "defer_activation", False):
        require_free_plan(session)
        activate_distribution(session, state, values["ORIGIN_VERIFY_SECRET"])
    else:
        state = {**state, "activationDeferred": True}
    return state


def import_inputs(args):
    """Read only owner-supplied metadata and fingerprint the binary archive."""
    if not args.dump_file or not args.manifest_file:
        raise Refusal("Initial import requires --dump-file and --manifest-file from one consistent snapshot.")
    archive, metadata = Path(args.dump_file).expanduser().resolve(), Path(args.manifest_file).expanduser().resolve()
    if not metadata.is_file() or metadata.stat().st_size > 65536:
        raise Refusal("The initial-import manifest is missing or too large.")
    try:
        spec = importlib.util.spec_from_file_location("stova_import_runtime", AWS / "runtime.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manifest = module.validate_import_manifest(json.loads(metadata.read_text(encoding="utf-8")))
    except Exception:
        raise Refusal("The initial-import manifest could not be validated.") from None
    fingerprint = dump_fingerprint(archive)
    if fingerprint != {"sha256": manifest["sha256"], "sizeBytes": manifest["sizeBytes"]} or manifest.get("archive", archive.name) != archive.name:
        raise Refusal("The source archive does not match its initial-import manifest.")
    with metadata.open("rb") as handle:
        manifest_digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return archive, metadata, manifest, manifest_digest


def initial_import(session, args):
    # No local private file read, upload, runtime replacement, or SSM command is
    # permitted until the account's active Free plan has been checked.
    require_free_plan(session)
    validate_stack(args.stack)
    archive, metadata, manifest, metadata_digest = import_inputs(args)
    stack = stack_details(session, args.stack)
    if not stack or stack.get("StackStatus") not in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"):
        raise Refusal("Deploy a healthy destination stack before the initial database import.")
    state = outputs(stack)
    prefix = "imports/" + secrets.token_hex(16)
    key, manifest_key, runtime_key = prefix + "/source.dump", prefix + "/manifest.json", prefix + "/runtime.py"
    s3 = session.client("s3")
    require_free_plan(session)
    for filename, target in ((archive, key), (metadata, manifest_key), (AWS / "runtime.py", runtime_key)):
        s3.upload_file(str(filename), state["ReleaseBucket"], target, ExtraArgs={"ServerSideEncryption": "AES256"})
    with (AWS / "runtime.py").open("rb") as handle:
        runtime_digest = hashlib.file_digest(handle, "sha256").hexdigest()
    download = ("import boto3,hashlib,os\n"
                "p='/opt/stova/runtime.py.next'\n"
                f"boto3.client('s3',region_name={args.region!r}).download_file({state['ReleaseBucket']!r}, {runtime_key!r}, p)\n"
                f"if hashlib.sha256(open(p,'rb').read()).hexdigest() != {runtime_digest!r}:\n"
                "    raise RuntimeError('Import runtime checksum did not match.')\n"
                "os.replace(p,'/opt/stova/runtime.py')")
    command = " ".join(shlex.quote(value) for value in ("/opt/stova/tools/bin/python", "/opt/stova/runtime.py", "import-db", "--region", args.region, "--stack", args.stack, "--bucket", state["ReleaseBucket"], "--key", key, "--sha256", manifest["sha256"], "--manifest-key", manifest_key, "--manifest-sha256", metadata_digest))
    remote(session, state["InstanceId"], ["set -eu", "test -f /opt/stova/bootstrap-ready", "/opt/stova/tools/bin/python -c " + shlex.quote(download), command], "Stova first empty database import")
    # These objects contain private password hashes, sessions and article data.
    # Delete the transfer copies after verified success; the owner's local dump
    # remains intact. Failed transfers remain private for explicit investigation.
    s3.delete_objects(Bucket=state["ReleaseBucket"], Delete={"Objects": [{"Key": key}, {"Key": manifest_key}, {"Key": runtime_key}], "Quiet": True})
    return {**state, "imported": True, "archiveSha256": manifest["sha256"], "tables": manifest["tables"], "writingReservedNeurons": manifest["writingReservedNeurons"]}


def activate(session, args):
    require_free_plan(session)
    validate_stack(args.stack)
    state = outputs(stack_details(session, args.stack))
    remote(session, state["InstanceId"], ["set -eu", "test -f /opt/stova/initial-import-receipt.json", "docker compose --env-file /opt/stova/.runtime.env -f /opt/stova/compose.aws.yaml exec -T api python -c 'import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:8000/readyz\", timeout=10).read()' >/dev/null"], "Verify initial Stova import readiness")
    payload = session.client("ssm").get_parameter(Name=f"/stova/{args.stack}/runtime", WithDecryption=True)
    values = json.loads(payload["Parameter"]["Value"])
    secret = values.get("ORIGIN_VERIFY_SECRET") if isinstance(values, dict) else None
    if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret):
        raise Refusal("The deployed origin-verification secret could not be verified. No new secret was generated.")
    require_free_plan(session)
    activate_distribution(session, state, secret)
    return {**state, "activated": True}


def backup_sidecar(path):
    return path.with_name(path.name + ".json")


def dump_fingerprint(path):
    """Check the pg_dump custom-format marker and fingerprint the local copy."""
    if not path.is_file() or path.stat().st_size < 32:
        raise Refusal("The database export is missing, empty, or truncated; teardown was not started.")
    with path.open("rb") as handle:
        if handle.read(5) != b"PGDMP":
            raise Refusal("The backup is not a PostgreSQL custom-format dump; teardown was not started.")
        handle.seek(0)
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"sha256": digest, "sizeBytes": path.stat().st_size}


def write_backup_evidence(path, stack):
    stack_id = stack.get("StackId")
    if not isinstance(stack_id, str) or not stack_id:
        raise Refusal("The source stack identity could not be verified; no backup evidence was written.")
    evidence = {"schemaVersion": 1, "format": "pg_dump-custom", "stackId": stack_id,
                "stackName": stack["StackName"], "createdAt": datetime.now(timezone.utc).isoformat(),
                **dump_fingerprint(path)}
    sidecar = backup_sidecar(path)
    with os.fdopen(os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(evidence, handle, indent=2)
        handle.write("\n")
    os.chmod(sidecar, 0o600)
    return sidecar


def read_backup_evidence(filename, stack_name):
    """Validate local integrity before any teardown mutation or host operation."""
    path = Path(filename).expanduser().resolve()
    sidecar = backup_sidecar(path)
    if not sidecar.is_file() or sidecar.stat().st_size > 16384:
        raise Refusal("The backup evidence sidecar is missing or invalid. Use a dump and its matching .dump.json file produced by the backup command.")
    try:
        evidence = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(evidence, dict):
            raise ValueError("Invalid metadata")
        created = datetime.fromisoformat(evidence.get("createdAt", ""))
        if created.tzinfo is None:
            raise ValueError("Missing timestamp timezone")
    except (TypeError, ValueError, OSError):
        raise Refusal("The backup evidence sidecar could not be verified; teardown was not started.") from None
    if evidence.get("schemaVersion") != 1 or evidence.get("format") != "pg_dump-custom" or evidence.get("stackName") != stack_name or not isinstance(evidence.get("stackId"), str) or not evidence["stackId"]:
        raise Refusal("The backup evidence is invalid or belongs to a different stack; teardown was not started.")
    if not isinstance(evidence.get("sizeBytes"), int) or isinstance(evidence["sizeBytes"], bool) or not isinstance(evidence.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", evidence["sha256"]):
        raise Refusal("The backup evidence has an invalid size or checksum; teardown was not started.")
    fingerprint = dump_fingerprint(path)
    if fingerprint["sizeBytes"] != evidence["sizeBytes"] or not secrets.compare_digest(fingerprint["sha256"], evidence["sha256"]):
        raise Refusal("The backup size or checksum no longer matches its evidence; teardown was not started.")
    return path, evidence


def backup(session, args):
    validate_stack(args.stack)
    stack = stack_details(session, args.stack)
    state = outputs(stack)
    directory = Path(args.backup_dir or "").resolve()
    if not args.backup_dir:
        raise Refusal("Backup/teardown requires an explicit --backup-dir for a local database export.")
    directory.mkdir(parents=True, exist_ok=True)
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    key = f"backups/{suffix}.dump"
    command = " ".join(shlex.quote(value) for value in ("/opt/stova/tools/bin/python", "/opt/stova/runtime.py", "backup", "--region", args.region, "--stack", args.stack, "--bucket", state["ReleaseBucket"], "--key", key))
    remote(session, state["InstanceId"], [command], "Stova database backup")
    destination = directory / f"{args.stack}-{suffix}.dump"
    session.client("s3").download_file(state["ReleaseBucket"], key, str(destination))
    os.chmod(destination, 0o600)
    write_backup_evidence(destination, stack)
    return state, destination


def destroy(session, args):
    # Cleanup remains possible if credits expired or an account was upgraded.
    validate_stack(args.stack)
    if getattr(args, "backup_file", None):
        if args.backup_dir:
            raise Refusal("Choose a fresh --backup-dir or an existing --backup-file, not both.")
        destination, evidence = read_backup_evidence(args.backup_file, args.stack)
        stack = stack_details(session, args.stack)
        state = outputs(stack)
        if evidence["stackId"] != stack.get("StackId"):
            raise Refusal("The backup belongs to a different stack identity, possibly an older stack with the same name. Teardown was not started.")
    else:
        state, destination = backup(session, args)
    activate_distribution(session, state, None, enabled=False)
    s3 = session.client("s3")
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=state["ReleaseBucket"]):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=state["ReleaseBucket"], Delete={"Objects": objects, "Quiet": True})
    client = session.client("cloudformation")
    client.delete_stack(StackName=args.stack)
    client.get_waiter("stack_delete_complete").wait(StackName=args.stack, WaiterConfig={"Delay": 15, "MaxAttempts": 240})
    session.client("ssm").delete_parameter(Name=f"/stova/{args.stack}/runtime")
    return {"deleted": True, "backup": str(destination), "evidence": str(backup_sidecar(destination)), "usedExistingBackup": bool(getattr(args, "backup_file", None))}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("action", nargs="?", default="plan", choices=["plan", "deploy", "release", "backup", "destroy", "import-db", "activate"])
    result.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    result.add_argument("--profile")
    result.add_argument("--stack", default="stova")
    result.add_argument("--env-file")
    result.add_argument("--archive")
    result.add_argument("--release-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    result.add_argument("--github-repo", default="")
    result.add_argument("--backup-dir")
    result.add_argument("--backup-file", help="Destroy only: a previously downloaded .dump with its matching integrity/stack-identity .dump.json sidecar. Review backup freshness before use.")
    result.add_argument("--dump-file", help="Initial import only: full PostgreSQL custom archive; destination must be empty.")
    result.add_argument("--manifest-file", help="Initial import only: metadata and checksums for the same consistent snapshot.")
    result.add_argument("--defer-activation", action="store_true", help="Deploy only: keep a new CloudFront distribution disabled until initial import and explicit activate.")
    return result


def main(argv=None, session=None):
    args = parser().parse_args(argv)
    try:
        if args.backup_file and args.action != "destroy":
            raise Refusal("--backup-file is only supported for destroy. The backup command always creates a fresh export.")
        if (args.dump_file or args.manifest_file) and args.action != "import-db":
            raise Refusal("--dump-file and --manifest-file are only supported for import-db.")
        if args.defer_activation and args.action != "deploy":
            raise Refusal("--defer-activation is only supported for deploy.")
        session = session or boto3.Session(profile_name=args.profile, region_name=args.region)
        if args.action == "plan":
            plan = account_plan(session)
            print(json.dumps(plan, indent=2))
            return 0 if plan["eligible"] else 2
        if args.action == "deploy":
            state = deploy(session, args)
        elif args.action == "release":
            state = release(session, args)
        elif args.action == "backup":
            _, destination = backup(session, args)
            state = {"backup": str(destination), "evidence": str(backup_sidecar(destination))}
        elif args.action == "import-db":
            state = initial_import(session, args)
        elif args.action == "activate":
            state = activate(session, args)
        else:
            state = destroy(session, args)
        print(json.dumps(state, indent=2))
        return 0
    except Refusal as error:
        print(f"Refused: {error}", file=sys.stderr)
    except Exception as error:
        code = error.response.get("Error", {}).get("Code", "Unavailable") if isinstance(error, ClientError) else type(error).__name__
        print(f"AWS operation did not complete ({code}). No secret-bearing provider output was printed.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
