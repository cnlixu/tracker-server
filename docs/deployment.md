# Production deployment on Ubuntu 24.04

This guide deploys the repository to `/opt/tracker`. Run the commands manually
on the Ubuntu server after reviewing placeholders such as the private Git URL
and trusted client IP addresses. Development remains in the local Windows
workspace; do not edit application source directly on the server.

## 1. Network and account preparation

In the Alibaba Cloud security group, allow only the ports that are needed:

- TCP 22 from trusted administrator addresses.
- TCP 80 for the first HTTP deployment. Add 443 when TLS is configured.
- TCP 8686 from tracker device networks, or from all addresses only when the
  devices do not have predictable source ranges.
- Do not expose TCP 8000; Uvicorn listens only on `127.0.0.1`.

PostgreSQL is used locally by the application. The baseline deployment keeps
TCP 5432 private. If remote database administration is required, use an SSH
tunnel or the restricted procedure in "Optional remote PostgreSQL access"
below rather than opening 5432 to every address.

Install the required operating-system packages:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip postgresql nginx curl
```

Create an unprivileged service account. It does not need an interactive shell:

```bash
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin tracker
```

If that account already exists, do not recreate it.

## 2. Clone the private repository

Configure a read-only deploy key or another private-repository credential for
the administrator performing deployments. Replace the URL below:

```bash
sudo install -d -o "$USER" -g "$USER" -m 0755 /opt/tracker
git clone git@YOUR_GIT_HOST:YOUR_ORG/tracker.git /opt/tracker
cd /opt/tracker
```

The repository and `web/` files must remain readable by the `tracker` and
`www-data` service users. Do not put credentials in the repository.

## 3. Create the Python environment

Create the server's own virtual environment and install locked project
requirements from the checked-out revision:

```bash
cd /opt/tracker
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
deactivate
```

## 4. Initialize PostgreSQL

Create the PostgreSQL role with an interactive password prompt so the password
does not enter shell history, then create the database owned by that role:

```bash
sudo -u postgres createuser --pwprompt tracker
sudo -u postgres createdb --owner=tracker tracker
```

Apply the version-controlled initial schema. `-W` prompts for the database
password:

```bash
psql -h 127.0.0.1 -U tracker -W -d tracker \
  -f /opt/tracker/backend/sql/schema.sql
```

The schema uses `TIMESTAMPTZ`; application and database timestamps remain UTC.
Do not manually store Beijing-time strings in database columns.

## 5. Create the production environment file

Install the example as `/opt/tracker/.env`, restrict it to root and the service
group, and edit every placeholder:

```bash
cd /opt/tracker
sudo install -o root -g tracker -m 0640 .env.example .env
sudoedit /opt/tracker/.env
```

Generate the administrator Argon2 password hash and a random Session signing
secret interactively. The plain password is never written by this command:

```bash
cd /opt/tracker
.venv/bin/python scripts/generate_admin_credentials.py
```

Copy the generated values into `.env`, set the real PostgreSQL password, and
verify these production values:

```dotenv
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=tracker
DB_USER=tracker
DB_PASSWORD=replace_with_a_strong_password
TCP_HOST=0.0.0.0
TCP_PORT=8686
TCP_READ_TIMEOUT_SECONDS=600
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=generated_argon2_hash
SESSION_SECRET=generated_random_secret
AUTH_SESSION_MAX_AGE_SECONDS=28800
AUTH_COOKIE_SECURE=false
```

Keep `.env` out of Git. The systemd services read this file directly. The API
intentionally refuses to start when the administrator hash or Session secret
is missing or still contains the example placeholder.

`AUTH_COOKIE_SECURE=false` is only suitable for local testing or temporary
access restricted to a trusted source IP. Before exposing the login page to
the public Internet, configure HTTPS and change it to `true`; otherwise login
credentials and Session cookies can be intercepted in transit.

## 6. Install the systemd units

Copy the version-controlled units, reload systemd, enable startup, and start
both services:

```bash
cd /opt/tracker
sudo install -o root -g root -m 0644 deploy/tracker-tcp.service \
  /etc/systemd/system/tracker-tcp.service
sudo install -o root -g root -m 0644 deploy/tracker-api.service \
  /etc/systemd/system/tracker-api.service
