# Linux operations

Use a dedicated VM, local disk for SQLite, HTTPS reverse proxy, firewall, and encrypted backups. Do not place SQLite on NFS. Do not expose port 8080 publicly; bind to loopback and use the supplied Caddy example. The API intentionally has no cookie auth or permissive CORS.

Install the checkout and locked virtualenv at `/opt/software-factory`. Configure `FACTORY_DATA=/var/lib/software-factory` and `FACTORY_CONFIG=/etc/software-factory/config.json`. Store tokens and provider credentials in a mode-0600 environment file referenced by systemd. Use separate deployment credentials and accounts; build workers must not receive production SSH keys. For a first single-user setup the processes may share an account, but this does not constitute OS-level privilege separation. Before production, separate the release account and grant only required access to artifacts and SQLite; validate directory and WAL permissions. The example service is a baseline, not an unattended installer.

Start API, build lane and deploy lane as separate services. Never start multiple API installations against the same data path on different machines. Restart services after config changes. `factory doctor` checks installed/configured components; it does not authenticate to the model provider or certify application readiness.

## Verification profiles

Profiles are administrator-controlled. Images must be digest pinned, pre-pulled, and contain Python 3 plus any application toolchain/dependencies. Verification has no network access. Preinstall dependencies in an audited image; do not enable network to make arbitrary generated install scripts work. Checks execute in `/work/app`, see source at `/source` read-only and checks at `/checks` read-only. They must export deployable files to `/output`. The legacy `artifact_dir` profile field is unused; `/output` is always the export contract. Add application-specific acceptance tests, not just build checks. The included static check is structural, not browser, accessibility, security or business acceptance certification.

## Deployment target

Prepare a separate unprivileged account and dedicated root per application/environment. Install `deploy/sf-release` as root-owned executable `/usr/local/bin/sf-release`. Write root-owned `/etc/software-factory-release.json`, for example:

```json
{"root":"/srv/portfolio","health_url":"http://127.0.0.1:8081/","restart_argv":[],"health_attempts":15}
```

Serve `/srv/portfolio/current` with your web server. For a server application, preconfigure a fixed service and a narrowly authorized restart command in `restart_argv`. Keep database and uploads outside release directories. Do not grant generic sudo access. Provision health endpoints and TLS before releasing. The helper checks HTTP 200; add stronger application-specific health tests where required.

In factory config, add targets under `PROJECT_ID:staging` and `PROJECT_ID:production`:

```json
{"host":"release@app.example.com","identity_file":"/secure/release_key","known_hosts":"/secure/known_hosts","helper":"/usr/local/bin/sf-release","port":22}
```

Verify SSH host keys through a trusted channel. Keys belong to the deployment account only. Every environment needs its own app root/account configuration. Staging must use a representative runtime and safe data. Deploy staging from the dashboard, perform human acceptance, then approve the exact digest using the release token.

If SSH is interrupted, inspect the remote `release.json`. Run `sf-release status` on the target. If the journal says `deploying` or `rollback_failed`, investigate and explicitly run `sf-release recover` to restore the previous release. Then run `factory reconcile DEPLOYMENT_ID` from the controller. An unknown first deployment with no previous healthy version requires operator incident handling; the CLI deliberately refuses to invent a successful reconciliation.

## Backup and restore

`factory backup /safe/path/factory.sqlite3` creates a consistent online SQLite snapshot. For a complete backup, stop both schedulers, copy the verified SQLite snapshot plus the entire jobs and memory directories and configuration, encrypt the backup, and store it off-host. Back up secrets separately. Test restoration into an isolated instance: integrity check, project/job history, artifact digest verification, and memory access. Never point a restore drill at production targets. Restore files with correct ownership before starting workers. Interrupted jobs require inspection and explicit approval to retry.

Monitor worker heartbeat age, failed/unknown jobs, memory failures, disk use and backup age. Audit events contain project data; restrict access. Retention is manual in this release: never remove artifacts referenced by active releases, investigations or restore points. Keep disk-space alerts enabled.

## Production acceptance still required

Live Pi generation/review with the selected provider; live Memorix roundtrip; HTTPS and token rotation; actual staging/production SSH deployment; service restart, health failure and rollback; server reboot/interrupted-release recovery; full backup restoration; application-specific browser/security/business tests. No remote production deployment was performed during implementation.
