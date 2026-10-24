# AWS deployment

This is an optional, credit-backed deployment. It has **not** been deployed by preparing these files. AWS resources consume credits; this is not a permanent-free hosting promise.

The deploy and release commands refuse to create or update workloads unless AWS's `GetAccountPlanState` reports all of:

- `accountPlanType = FREE`
- `accountPlanStatus = ACTIVE`
- Positive `accountPlanRemainingCredits.amount`, with unit `USD`
- A future `accountPlanExpirationDate`

Missing permissions, old paid accounts, expired credits, and unknown responses fail closed. There is no paid-plan override. A budget alert is not used as a spending cap. Do not upgrade the account to a paid plan while these resources remain: retained EC2 storage, S3 objects, and CloudFront can then incur normal charges. AWS Free accounts can stop working when their credits or time expire.

## What runs

A single AL2023 `t3.micro` runs Docker Compose with FastAPI, the Tornado/React gateway, and PostgreSQL 17. PostgreSQL uses a persistent Docker volume on the encrypted 20 GB EC2 root disk. Container restarts and releases preserve it; deleting/replacing the instance destroys it.

CloudFront provides an HTTPS `cloudfront.net` URL with its default certificate. Caching is disabled, and viewer headers/cookies/query strings are forwarded for authentication and SSE. CloudFront reaches the origin over HTTP on port 8080. That port accepts only the AWS-managed CloudFront origin-facing prefix list, and the gateway also requires a random `X-Stova-Origin` verification header. SSH, PostgreSQL, and the API port are not exposed.

The origin secret is installed through the CloudFront SDK after stack creation, rather than embedded in the CloudFormation template. This intentionally creates a small, managed CloudFormation drift (`Enabled` and the origin header); `aws_deploy.py deploy` reapplies it. Do not enable the distribution manually before runtime setup.

S3 stores private encrypted image archives and database exports. JWT, PostgreSQL, Cloudflare, and origin-verification secrets live in a Standard SSM SecureString at `/stova/STACK/runtime`; the host fetches them directly into a root-only runtime file. They are not sent in user data, CloudFormation parameters, deployment command lines, repository files, or script output. The EC2 IAM role uses IMDSv2 with a one-hop limit, which prevents ordinary container access to instance credentials.

The host rechecks Free account eligibility every five minutes. If verification fails, it stops the workload and instance while preserving the disk. This is a secondary operational guard, **not a billing hard cap**: retained resources still exist. An explicit owner `deploy` can resume an existing stopped instance only after a fresh positive Free-plan check. It then reapplies runtime settings and updates CloudFront to the instance's current origin hostname. There is no paid-plan recovery or instance replacement fallback.

### Explicit direct HTTPS alternative

`--hosting direct` selects a separate reviewed template for a **new stack**. It creates no CloudFront resources and makes no CloudFront SDK calls. It never converts or replaces an existing CloudFront installation; the infrastructure comparison still refuses changes to an existing stack. Account/service verification failures do not trigger an automatic architecture or paid-account fallback.

The direct stack assigns an Elastic IP and publishes `https://stova.ADDRESS.sslip.io`. Its security group opens only TCP80/443 to the public; SSH, database and API ingress remain closed. Caddy terminates HTTPS on the host and proxies to the Tornado gateway at `127.0.0.1:8080`. The gateway port is bound only to loopback. Caddy overwrites the origin proof and a dedicated client-IP header; the gateway checks the proof and validates that IP in direct mode before forwarding it to the API. Arbitrary forwarded IP chains are not trusted.

