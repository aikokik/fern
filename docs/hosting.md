# Always-on hosting

The same Docker stack runs on a small Linux server, so fern works while your Mac sleeps.
Default: a Hetzner CX22-class VPS (EU region, ~€4–5/month, Ubuntu LTS). Any VPS or a home mini PC works the same way.

**Security model:** the server has no public ports (ufw denies all inbound). It's reachable only over Tailscale.
Secrets live only in `deploy/.env` (mode 600). `data/` is backed up nightly with restic encryption.
Deploys happen only when you merge to `main`.

## Steps
1. Create the server (Ubuntu 24.04 LTS, your SSH key). Log in once as root.
2. `curl -fsSLO https://raw.githubusercontent.com/<owner>/fern/main/deploy/bootstrap-vps.sh && bash bootstrap-vps.sh <owner>/fern`
   (on a private repo, copy the file over with `scp` instead). Follow the prompts: Tailscale login, then add the printed
   **read-only deploy key** on GitHub.
3. From the Mac: `scp .env fern@<server>:fern/deploy/.env && ssh fern@<server> chmod 600 fern/deploy/.env`
4. Backups: on the server, create `fern/deploy/backup.env` (mode 600) with `RESTIC_REPOSITORY=…` (e.g. an S3/B2 bucket,
   or `sftp:you@<mac>.ts.net:fern-backups`) and `RESTIC_PASSWORD=…` (keep a copy in your password manager).
   Run `restic init` once, then `systemctl enable --now fern-backup.timer`.
5. `tailscale serve --bg https / http://127.0.0.1:8766`, then run `sudo -u fern ~fern/fern/deploy/deployer.sh` for the first deploy.
6. Point the iPhone Shortcuts and the web app at the server's `https://….ts.net` address. Stop the Mac stack.

## Check
- From outside the tailnet: `nmap <public ip>` shows no open ports.
- `systemctl list-timers | grep fern` lists the deployer (5 min) and backup (nightly).
- Restore test: `restic restore latest --target /tmp/r` contains `data/fern.db`.
