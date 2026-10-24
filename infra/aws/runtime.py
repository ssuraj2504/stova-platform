"""Host-side release loader. No secrets are accepted in CLI arguments or output."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import boto3

ROOT = Path("/opt/stova")
IMPORT_TABLES = ("users", "auth_sessions", "auth_throttles", "projects", "articles", "writing_accounts", "writing_reservations")
MAX_IMPORT_BYTES = 512 * 1024 * 1024
CADDY_CONFIG_DIR = Path("/etc/stova-caddy")
TLS_READY_SECONDS = 45


class RuntimeRefusal(RuntimeError):
    """A deliberately public message containing no provider or secret values."""


def render_caddyfile():
    return """{
    admin off
}
{$PUBLIC_HOSTNAME} {
    tls {
        issuer acme https://acme-v02.api.letsencrypt.org/directory {
            disable_tlsalpn_challenge
            timeout 30s
        }
    }
    reverse_proxy 127.0.0.1:8080 {
        header_up X-Stova-Origin {env.ORIGIN_VERIFY_SECRET}
        header_up X-Stova-Client-IP {remote_host}
        flush_interval 1ms
    }
}
"""


def hosting_values(values):
    mode = values.get("HOSTING_MODE", "cloudfront")
    if mode not in ("cloudfront", "direct"):
        raise RuntimeError("The hosting mode is invalid.")
    if mode == "direct":
        hostname = values.get("PUBLIC_HOSTNAME", "")
        match = re.fullmatch(r"stova\.(\d{1,3}(?:\.\d{1,3}){3})\.sslip\.io", hostname) if isinstance(hostname, str) else None
        if not match:
            raise RuntimeError("The direct HTTPS hostname is invalid.")
        try:
            address = ipaddress.IPv4Address(match[1])
        except ipaddress.AddressValueError:
            raise RuntimeError("The direct HTTPS address is invalid.") from None
        if not address.is_global or values.get("WEB_BIND") != "127.0.0.1" or values.get("ALLOWED_ORIGINS") != "https://" + hostname:
            raise RuntimeError("Direct HTTPS needs its assigned public hostname and loopback gateway binding.")
        return mode, hostname, str(address)
    return mode, "", ""


def verify_direct_dns(hostname, address):
    """Check the host resolver and two independent HTTPS DNS resolvers first."""
    local = {result[4][0] for result in socket.getaddrinfo(hostname, 443, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)}
    if local != {address}:
        raise RuntimeError("The direct hostname does not resolve to its assigned address.")
    for endpoint in ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve"):
        for kind, record_type, expected in (("A", 1, {address}), ("AAAA", 28, set())):
            request = urllib.request.Request(endpoint + "?name=" + hostname + "&type=" + kind, headers={"Accept": "application/dns-json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                value = json.loads(response.read(65537))
            answers = {record.get("data") for record in value.get("Answer", []) if isinstance(record, dict) and record.get("type") == record_type}
            if value.get("Status") != 0 or answers != expected:
                raise RuntimeError("Independent DNS verification failed; no certificate request was started.")


def configure_direct_tls(values):
    hosting_values(values)
    secret = values.get("ORIGIN_VERIFY_SECRET")
    if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret):
        raise RuntimeError("The existing origin secret is invalid.")
    CADDY_CONFIG_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    environment = CADDY_CONFIG_DIR / "runtime.env"
    with os.fdopen(os.open(environment, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        handle.write(f"PUBLIC_HOSTNAME='{values['PUBLIC_HOSTNAME']}'\nORIGIN_VERIFY_SECRET='{secret}'\n")
    os.chmod(environment, 0o600)
    config = CADDY_CONFIG_DIR / "Caddyfile"
    config.write_text(render_caddyfile())
    os.chmod(config, 0o644)
    # Adapt checks the Caddyfile without starting certificate issuance. Only the
    # public hostname is placed in this subprocess's arguments/environment.
    subprocess.run(["/usr/local/bin/caddy", "adapt", "--config", str(config), "--adapter", "caddyfile"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, "PUBLIC_HOSTNAME": values["PUBLIC_HOSTNAME"]})


def caddy_active():
    try:
        command("systemctl", "is-active", "--quiet", "stova-caddy", capture=True)
        return True
    except subprocess.CalledProcessError:
        return False


def trusted_tls_ready(hostname):
    # Connect locally while verifying the public SNI name and a system-trusted
    # certificate chain. This avoids EC2 public-IP hairpin routing assumptions.
    with socket.create_connection(("127.0.0.1", 443), timeout=3) as connection:
        with ssl.create_default_context().wrap_socket(connection, server_hostname=hostname) as secured:
            secured.sendall(f"GET /readyz HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n".encode())
            response = http.client.HTTPResponse(secured)
            response.begin()
            if response.status != 200:
                raise RuntimeError("The trusted HTTPS endpoint is not ready.")
            response.read(65536)


def activate_direct(session, args):
    free_plan(session)
    values = settings(session, args.stack)
    mode, hostname, address = hosting_values(values)
    if mode != "direct":
        raise RuntimeError("This host is not configured for direct HTTPS.")
    if getattr(args, "require_import", False) and not (ROOT / "initial-import-receipt.json").is_file():
        raise RuntimeError("A verified initial database import is required before activation.")
    private_compose("exec", "-T", "api", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=10).read()")
    try:
        verify_direct_dns(hostname, address)
    except Exception:
        raise RuntimeRefusal("Independent DNS verification failed. Caddy was not started and no certificate request was made.") from None
    free_plan(session)
    configure_direct_tls(values)
    try:
        command("systemctl", "enable", "--now", "stova-caddy", capture=True)
        deadline = time.monotonic() + TLS_READY_SECONDS
        while True:
            try:
                trusted_tls_ready(hostname)
                print("Direct HTTPS is active with a trusted certificate and healthy database.")
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeRefusal("Trusted HTTPS did not become ready. Certificate issuance may be rate-limited; inspect Caddy before another explicit attempt. No HTTP or self-signed fallback was enabled.") from None
                time.sleep(2)
    except Exception:
        try:
            command("systemctl", "disable", "--now", "stova-caddy", capture=True)
        except Exception:
            command("systemctl", "stop", "stova-caddy", capture=True)
        raise


def validate_import_manifest(value):
    expected = {f"public.{name}" for name in (*IMPORT_TABLES, "alembic_version")}
    if not isinstance(value, dict) or value.get("format") != "PostgreSQL custom" or value.get("consistentSnapshot") is not True:
        raise RuntimeError("Import manifest does not describe a consistent PostgreSQL archive.")
    if not isinstance(value.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]):
        raise RuntimeError("Import manifest checksum is invalid.")
    size = value.get("sizeBytes")
    if type(size) is not int or not 32 <= size <= MAX_IMPORT_BYTES:
        raise RuntimeError("Import archive size is invalid.")
    tables = value.get("tables")
    if not isinstance(tables, dict) or set(tables) != expected or any(type(count) is not int or count < 0 for count in tables.values()) or tables["public.alembic_version"] != 1:
        raise RuntimeError("Import manifest table inventory is invalid.")
    neurons = value.get("writingReservedNeurons")
    if type(neurons) is not int or neurons < 0 or neurons != tables["public.writing_reservations"] * 300:
        raise RuntimeError("Import manifest writing reservations are inconsistent.")
    validation = value.get("validation")
    if not isinstance(validation, dict) or any(validation.get(key) is not True for key in ("pgRestoreList", "allTableDataEntriesPresent", "fullArchiveDecoded")) or type(validation.get("tableDataEntries")) is not int or validation["tableDataEntries"] != len(expected):
        raise RuntimeError("Import archive validation metadata is incomplete.")
    return value


def filter_import_toc(value):
    """Restore only known table data, in foreign-key dependency order."""
    selected = {}
    for line in value.splitlines():
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        if " TABLE DATA " in line:
            match = re.fullmatch(r"\s*\d+;\s+\d+\s+\d+\s+TABLE DATA\s+public\s+([a-z_]+)\s+\S+\s*", line)
            if not match or match[1] not in (*IMPORT_TABLES, "alembic_version") or match[1] in selected:
                raise RuntimeError("Import archive has unexpected or duplicate table data.")
            selected[match[1]] = line
        elif re.search(r"\b(?:SEQUENCE SET|BLOB|BLOBS|LARGE OBJECT|MATERIALIZED VIEW DATA|FOREIGN TABLE)\b", line):
            raise RuntimeError("Import archive contains unsupported data objects.")
    if set(selected) != {*IMPORT_TABLES, "alembic_version"}:
        raise RuntimeError("Import archive table data is incomplete.")
    return "\n".join(selected[name] for name in IMPORT_TABLES) + "\n", selected["alembic_version"] + "\n"


def archive_revision(sql):
    matches = re.findall(r"^COPY public\.alembic_version \(version_num\) FROM stdin;\r?\n([\s\S]*?)^\\\.\s*$", sql, re.MULTILINE)
    if len(matches) != 1:
        raise RuntimeError("The archive migration revision could not be verified.")
    revision = matches[0].strip()
    if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", revision):
        raise RuntimeError("The archive must contain exactly one valid migration revision.")
    return revision


def import_preflight_sql(revision):
    if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", revision):
        raise RuntimeError("The source migration revision is invalid.")
    names = sorted(f"public.{name}" for name in (*IMPORT_TABLES, "alembic_version"))
    inventory = ", ".join(f"'{name}'" for name in names)
    checks = "\n".join(f"IF EXISTS (SELECT 1 FROM public.{name}) THEN RAISE EXCEPTION 'Destination is not empty; nothing was imported.'; END IF;" for name in IMPORT_TABLES)
    return f"""SET LOCAL lock_timeout = '10s';