sudo systemctl daemon-reload
sudo systemctl enable tracker-tcp tracker-api
sudo systemctl start tracker-tcp tracker-api
```

The TCP process listens on `0.0.0.0:8686`. The API process listens only on
`127.0.0.1:8000`; it is exposed through Nginx.

## 7. Install the Nginx site

Install the site configuration and disable the Ubuntu default-site symlink so
the tracker site handles requests to the server IP:

```bash
cd /opt/tracker
sudo install -o root -g root -m 0644 deploy/nginx-tracker.conf \
  /etc/nginx/sites-available/tracker
sudo ln -sfn /etc/nginx/sites-available/tracker \
  /etc/nginx/sites-enabled/tracker
sudo unlink /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl restart nginx
```

If the default symlink is already absent, skip the `unlink` command. Nginx
serves `/opt/tracker/web` directly and proxies only `/api/` to Uvicorn. It does
not proxy tracker TCP port 8686. Nginx uses `/api/auth/verify` to protect the
map and device-management pages, while FastAPI independently protects device
and track APIs. The login endpoint is rate limited. `/api/health` remains
public for service monitoring.

If UFW is enabled, allow SSH before enabling it, then allow HTTP and tracker
TCP traffic:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx HTTP'
sudo ufw allow 8686/tcp
sudo ufw enable
```

The Alibaba Cloud security group must allow the same intended traffic.

## 8. Verify the deployment

Make the operational scripts executable once after the initial clone, then run
the health check:

```bash
cd /opt/tracker
chmod +x scripts/deploy.sh scripts/health_check.sh
./scripts/health_check.sh
```

Useful individual checks are:

```bash
sudo systemctl status tracker-tcp tracker-api nginx --no-pager
sudo ss -lntp | grep -E ':(80|8000|8686)\b'
curl --fail http://127.0.0.1:8000/api/health
curl --fail http://127.0.0.1/api/health
```

From another machine, open `http://SERVER_PUBLIC_IP/`. An unauthenticated
request must redirect to `/login.html`. Log in, verify the track map and device
management page, then log out and confirm protected pages are no longer
accessible. Test TCP 8686 separately with an actual tracker or an approved test
client; Web authentication does not change the PTRK protocol.

Inspect service logs without exposing `.env` contents:

```bash
sudo journalctl -u tracker-tcp -n 100 --no-pager
sudo journalctl -u tracker-api -n 100 --no-pager
sudo journalctl -u nginx -n 100 --no-pager
sudo journalctl -u tracker-tcp -f
```

## 9. Deploy later revisions

Commit and push reviewed changes from the local development machine. On the
server, run the deployment script as the user that owns the Git checkout:

```bash
cd /opt/tracker
./scripts/deploy.sh
```

The script performs only a fast-forward Git pull, updates packages inside the
existing venv, and restarts `tracker-tcp` and `tracker-api`. It prints recent
service logs when either service fails. It does not reset the working tree,
delete `.env`, or modify production data. The invoking account needs `sudo`
permission for `systemctl` and `journalctl` unless it is root.

## Optional remote PostgreSQL access

The application itself should continue using `DB_HOST=127.0.0.1`. If a remote
administration client must connect over the public network, restrict access to
one trusted public client address and require password authentication and TLS.

In the active `postgresql.conf`, set an appropriate listen address and keep
SSL enabled. In `pg_hba.conf`, add a rule with the administrator's actual
public address, never `0.0.0.0/0`:

```conf
hostssl    tracker    tracker    TRUSTED_CLIENT_PUBLIC_IP/32    scram-sha-256
```

Restart PostgreSQL only after validating its configuration. In both UFW and
the Alibaba Cloud security group, allow TCP 5432 only from that same `/32`
address. Prefer an SSH tunnel when the client IP changes. Never place the
database password in a command, URL, Git file, or shell history.

## Troubleshooting

If a service does not start, check permissions on `/opt/tracker/.env`, verify
that PostgreSQL accepts the configured credentials, and inspect its journal:

```bash
sudo -u tracker test -r /opt/tracker/.env
sudo -u tracker test -x /opt/tracker/.venv/bin/python
sudo journalctl -u tracker-tcp -n 50 --no-pager
sudo journalctl -u tracker-api -n 50 --no-pager
sudo nginx -t
```

Do not resolve deployment failures with `git reset --hard`, by deleting the
venv or `.env`, or by recreating the production database.
