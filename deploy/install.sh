#!/bin/bash
# Installs the server. Run as root: ./deploy/install.sh mcp.example.com
# Safe to re-run: keeps the vault key, the session, the connection and the settings.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run as root." >&2
    exit 1
fi
if [[ $# -ne 1 || ! "$1" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]]; then
    echo "Usage: $0 <public-hostname>, e.g. $0 49-12-236-228.sslip.io" >&2
    exit 1
fi

HOST="$1"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER=tgmcp
WEB_USER=tgmcp-web
STATE=/var/lib/tg-mcp
WEB_STATE=/var/lib/tg-mcp-web
WEB_CONFIG=/etc/tg-mcp/web.toml
EXPORTS=$STATE/exports
CREDENTIAL=/etc/tg-mcp/vault-key.cred
CONFIG=/etc/tg-mcp/config.toml
EXPORT_RAM_LIMIT=1G

echo "==> Reverse proxy (Caddy)"
CADDY_IS_NEW=no
if ! command -v caddy >/dev/null; then
    CADDY_IS_NEW=yes
    apt-get update -qq
    apt-get install -y -qq caddy python3-venv
else
    apt-get install -y -qq python3-venv
fi

echo "==> Service user and directories"
for user in "$SERVICE_USER" "$WEB_USER"; do
    id "$user" >/dev/null 2>&1 ||
        useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$user"
done
install -d -m 0755 -o root -g root /etc/tg-mcp /opt/tg-mcp /usr/local/lib/tg-mcp /etc/caddy/conf.d
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE" "$STATE/private"
install -d -m 0700 -o "$WEB_USER" -g "$WEB_USER" "$WEB_STATE"
# Earlier versions kept Claude's connection next to the Telegram session; move it out.
for file in oauth.json pairing.json; do
    if [[ -e "$STATE/private/$file" && ! -e "$WEB_STATE/$file" ]]; then
        mv "$STATE/private/$file" "$WEB_STATE/$file"
        chown "$WEB_USER:$WEB_USER" "$WEB_STATE/$file"
    fi
    rm -f "$STATE/private/$file"
done

echo "==> Vault key"
if [[ -e "$CREDENTIAL" ]]; then
    echo "    Keeping the existing key"
else
    if systemd-creds has-tpm2 >/dev/null 2>&1; then
        KEY_MODE=host+tpm2
    else
        KEY_MODE=host
        echo "    NOTE: no TPM2 here. The key is protected by a root-only host secret,"
        echo "    which is also included in full-disk backups and snapshots of this server."
    fi
    systemd-creds setup
    head -c 32 /dev/urandom |
        systemd-creds encrypt --with-key="$KEY_MODE" --name=vault-key - "$CREDENTIAL"
    chmod 0600 "$CREDENTIAL"
fi

echo "==> Application (hash-verified dependencies)"
python3 -m venv /opt/tg-mcp/venv
# pyaes ships only as source, so its build tool is hash-pinned too and removed afterwards.
PIP="/opt/tg-mcp/venv/bin/pip install --quiet --no-input --require-hashes --only-binary=:all:"
$PIP -r "$SOURCE/requirements-build.lock"
$PIP --no-binary=pyaes --no-build-isolation -r "$SOURCE/requirements.lock"
/opt/tg-mcp/venv/bin/pip uninstall --quiet --yes setuptools
SITE="$(/opt/tg-mcp/venv/bin/python -c 'import site; print(site.getsitepackages()[0])')"
rm -rf "$SITE/tg_mcp"
cp -r "$SOURCE/src/tg_mcp" "$SITE/tg_mcp"
find "$SITE/tg_mcp" -name __pycache__ -type d -prune -exec rm -rf {} +
chown -R root:root /opt/tg-mcp
chmod -R go-w /opt/tg-mcp

echo "==> Configuration and commands"
if [[ ! -e "$CONFIG" ]]; then
    sed "s|__PUBLIC_URL__|https://$HOST|" "$SOURCE/deploy/config.toml" >"$CONFIG"
else
    sed -i "s|^public_url = .*|public_url = \"https://$HOST\"|" "$CONFIG"
    # Upgrade from the single-process version: this setting moved to web.toml.
    sed -i '/^session_days *=/d' "$CONFIG"
    if ! grep -q '^\[clients\.claude\]' "$CONFIG"; then
        printf '\n[clients.claude]\nchats = "all"\nread = true\nmedia = true\nexport = true\nsocket_user = "%s"\nsocket_group = "%s"\n' \
            "$WEB_USER" "$WEB_USER" >>"$CONFIG"
    fi
fi
chown root:"$SERVICE_USER" "$CONFIG"
chmod 0640 "$CONFIG"
if [[ ! -e "$WEB_CONFIG" ]]; then
    sed "s|__PUBLIC_URL__|https://$HOST|" "$SOURCE/deploy/web.toml" >"$WEB_CONFIG"
else
    sed -i "s|^public_url = .*|public_url = \"https://$HOST\"|" "$WEB_CONFIG"
fi
chown root:"$WEB_USER" "$WEB_CONFIG"
chmod 0640 "$WEB_CONFIG"
install -m 0644 -o root -g root "$SOURCE/deploy/hardening.sh" /usr/local/lib/tg-mcp/hardening.sh
for command in tg-mcp-login tg-mcp-pair tg-mcp-revoke; do
    install -m 0750 -o root -g root "$SOURCE/deploy/$command" "/usr/local/sbin/$command"
done

echo "==> RAM-only export storage (never written to disk or swap)"
MOUNT_UNIT="$(systemd-escape --path --suffix=mount "$EXPORTS")"
# The bare mountpoint belongs to root: if the RAM disk is ever missing, exports fail
# instead of silently landing on the disk.
if ! mountpoint -q "$EXPORTS"; then
    install -d -m 0755 -o root -g root "$EXPORTS"
fi
cat >"/etc/systemd/system/$MOUNT_UNIT" <<EOF
[Unit]
Description=RAM-only Telegram MCP exports

[Mount]
What=tmpfs
Where=$EXPORTS
Type=tmpfs
Options=size=$EXPORT_RAM_LIMIT,mode=0700,uid=$(id -u "$SERVICE_USER"),gid=$(id -g "$SERVICE_USER"),noexec,nosuid,nodev,noswap

[Install]
WantedBy=multi-user.target
EOF

echo "==> Services"
# The single-process version ran the endpoint as the session owner; stop it before switching.
systemctl stop tg-mcp.service 2>/dev/null || true

# Telegram core: full sandbox, network only to Telegram.
{
    cat "$SOURCE/deploy/tg-core.service"
    sed -n 's/^-p //p' "$SOURCE/deploy/hardening.sh" | grep -v '^RequiresMountsFor='
    printf '\n[Install]\nWantedBy=multi-user.target\n'
} >/etc/systemd/system/tg-core.service

# Public endpoint: the same sandbox without any IP networking, writing only its own state.
install -m 0644 -o root -g root "$SOURCE/deploy/tg-mcp.socket" /etc/systemd/system/tg-mcp.socket
{
    cat "$SOURCE/deploy/tg-mcp.service"
    sed -n 's/^-p //p' "$SOURCE/deploy/hardening.sh" |
        grep -vE '^(IPAddressAllow|RestrictAddressFamilies=AF_INET|ReadWritePaths|RequiresMountsFor)='
    echo "ReadWritePaths=$WEB_STATE"
} >/etc/systemd/system/tg-mcp.service
chmod 0644 /etc/systemd/system/tg-core.service /etc/systemd/system/tg-mcp.service

# One socket per client, generated from config.toml. Socket owners must exist.
GENERATED="$(mktemp -d)"
UNIT_LIST="$(/opt/tg-mcp/venv/bin/python -I -B -m tg_mcp.units "$GENERATED")"
while read -r unit user group; do
    getent passwd "$user" >/dev/null || { echo "Socket owner $user for $unit does not exist" >&2; exit 1; }
    getent group "$group" >/dev/null || { echo "Socket group $group for $unit does not exist" >&2; exit 1; }
done <<<"$UNIT_LIST"
for existing in /etc/systemd/system/tg-core-*.socket; do
    [[ -e "$existing" ]] || continue
    unit="$(basename "$existing")"
    if ! grep -q "^$unit " <<<"$UNIT_LIST"; then
        systemctl disable --now "$unit" || true
        rm -f "$existing"
    fi
done
install -d -m 0755 /etc/systemd/system/tg-core.service.d
for file in "$GENERATED"/tg-core-*.socket "$GENERATED"/tg-core.service.d/sockets.conf; do
    install -m 0644 -o root -g root "$file" "/etc/systemd/system/${file#"$GENERATED"/}"
done
rm -rf "$GENERATED"

systemctl daemon-reload

# Refuse to start without the sandbox: systemd silently ignores a misplaced setting.
sandbox_broken() {
    echo "$1: $2. Not starting it without the sandbox." >&2
    exit 1
}
for unit in tg-core.service tg-mcp.service; do
    if systemd-analyze verify "/etc/systemd/system/$unit" 2>&1 | grep 'Unknown key' >&2; then
        sandbox_broken "$unit" "sandbox settings are in the wrong section"
    fi
    for setting in NoNewPrivileges=yes ProtectSystem=strict MemoryDenyWriteExecute=yes; do
        [[ "$(systemctl show -p "${setting%%=*}" --value "$unit")" == "${setting#*=}" ]] ||
            sandbox_broken "$unit" "$setting is not in effect"
    done
done
[[ -n "$(systemctl show -p IPAddressAllow --value tg-core.service)" ]] ||
    sandbox_broken tg-core.service "the Telegram-only network rule is not in effect"
systemctl enable --now "$MOUNT_UNIT"
# Sockets are recreated with the core stopped, so it never keeps stale ones.
systemctl stop tg-core.service 2>/dev/null || true
while read -r unit _; do
    systemctl enable "$unit"
    systemctl restart "$unit"
done <<<"$UNIT_LIST"
systemctl enable tg-core.service
systemctl start tg-core.service
systemctl enable --now tg-mcp.socket
systemctl try-restart tg-mcp.service

echo "==> Public HTTPS on port 443"
sed "s|__HOST__|$HOST|" "$SOURCE/deploy/tg-mcp.caddy" >/etc/caddy/conf.d/tg-mcp.caddy
IMPORT_LINE='import /etc/caddy/conf.d/*.caddy'
if [[ "$CADDY_IS_NEW" == yes ]]; then
    # Replaces the package's demo site. The admin API stays off, so use
    # "systemctl restart caddy" after changes rather than "reload".
    cat >/etc/caddy/Caddyfile <<EOF
{
	admin off
	auto_https disable_redirects
}

$IMPORT_LINE
EOF
elif ! grep -qxF "$IMPORT_LINE" /etc/caddy/Caddyfile; then
    printf '\n%s\n' "$IMPORT_LINE" >>/etc/caddy/Caddyfile
fi
caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile
systemctl enable caddy
systemctl restart caddy
if ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow 443/tcp
fi

echo
echo "Installed. Next steps (skip what is already done):"
echo "  1. tg-mcp-login   (then the core starts and stays connected)"
echo "  2. In Claude: Settings > Connectors > Add custom connector, URL: https://$HOST/mcp"
echo "  3. tg-mcp-pair, then enter the code on the page that Claude opens"
echo
echo "Ports 80 and 443 now belong to Caddy. Other projects on this server should"
echo "add their own site file to /etc/caddy/conf.d/ instead of binding those ports."