The boot script installs the official [Caddy v2.11.7 release](https://github.com/caddyserver/caddy/releases/tag/v2.11.7), pinned to SHA-256 `727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b`. It retries bounded package/download failures during initial EIP association. Caddy runs as an unprivileged systemd user with only the low-port binding capability. Its admin API is disabled. The root-only environment file contains the existing origin proof; the readable Caddyfile contains an environment placeholder instead of a literal secret. Access logging and environment printing are not enabled.

Caddy stays stopped through a deferred first deployment and database import. Explicit activation checks local readiness and verifies that the hostname resolves exclusively to the assigned IPv4 address through the host resolver and two independent HTTPS DNS resolvers, including refusal of unexpected IPv6 answers. Only then can Caddy request a certificate from the explicitly configured Let's Encrypt ACME issuer. Activation checks a system-trusted chain and public hostname over TLS; failure disables/stops Caddy and reports a safe refusal. There is no insecure certificate bypass, HTTP application fallback, self-signed fallback, or alternate paid issuer. Do not repeatedly rerun production issuance after a failure: inspect DNS, firewall and certificate errors first. Shared `sslip.io`/`nip.io` certificate limits can cause rate-limited issuance despite correct configuration.

Certificates, private keys and ACME account state persist under `/var/lib/stova-caddy` on the encrypted instance disk across releases, stops and restarts. Do not clear this directory to troubleshoot issuance: doing so can request duplicate certificates and hit limits. A normal release keeps an inactive Caddy service inactive and refreshes an already active service without deleting its state. The Free-plan guard stops Caddy too, and still requests instance shutdown if stopping containers or Caddy fails.

Use the same reviewed three-image release archive and an explicitly supplied ignored runtime settings file:

```sh
python scripts/aws_deploy.py deploy --hosting direct --region us-east-1 \
  --stack stova-direct --env-file .env.aws.local --archive outputs/images.tar.gz \
  --release-id initial --github-repo ssuraj2504/stova-platform --defer-activation
```

Then follow `import-db` and `activate` below using `--stack stova-direct`. `HOSTING_MODE`, `PUBLIC_HOSTNAME` and `WEB_BIND` come from verified stack outputs, not owner-provided overrides. Application-only `release` commands infer the existing mode from stored runtime configuration. Verify the returned HTTPS URL from an external browser after activation; local trusted-TLS readiness alone is not an Internet reachability test.

The public IPv4 allocation and other AWS resources consume credits, including while an Elastic IP remains allocated to a stopped instance. They are not permanently free. Teardown of the direct stack skips CloudFront and releases the Elastic IP along with the instance/resources. Keep the hostname's address allocated while using it: after release, a later owner of that IP can use the same address-derived DNS name. Free DNS and certificate services are external dependencies with their own availability and limits.

## Read-only eligibility check

Install a current SDK in the deployment environment:

```sh
python -m pip install 'boto3>=1.43.118,<2'
python scripts/aws_deploy.py
```

The default command only calls the read-only Free Tier account-plan API. Authenticate through your normal AWS profile or SSO; do not put AWS access keys in a source file. This check requires `freetier:GetAccountPlanState`.

## First deployment

Build the Linux amd64 images locally or in a trusted runner. Include PostgreSQL so the host does not need Docker Hub credentials:

```sh
docker build --platform linux/amd64 -t stova-api:release -f backend/Dockerfile .
docker build --platform linux/amd64 -t stova-web:release .
docker pull --platform linux/amd64 postgres:17-alpine
mkdir -p outputs
docker save stova-api:release stova-web:release postgres:17-alpine | gzip > outputs/images.tar.gz
```

Create the ignored `.env.aws.local` yourself. Managed writing can remain unconfigured:

```dotenv
CLOUDFLARE_ACCOUNT_ID=
CLOUDFLARE_API_TOKEN=
CLOUDFLARE_AI_FREE_TIER_CONFIRMED=false
```

Only set the confirmation to `true` after the owner has confirmed the Cloudflare account is on its Free plan. The script generates strong PostgreSQL, JWT, and origin secrets in memory if not explicitly supplied, and sends them directly to SSM. It never automatically reads the development `.env` or another setup file.

Run the explicit deployment after reviewing the read-only eligibility result:

```sh
python scripts/aws_deploy.py deploy --region us-east-1 --stack stova \
  --env-file .env.aws.local --archive outputs/images.tar.gz \
  --release-id initial --github-repo ssuraj2504/stova-platform
```

The owner deployment identity needs CloudFormation, EC2/VPC, S3, IAM, SSM, and CloudFront provisioning permissions in addition to the eligibility read permission. Existing-stack checks require `cloudformation:GetTemplate`; resuming a stopped host requires EC2 describe/start/waiter permissions. The script installs a pinned Docker Compose plugin with a checksum check. The AMI is resolved from AWS's public AL2023 parameter on first creation, then pinned for later runtime deployments.

The script waits for the stack, uploads runtime assets/images, executes a scoped SSM command, checks container health, installs the CloudFront header, and prints only public outputs. CloudFront creation/updates can take several minutes. A returned domain alone does not prove deployment health; the command must finish successfully. No live AWS deployment has been verified by the included offline checks.

When moving an existing local installation, add `--defer-activation` to this initial deploy. A new distribution stays disabled while its empty database is prepared. Complete the first-import procedure below before running the separate `activate` command. The flag skips activation; it does not disable a distribution that is already live. Deploying without this flag retains the normal activation behavior.

### Existing infrastructure is frozen

Initial `deploy` may create a stack. When the stack already exists, `deploy` compares its original CloudFormation template and complete parameter values with the requested configuration. JSON key order and whitespace do not count as differences. Any substantive template or parameter difference is refused **before runtime changes**, with no call to `update_stack`. An unverifiable comparison is also refused.

An unchanged stack can receive runtime settings, application images and origin-header configuration, and an existing stopped instance can be resumed while Free eligibility is verified. Use `release` for normal application-only updates. Changes to the AMI, networking, disks, IAM resources, GitHub repository parameters or any other infrastructure need a separately reviewed backup, change set and data-migration plan outside this helper. Removing or replacing the EC2 instance destroys its database disk; the helper never attempts that as a repair.

The first direct host provisioned on 2026-10-24 encountered an AL2023 package conflict: its preinstalled `curl-minimal` conflicted with an explicit full `curl` package request. The owner performed a Free-gated, one-time package swap on that still-empty host, then SHA-verified and reran its original frozen user-data script; bootstrap completed. The corrected script for **future stacks** uses the preinstalled curl binary instead. Consequently, that existing stack's original user-data differs from the corrected source template: `deploy` intentionally refuses that infrastructure difference until a separate review. Its normal application-only `release` path is unaffected. This repair does not authorize automatic template updates, instance replacement, or bypassing the immutable comparison.

## GitHub release workflow contract

The optional role trusts only `repo:ssuraj2504/stova-platform:ref:refs/heads/main` when that repository parameter is used. A job with `environment: production` has a different OIDC subject and will not match this trust. Use a protected main branch and the repository's reviewed manual deployment workflow; never deploy pull requests.

Set workflow variables `AWS_REGION`, `AWS_STACK_NAME`, and `AWS_ROLE_ARN` from the stack's `GitHubRoleArn` output. Assume the role using GitHub OIDC (`id-token: write`), build the three-image archive above, then run:

```sh
python scripts/aws_deploy.py release --region "$AWS_REGION" \
  --stack "$AWS_STACK_NAME" --archive outputs/images.tar.gz \
  --release-id "$GITHUB_SHA"
```

This role can read eligibility/stack outputs, upload release assets, and execute commands on this one instance. It has no direct SSM secret-read permission or infrastructure-change permission. Release commands run as root on the host, so protect this role and the main branch as production deployment authority. The host fetches its own secrets. Each release rechecks Free account eligibility; there is no automatic paid provider or paid AWS fallback.

## First database transfer

This is an owner-only cutover into an already deployed, migrated **empty** destination. It transfers the complete application data: user IDs and password hashes, sessions and throttles, publications and articles, and every shared writing-budget reservation. It never replaces the schema or the destination's Alembic version row. It has no overwrite or force option and is not a general restore command for a populated installation.

Pause source API writes and writing requests before creating the final backup, and keep the source writer disabled after cutover. Create a PostgreSQL 17 custom-format `pg_dump` archive and matching manifest using the same exported, consistent database snapshot. A prior snapshot does not include later registrations, edits or inference reservations. Running two independent copies against the same writing account would also split the shared budget ledger; do not resume the old writer after activation.

The manifest must contain `format: "PostgreSQL custom"`, `consistentSnapshot: true`, the archive's lowercase SHA-256 and `sizeBytes` (maximum 512 MiB), exact nonnegative integer counts in `tables`, and `writingReservedNeurons`. Its table inventory must be exactly:

```text
public.alembic_version       (exactly one row)
public.users
public.auth_sessions
public.auth_throttles
public.projects
public.articles
public.writing_accounts
public.writing_reservations
```

`writingReservedNeurons` must equal the reservation count times 300. The `validation` object must record `pgRestoreList`, `allTableDataEntriesPresent` and `fullArchiveDecoded` as `true`, and `tableDataEntries` as `8`. An optional `archive` filename must match the supplied dump; an optional `alembicRevision` must match the archive. These fields are owner-supplied integrity metadata, not an independent signature or proof that the source is trustworthy. Only import a trusted owner-created archive: PostgreSQL restores execute source-controlled statements.

After the deferred deployment is healthy, run:

```sh
python scripts/aws_deploy.py import-db --region us-east-1 --stack stova \
  --dump-file outputs/deployment-backups/stova-TIMESTAMP.dump \
  --manifest-file outputs/deployment-backups/stova-TIMESTAMP.manifest.json

python scripts/aws_deploy.py activate --region us-east-1 --stack stova
```

The owner helper checks Free eligibility before file access or uploads, verifies the archive against its manifest, then uploads three encrypted, private objects under one unique `imports/` prefix. The host downloads them through its own role; command arguments contain identifiers and pinned checksums, never runtime credentials or database connection secrets. It verifies both archive and manifest hashes, allowlists the expected table-data entries, and separately extracts the source Alembic revision as metadata. Unknown, missing or duplicate table data, unsupported data objects, and schema-revision mismatches are refused.

The host first checks the destination without stopping the API. Once that check passes it stops the API, then uses one `psql --single-transaction` invocation for the authoritative empty/schema checks, restoration, and final exact row-count and budget-total assertions. Every application table and the migration table are locked throughout that transaction. Tables restore in foreign-key order with their constraints active. Any failed check or COPY rolls back the complete transfer. This uses [psql's transaction wrapper](https://www.postgresql.org/docs/17/app-psql.html) around the data-only SQL emitted by [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html), so validation happens before commit. A separate check after `pg_restore --single-transaction` would be too late to roll back.

The API resumes only after the verified commit, and a root-only import receipt records hashes, revision and counts. The transfer objects are deleted from S3 after confirmed success; the local backup remains intact. A failure after the API stops leaves it stopped for owner review. A failed API restart or a failed transfer-object cleanup can occur after a successful commit: inspect the receipt and database before retrying, because a second import into that now-populated database is deliberately refused. Restore output is captured rather than logged, because database errors can include private row data. Failed upload/import objects remain private and need explicit owner cleanup; no failed operation automatically overwrites records or enables the distribution.

`activate` requires the import receipt and a healthy API/database, reads the existing origin secret directly from the owner's SSM parameter, rechecks Free eligibility, and enables CloudFront. It never generates a replacement secret. Verify sign-in and published pages at the returned HTTPS URL. Password hashes and IDs are preserved; a new domain and deployment signing secret require a fresh sign-in.

Offline safeguards run without AWS or Docker:

```sh
python -m unittest discover -s infra/aws/tests -p 'test_aws_*.py' -q
```

The opt-in PostgreSQL proof creates and drops two uniquely named `*_test` databases containing only synthetic records in the separate local test container. Install the backend test dependencies, start `stova-platform-postgres` on port 55432, then set `STOVA_IMPORT_POSTGRES_TEST=1` and run `test_aws_import_postgres.py`. It proves successful full-data restoration, exact-count rollback, nonempty refusal, revision refusal, and active foreign-key rollback. It does not read or change the local application database.

## Backup, restore, and teardown

Export regularly and before any infrastructure change:

```sh
python scripts/aws_deploy.py backup --region us-east-1 --stack stova \
  --backup-dir outputs/database-backups
```

Exports are `pg_dump -Fc` files, containing private account and article data. A successful backup downloads the dump and writes a matching `.dump.json` evidence sidecar with the exact CloudFormation stack ID, stack name, creation time, file size and SHA-256 checksum. The export command must have completed successfully, and the local copy must have a non-empty PostgreSQL custom-format header before evidence is written. Both local files are created with restrictive permissions.

Keep the dump and its sidecar together outside Git, protect access, and test a restore into a separate PostgreSQL database with `pg_restore --no-owner --no-privileges`. The sidecar checks identity and file integrity; it does not prove that the backup is current or that a restore will satisfy your recovery needs. Do not overwrite a live database until a restore has been reviewed.

Releases retain the PostgreSQL volume but database schema migrations run on API startup. A previous image archive is not a database migration rollback. A failed release can leave the site unavailable; inspect the SSM command, preserve the database, and deploy a compatible fix or restore the reviewed backup.

```sh
python scripts/aws_deploy.py destroy --region us-east-1 --stack stova \
  --backup-dir outputs/database-backups
```

By default, teardown first takes a fresh live backup. If that export fails, nothing is deleted. After a successful backup it disables CloudFront, clears the private S3 bucket, deletes the stack and deletes the runtime SSM parameter.

If the guard has stopped the instance, cleanup can instead use a previously downloaded, reviewed backup without starting paid compute or contacting the stopped host:

```sh
python scripts/aws_deploy.py destroy --region us-east-1 --stack stova \
  --backup-file outputs/database-backups/stova-TIMESTAMP.dump
```

The adjacent `stova-TIMESTAMP.dump.json` sidecar is required. Before any teardown mutation, the helper validates its structure, creation timestamp, PostgreSQL archive marker, file size and SHA-256, then checks its exact stack ID against the current stack. A backup from a different stack, including a deleted/recreated stack with the same name, is refused. Missing, altered or mismatched evidence is refused. Do not combine `--backup-file` with `--backup-dir`.

**Choosing `--backup-file` is the owner's explicit responsibility to review backup freshness and accept losing any later database changes.** The helper does not impose an age cutoff, start the host, or take a newer export in this mode. Review or test the restore before choosing this destructive path. Evidence is local integrity metadata, not an independent attestation of data completeness.

Cleanup remains available on paid/expired accounts because it does not create or resume workloads. It removes the instance and its database disk. Downloaded local backups and sidecars remain; AWS backups in the bucket are deleted. If a stack failed before the database started or no reviewed backup exists, inspect the resources and preserve the disk before arranging manual cleanup; this helper will not silently bypass the backup requirement.

## Offline checks

```sh
python infra/aws/template.py
cfn-lint infra/aws/template.json
python -m unittest discover -s infra/aws/tests -v
```

These checks validate structure and refusal logic, not account eligibility, provider inference, or a live deployment.

Sources: [AWS account-plan response](https://docs.aws.amazon.com/botocore/latest/reference/services/freetier/client/get_account_plan_state.html), [managed cache policy](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html), [managed origin-request policy](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-origin-request-policies.html).
