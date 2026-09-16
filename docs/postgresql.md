# PostgreSQL deployment

Run these steps on the Ubuntu server after the repository is deployed to
`/opt/tracker`. Do not expose TCP port 5432 in the cloud firewall or UFW.

## Create the role and database

Create the application role with an interactive password prompt so the password
does not enter shell history, then create its database:

```bash
sudo -u postgres createuser --pwprompt tracker
sudo -u postgres createdb --owner=tracker tracker
```

If the role already exists, change its password interactively with
`sudo -u postgres psql` followed by `\password tracker`.

## Restrict PostgreSQL to the local machine

Set the following in the active `postgresql.conf`:

```conf
listen_addresses = 'localhost'
```

Limit the application entries in `pg_hba.conf` to loopback addresses:

```conf
host    tracker    tracker    127.0.0.1/32    scram-sha-256
host    tracker    tracker    ::1/128         scram-sha-256
```

Restart PostgreSQL and verify that it is not listening on a public address:

```bash
sudo systemctl restart postgresql
sudo ss -lntp | grep 5432
```

## Application settings and schema

Copy `.env.example` to `/opt/tracker/.env`, replace the placeholder password,
and restrict the file to its owning service account:

```bash
cd /opt/tracker
cp .env.example .env
chmod 600 .env
```

The application data layer exposes `initialize_schema(pool)`, which applies
`backend/sql/schema.sql` through the configured connection pool.
