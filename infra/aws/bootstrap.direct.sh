#!/bin/bash
set -euo pipefail
umask 077

# EIP association can briefly interrupt the initial public connection.
retry() {
  local attempt
  for attempt in 1 2 3 4 5 6; do
    if "$@"; then return 0; fi
    sleep "$((attempt * 5))"
  done
  return 1
}

retry dnf install -y docker python3.12 python3.12-pip curl tar
systemctl enable --now docker
systemctl enable --now amazon-ssm-agent
install -d -m 700 /opt/stova /opt/stova/releases
python3.12 -m venv /opt/stova/tools
retry /opt/stova/tools/bin/python -m pip install --quiet 'boto3>=1.43.118,<2'
install -d -m 755 /usr/local/lib/docker/cli-plugins
compose_version=v2.39.4
compose_base="https://github.com/docker/compose/releases/download/$compose_version"
retry curl --fail --silent --show-error --location --retry 3 "$compose_base/docker-compose-linux-x86_64" --output /tmp/stova-compose
retry curl --fail --silent --show-error --location --retry 3 "$compose_base/docker-compose-linux-x86_64.sha256" --output /tmp/stova-compose.sha256
expected=$(cut -d ' ' -f 1 /tmp/stova-compose.sha256)
printf '%s  /tmp/stova-compose\n' "$expected" | sha256sum --check --status
install -m 755 /tmp/stova-compose /usr/local/lib/docker/cli-plugins/docker-compose
rm -f /tmp/stova-compose /tmp/stova-compose.sha256
docker compose version >/dev/null

# Pinned official Caddy release and its vendor-published SHA-256.
caddy_version=2.11.7
caddy_sha256=727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b
retry curl --fail --silent --show-error --location --retry 3 "https://github.com/caddyserver/caddy/releases/download/v$caddy_version/caddy_${caddy_version}_linux_amd64.tar.gz" --output /tmp/stova-caddy.tar.gz
printf '%s  /tmp/stova-caddy.tar.gz\n' "$caddy_sha256" | sha256sum --check --status
install -d -m 700 /tmp/stova-caddy-install
tar -xzf /tmp/stova-caddy.tar.gz -C /tmp/stova-caddy-install caddy
install -m 755 /tmp/stova-caddy-install/caddy /usr/local/bin/caddy
rm -f /tmp/stova-caddy.tar.gz /tmp/stova-caddy-install/caddy
rmdir /tmp/stova-caddy-install
groupadd --system stova-caddy
useradd --system --gid stova-caddy --home-dir /var/lib/stova-caddy --shell /sbin/nologin stova-caddy
install -d -m 755 /etc/stova-caddy
install -d -m 700 -o stova-caddy -g stova-caddy /var/lib/stova-caddy
cat > /etc/systemd/system/stova-caddy.service <<'STOVA_SERVICE'
[Unit]
Description=Stova HTTPS entry point
After=network-online.target docker.service
Wants=network-online.target
[Service]
Type=notify
User=stova-caddy
Group=stova-caddy
EnvironmentFile=/etc/stova-caddy/runtime.env
Environment=XDG_DATA_HOME=/var/lib/stova-caddy/data
Environment=XDG_CONFIG_HOME=/var/lib/stova-caddy/config
WorkingDirectory=/var/lib/stova-caddy
ExecStart=/usr/local/bin/caddy run --config /etc/stova-caddy/Caddyfile --adapter caddyfile
StateDirectory=stova-caddy
StateDirectoryMode=0700
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/stova-caddy
LimitNOFILE=1048576
Restart=on-failure
RestartSec=5s
TimeoutStopSec=10s
[Install]
WantedBy=multi-user.target
STOVA_SERVICE
systemctl daemon-reload
# No enable/start here: deferred deployment and import must remain private.
touch /opt/stova/bootstrap-ready
