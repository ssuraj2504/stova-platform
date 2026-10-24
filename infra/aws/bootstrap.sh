#!/bin/bash
set -euo pipefail
umask 077
dnf install -y docker python3.12 python3.12-pip
systemctl enable --now docker
systemctl enable --now amazon-ssm-agent
install -d -m 700 /opt/stova /opt/stova/releases
python3.12 -m venv /opt/stova/tools
/opt/stova/tools/bin/python -m pip install --quiet 'boto3>=1.43.118,<2'
install -d -m 755 /usr/local/lib/docker/cli-plugins
compose_version=v2.39.4
compose_base="https://github.com/docker/compose/releases/download/$compose_version"
curl --fail --silent --show-error --location "$compose_base/docker-compose-linux-x86_64" --output /tmp/stova-compose
curl --fail --silent --show-error --location "$compose_base/docker-compose-linux-x86_64.sha256" --output /tmp/stova-compose.sha256
expected=$(cut -d ' ' -f 1 /tmp/stova-compose.sha256)
printf '%s  /tmp/stova-compose\n' "$expected" | sha256sum --check --status
install -m 755 /tmp/stova-compose /usr/local/lib/docker/cli-plugins/docker-compose
rm -f /tmp/stova-compose /tmp/stova-compose.sha256
docker compose version >/dev/null
touch /opt/stova/bootstrap-ready
