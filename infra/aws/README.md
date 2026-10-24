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

### Existing infrastructure is frozen

Initial `deploy` may create a stack. When the stack already exists, `deploy` compares its original CloudFormation template and complete parameter values with the requested configuration. JSON key order and whitespace do not count as differences. Any substantive template or parameter difference is refused **before runtime changes**, with no call to `update_stack`. An unverifiable comparison is also refused.

An unchanged stack can receive runtime settings, application images and origin-header configuration, and an existing stopped instance can be resumed while Free eligibility is verified. Use `release` for normal application-only updates. Changes to the AMI, networking, disks, IAM resources, GitHub repository parameters or any other infrastructure need a separately reviewed backup, change set and data-migration plan outside this helper. Removing or replacing the EC2 instance destroys its database disk; the helper never attempts that as a repair.

## GitHub release workflow contract

The optional role trusts only `repo:ssuraj2504/stova-platform:ref:refs/heads/main` when that repository parameter is used. A job with `environment: production` has a different OIDC subject and will not match this trust. Use a protected main branch and the repository's reviewed manual deployment workflow; never deploy pull requests.

Set workflow variables `AWS_REGION`, `AWS_STACK_NAME`, and `AWS_ROLE_ARN` from the stack's `GitHubRoleArn` output. Assume the role using GitHub OIDC (`id-token: write`), build the three-image archive above, then run:

```sh
python scripts/aws_deploy.py release --region "$AWS_REGION" \
  --stack "$AWS_STACK_NAME" --archive outputs/images.tar.gz \
  --release-id "$GITHUB_SHA"
```

This role can read eligibility/stack outputs, upload release assets, and execute commands on this one instance. It has no direct SSM secret-read permission or infrastructure-change permission. Release commands run as root on the host, so protect this role and the main branch as production deployment authority. The host fetches its own secrets. Each release rechecks Free account eligibility; there is no automatic paid provider or paid AWS fallback.

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