LOCK TABLE {', '.join(names)} IN ACCESS EXCLUSIVE MODE;
DO $stova_initial_import$
BEGIN
IF (SELECT array_agg(schemaname || '.' || tablename ORDER BY schemaname, tablename) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema') AND schemaname NOT LIKE 'pg_toast%') IS DISTINCT FROM ARRAY[{inventory}] THEN RAISE EXCEPTION 'Destination table inventory does not match.'; END IF;
IF (SELECT count(*) FROM public.alembic_version) <> 1 OR (SELECT max(version_num) FROM public.alembic_version) IS DISTINCT FROM '{revision}' THEN RAISE EXCEPTION 'Source and destination migration revisions do not match.'; END IF;
{checks}
END;
$stova_initial_import$;
"""


def import_verify_sql(manifest):
    manifest = validate_import_manifest(manifest)
    checks = "\n".join(f"IF (SELECT count(*) FROM public.{name}) <> {manifest['tables']['public.' + name]} THEN RAISE EXCEPTION 'Imported table count did not match the manifest.'; END IF;" for name in (*IMPORT_TABLES, "alembic_version"))
    return f"""DO $stova_initial_verify$
BEGIN
{checks}
IF (SELECT coalesce(sum(reserved_neurons), 0) FROM public.writing_reservations) <> {manifest['writingReservedNeurons']} THEN RAISE EXCEPTION 'Imported writing budget did not match the manifest.'; END IF;
END;
$stova_initial_verify$;
"""


def private_compose(*args, output=None):
    """Never inherit restore output: COPY error context may contain private data."""
    arguments = ["docker", "compose", "--env-file", str(ROOT / ".runtime.env"), "-f", str(ROOT / "compose.aws.yaml"), *args]
    return subprocess.run(arguments, check=True, cwd=ROOT, stdout=output if output is not None else subprocess.PIPE, stderr=subprocess.PIPE)


def file_sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def import_database(session, args):
    """First import only: checks, COPY, and verification share one transaction."""
    free_plan(session)
    if not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in (args.sha256, args.manifest_sha256)):
        raise RuntimeError("Both import checksums are required.")
    if not re.fullmatch(r"imports/[A-Za-z0-9_-]{1,80}/source\.dump", args.key or "") or args.manifest_key != args.key.rsplit("/", 1)[0] + "/manifest.json":
        raise RuntimeError("Import objects must use one scoped private import prefix.")
    if not (ROOT / ".runtime.env").is_file() or not (ROOT / "compose.aws.yaml").is_file():
        raise RuntimeError("The destination runtime must be deployed before importing.")
    lock = ROOT / ".initial-database-import.lock"
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    container_files = []
    container_directory = "/tmp/stova-initial-import-" + secrets.token_hex(12)
    try:
        with tempfile.TemporaryDirectory(prefix="initial-import-", dir=ROOT) as directory:
            work = Path(directory)
            archive, metadata = work / "source.dump", work / "manifest.json"
            s3 = session.client("s3")
            if s3.head_object(Bucket=args.bucket, Key=args.manifest_key).get("ContentLength", 65537) > 65536:
                raise RuntimeError("Import manifest exceeds its size limit.")
            s3.download_file(args.bucket, args.manifest_key, str(metadata))
            if metadata.stat().st_size > 65536 or file_sha256(metadata) != args.manifest_sha256:
                raise RuntimeError("Import manifest checksum did not match.")
            manifest = validate_import_manifest(json.loads(metadata.read_text(encoding="utf-8")))
            if s3.head_object(Bucket=args.bucket, Key=args.key).get("ContentLength") != manifest["sizeBytes"]:
                raise RuntimeError("Import archive remote size did not match.")
            s3.download_file(args.bucket, args.key, str(archive))
            if archive.stat().st_size != manifest["sizeBytes"] or archive.stat().st_size > MAX_IMPORT_BYTES or file_sha256(archive) != args.sha256 or manifest["sha256"] != args.sha256:
                raise RuntimeError("Import archive checksum or size did not match.")
            with archive.open("rb") as handle:
                if handle.read(5) != b"PGDMP":
                    raise RuntimeError("The source is not a PostgreSQL custom archive.")
            private_compose("exec", "-T", "db", "mkdir", "-m", "700", container_directory)

            def copy_file(path):
                target = container_directory + "/" + path.name
                private_compose("cp", str(path), "db:" + target)
                container_files.append(target)
                return target

            archive_target = copy_file(archive)
            toc = private_compose("exec", "-T", "db", "pg_restore", "--list", archive_target).stdout.decode("utf-8")
            data_toc, revision_toc = filter_import_toc(toc)
            data_list, revision_list = work / "data.list", work / "revision.list"
            data_list.write_text(data_toc, encoding="utf-8")
            revision_list.write_text(revision_toc, encoding="utf-8")
            data_target, revision_target = copy_file(data_list), copy_file(revision_list)
            common = ("--data-only", "--no-owner", "--no-privileges", "--exit-on-error")
            revision_sql = private_compose("exec", "-T", "db", "pg_restore", *common, "--use-list=" + revision_target, "--file=-", archive_target).stdout.decode("utf-8")
            revision = archive_revision(revision_sql)
            if manifest.get("alembicRevision", revision) != revision:
                raise RuntimeError("Manifest and archive migration revisions do not match.")
            preflight, restore, verify = work / "preflight.sql", work / "restore.sql", work / "verify.sql"
            preflight.write_text(import_preflight_sql(revision), encoding="utf-8")
            verify.write_text(import_verify_sql(manifest), encoding="utf-8")
            with restore.open("wb") as output:
                private_compose("exec", "-T", "db", "pg_restore", *common, "--use-list=" + data_target, "--file=-", archive_target, output=output)
            preflight_target, restore_target, verify_target = copy_file(preflight), copy_file(restore), copy_file(verify)
            psql = ("exec", "-T", "db", "psql", "-U", "stova", "-d", "stova", "--no-psqlrc", "--set", "ON_ERROR_STOP=1", "--single-transaction")
            # Refuse an already populated destination without taking it offline.
            private_compose(*psql, "--file", preflight_target)
            free_plan(session)
            private_compose("stop", "--timeout", "30", "api")
            # The authoritative empty check is repeated under locks in the same
            # transaction as restoration and verification. FK triggers stay on.
            private_compose(*psql, "--file", preflight_target, "--file", restore_target, "--file", verify_target)
            receipt = {"schemaVersion": 1, "sha256": args.sha256, "manifestSha256": args.manifest_sha256, "alembicRevision": revision, "tables": manifest["tables"], "writingReservedNeurons": manifest["writingReservedNeurons"], "completedAtUtc": datetime.now(timezone.utc).isoformat()}
            receipt_path = ROOT / "initial-import-receipt.json"
            with os.fdopen(os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
                handle.write(json.dumps(receipt, sort_keys=True) + "\n")
            private_compose("up", "--detach", "--no-deps", "--wait", "--wait-timeout", "120", "api")
            print("Initial database transfer committed and verified; API is healthy.")
    finally:
        if container_files:
            try:
                private_compose("exec", "-T", "db", "rm", "-f", "--", *container_files)
                private_compose("exec", "-T", "db", "rmdir", "--", container_directory)
            except Exception:
                print("Temporary import files need owner cleanup on the host.", file=sys.stderr)
        lock.unlink(missing_ok=True)


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
    values.setdefault("HOSTING_MODE", "cloudfront")
    values.setdefault("PUBLIC_HOSTNAME", "")
    values.setdefault("WEB_BIND", "0.0.0.0")
    hosting_values(values)
    permitted = required | {"CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_AI_FREE_TIER_CONFIRMED", "HOSTING_MODE", "PUBLIC_HOSTNAME", "WEB_BIND"}
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
    return values


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
    values = settings(session, args.stack)
    preserve_caddy = values.get("HOSTING_MODE") == "direct" and caddy_active()
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
    if preserve_caddy:
        configure_direct_tls(values)
        command("systemctl", "restart", "stova-caddy", capture=True)
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
    parser.add_argument("action", choices=["deploy", "guard", "backup", "import-db", "activate"])
    parser.add_argument("--region", required=True)
    parser.add_argument("--stack", required=True)
    parser.add_argument("--bucket")
    parser.add_argument("--key")
    parser.add_argument("--sha256")
    parser.add_argument("--manifest-key")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--release-id")
    parser.add_argument("--require-import", action="store_true")
    args = parser.parse_args(argv)
    try:
        session = session or boto3.Session(region_name=args.region)
        if args.action == "guard":
            try:
                free_plan(session)
            except Exception:
                try:
                    if Path("/etc/systemd/system/stova-caddy.service").is_file():
                        try:
                            command("systemctl", "stop", "stova-caddy", capture=True)
                        except Exception:
                            print("The HTTPS service could not stop gracefully; requesting instance shutdown.", file=sys.stderr)
                    if (ROOT / "compose.aws.yaml").exists():
                        compose("down")
                except Exception:
                    print("Containers could not stop gracefully; requesting instance shutdown.", file=sys.stderr)
                finally:
                    # Stop preserves the encrypted database volume. A Compose or
                    # Docker failure must never bypass the instance shutdown.
                    command("shutdown", "-h", "now")
                return 2
        elif args.action == "activate":
            activate_direct(session, args)
        elif args.action == "import-db":
            import_database(session, args)
        elif args.action == "backup":
            backup(session, args)
        else:
            deploy(session, args)
    except Exception as error:
        # Provider exceptions may contain request payloads; output only their type.
        print(f"Runtime operation refused: {error}" if isinstance(error, RuntimeRefusal) else f"Runtime operation failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
