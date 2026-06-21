# GitHub Actions Deploy

Manual, gated deployment to the Oracle Cloud server via SSH.
Workflow: [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)

## How it runs

1. GitHub → **Actions** → **Deploy to Oracle Cloud** → **Run workflow**.
2. Select **DEPLOY** in the confirmation dropdown (anything else aborts) and pick which services to restart.
3. (If the `production` Environment has required reviewers) a reviewer must **Approve**.
4. The runner SSHes in and runs: `git pull --ff-only` → `systemctl restart <services>` → `systemctl is-active` check.

It **never deploys automatically** — manual trigger + confirm is the gate.

## One-time setup

### 1. Generate a dedicated deploy keypair (locally)

```bash
ssh-keygen -t ed25519 -f ./gh_deploy_key -N "" -C "github-actions-deploy"
```

This creates `gh_deploy_key` (private) and `gh_deploy_key.pub` (public).

### 2. Put the PUBLIC key on the server

```bash
ssh -i emcurekey ubuntu@<SERVER_IP>
echo "<contents of gh_deploy_key.pub>" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

### 3. Store the PRIVATE key + host as GitHub secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|--------|-------|
| `SSH_PRIVATE_KEY` | full contents of `gh_deploy_key` (incl. BEGIN/END lines) |
| `SSH_HOST` | `<SERVER_IP>` |
| `SSH_USER` | `ubuntu` |
| `SSH_PORT` | `22` (optional; defaults to 22) |

Then delete the local `gh_deploy_key` files.

### 4. Allow passwordless sudo for the deploy commands

Non-interactive SSH can't answer a sudo password prompt, so the deploy user needs `NOPASSWD` for exactly the commands used. On the server:

```bash
sudo tee /etc/sudoers.d/emcure-deploy >/dev/null <<'EOF'
ubuntu ALL=(root) NOPASSWD: /usr/bin/git -C /opt/emcure pull --ff-only, /usr/bin/git pull --ff-only, /bin/systemctl restart emcure-tracker, /bin/systemctl restart crypto-tracker, /bin/systemctl restart emcure-bot, /bin/systemctl restart emcure-tracker crypto-tracker emcure-bot
sudo visudo -c   # validate
EOF
```

> If `/opt/emcure` is owned by `ubuntu`, you can drop the `sudo` on `git pull` and only need NOPASSWD for the `systemctl restart` lines. Adjust `systemctl` path (`/bin` vs `/usr/bin`) if `which systemctl` differs.

### 5. (Optional but recommended) approval gate

Repo → **Settings → Environments → New environment → `production`** → enable **Required reviewers** and add yourself. The deploy then pauses for a manual Approve click.

## Security notes

- The private key lives only in GitHub secrets and is written to the runner, used, and deleted (`Cleanup key` step).
- Scope the `NOPASSWD` sudoers entry to just these commands — never `NOPASSWD: ALL`.
- Rotate the deploy key if it's ever exposed (regenerate, update `authorized_keys` + the secret).
