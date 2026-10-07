#!/usr/bin/env bash
# One-time, idempotent setup of an Ubuntu LTS server for fern. Run as root (download first; it asks for input):
#   curl -fsSLO <raw url of this file> && bash bootstrap-vps.sh <github-owner>/fern
# Result: no public ports (ufw denies all inbound), reachable only over Tailscale; deploy-on-merge + nightly backups.
set -euo pipefail
REPO=${1:?usage: bootstrap-vps.sh <owner>/fern}

apt-get update -q
apt-get install -yq docker.io docker-compose-v2 git ufw unattended-upgrades restic curl
dpkg-reconfigure -f noninteractive unattended-upgrades

# Tailscale (the only way in). You'll be asked to log in once via the printed URL.
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh

# Firewall: deny everything inbound except over the tailnet interface.
ufw default deny incoming
ufw default allow outgoing
ufw allow in on tailscale0
ufw --force enable
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && systemctl reload ssh || true

# Unprivileged deploy user with a read-only GitHub deploy key.
id fern >/dev/null 2>&1 || useradd -m -s /bin/bash -G docker fern
sudo -u fern bash -c '
  [ -f ~/.ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/id_ed25519
  ssh-keyscan -q github.com >> ~/.ssh/known_hosts 2>/dev/null'
if [ ! -d /home/fern/fern ]; then
  echo; echo "Add this as a READ-ONLY deploy key at https://github.com/$REPO/settings/keys, then press Enter:"
  cat /home/fern/.ssh/id_ed25519.pub; read -r _ < /dev/tty
  sudo -u fern git clone -q "git@github.com:$REPO.git" /home/fern/fern
fi

# Containers run as uid 10001 (read-only root fs); data/ is their only writable bind mount.
install -d -o 10001 -g 10001 -m 700 /home/fern/fern/data

install -m 644 /home/fern/fern/deploy/fern-{deployer,backup}.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now fern-deployer.timer
[ -f /home/fern/fern/deploy/backup.env ] && systemctl enable --now fern-backup.timer

cat <<EOF

Done. Next (from your Mac, over Tailscale):
  1. scp .env fern@<this-host>:fern/deploy/.env && ssh fern@<this-host> chmod 600 fern/deploy/.env
  2. Backups: create fern/deploy/backup.env with RESTIC_REPOSITORY=... and RESTIC_PASSWORD=..., chmod 600,
     run 'restic init' once as fern, then: systemctl enable --now fern-backup.timer
  3. HTTPS for the phone: tailscale serve --bg https / http://127.0.0.1:8766
  4. First deploy: sudo -u fern /home/fern/fern/deploy/deployer.sh   (later merges deploy automatically)
EOF
