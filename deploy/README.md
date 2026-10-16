# Paper-only production operations

Nothing in this directory is automatically executed. `install.sh` prepares an
immutable-commit release, builds a fresh venv and `npm ci`/`npm run build`, and
renders files without starting, enabling, restarting or reloading services. It
never reads a `.env`, copies `.local`, connects through SSH, creates an exchange
key, or enables live trading. App and worker commands forcibly select paper
mode/testnet even if the runtime environment file is edited.

## Prepare a deployment host

Prerequisites: Linux/systemd, Git, GNU coreutils/tar, npm/Node supported by the
frontend lock, nginx, and a Python interpreter supported by `pyproject.toml`.
The repository has exact direct runtime pins in `pyproject.toml`, but **no complete
transitive/hash-pinned Python lock**. The installer validates those direct pins,
uses `pip install RELEASE`, runs `pip check`, then retains
`deploy/requirements.resolved.txt` from `pip freeze --all` (excluding the local
application). A retry after freezing installs that exact resolved package set
before installing the app. Completed releases are reused without re-resolving.
This makes the resolved runtime versions auditable/replayable, not hash-verified
or bit-for-bit reproducible: initial transitive resolution and isolated build
dependencies can change, and replay requires compatible artifacts still available
from the configured public index. Retain the release/freeze and record Python,
OS, Node/npm versions. Do not call this a hash lock. No dependency/config secrets
belong in the checkout.

Replace all capitalized argument values before executing:

```sh
sudo bash deploy/install.sh \
  --source /srv/APPROVED_CHECKOUT \
  --revision FULL_40_CHARACTER_COMMIT_SHA \
  --python /usr/bin/python3.14 \
  --schema-revision 20261016_0006 \
  --lan-name binancebot.albotero.com \
  --lan-address IPAM_RESERVED_PRIVATE_IPV4 \
  --lan-cidr APPROVED_PRIVATE_LAN_CIDR \
  --tls-certificate /etc/ssl/certs/binancebot.albotero.com.pem \
  --tls-key /etc/ssl/private/binancebot.albotero.com.key
```

Only committed `src`, `frontend`, package/build metadata, migrations, and `deploy`
are archived. A repeated successful install with the same fingerprint reuses the
release; a changed installer policy/interpreter for the same commit is refused.
Build commands run with an empty environment as `futures-build`, not root or the
runtime user. Build logs are owner-only in that release. Runtime code becomes
root-owned; `futures-bot` can write only its private state directory. Backups run
under the same UID but in a separate read-only-state, network-disabled service.
No retention/deletion or off-site upload is automatic.
Confirm the assigned address/subnet and LAN restrictions before activation; do
not substitute an arbitrary IP or modify unrelated DNS/firewall configuration.
The public-looking DNS name does not authorize
public exposure; nginx remains bound and allowlisted to the approved private LAN.

Each `.in` file has explicit `@PLACEHOLDERS@`; do not install it verbatim.
The installer renders systemd units, `/etc/nginx/conf.d/futures-platform.conf`,
and the complete secretless `/etc/futures-platform/runtime.env` on every run,
including the current release's frontend path. Keep customizations in reviewed
templates rather than editing generated files. TLS key contents are never templated or
printed. Provision a LAN-trusted certificate/key separately; key readability must
be restricted to nginx's privileged master process. Bind only the private
interface, retain firewall LAN restrictions, and do not forward this port publicly.

### Private-IP TLS without a DNS record

If the operator explicitly accepts a self-signed certificate, use the assigned
private address for both `--lan-name` and `--lan-address`. This renders the
matching HTTPS origin and nginx server name; a DNS record is unnecessary.
Generate a certificate on the guest with an **IP subject alternative name**,
not just a matching common name. For example, substituting the assigned address:

```sh
sudo install -d -o root -g root -m 0700 /etc/futures-platform/tls
sudo sh -c 'umask 077; openssl req -x509 -newkey rsa:3072 -sha256 \
  -days 365 -nodes \
  -keyout /etc/futures-platform/tls/server.key \
  -out /etc/futures-platform/tls/server.crt \
  -subj /CN=172.17.20.121 \
  -addext subjectAltName=IP:172.17.20.121 \
  -addext extendedKeyUsage=serverAuth'
```

Point the installer at these files. Record the SHA256 certificate fingerprint
and expiry through a trusted management channel. Export only the **public
certificate**, never the private key, to clients that need to trust it.
Browser trust must be configured or a certificate warning will appear.
For automated acceptance, use `curl --cacert PUBLIC_CERTIFICATE
https://ASSIGNED_ADDRESS/ready`; do not use `curl -k` as certificate validation.
Self-signed TLS is encrypted but is not publicly trusted, and requires manual
renewal/client trust updates. It does not authorize Internet exposure.

An unprivileged Debian 13 LXC may require Proxmox `nesting=1` for systemd mount
namespaces. Verify startup mount failures and the hardened units on the target;
if necessary, enable that feature on the existing container and reboot it.
Do not make the container privileged or disable service hardening to bypass
namespace errors.

The installer adds an nginx systemd drop-in ordering it after Debian
`networking.service`, with a five-second failure retry. A dependency on
`network-online.target` alone did not wait for ifupdown2 address assignment in
target acceptance: binding the private address failed during reboot. Keep the
explicit ordering and verify a full container reboot, not only an nginx restart.

## Integration and explicit activation

The app runs `python -m futures_bot` on `127.0.0.1:8000`; the worker runs
`python -m futures_bot.execution.worker` and is pure paper. The actual settings are
`BOT_DASHBOARD_ENABLED=true`,
`BOT_PUBLIC_ORIGIN=https://binancebot.albotero.com`, `BOT_SECURE_COOKIES=true`,
`BOT_FRONTEND_DIRECTORY=/absolute/release/frontend/dist`, and
`BOT_SESSION_LIFETIME_SECONDS=28800`. The installer renders the origin from
`--lan-name` and the frontend directory from the immutable release path.
Apply all release migrations through the current head `20261016_0006`.
For unattended owner bootstrap, provision a UTF-8 password file separately at
`/run/futures-platform/owner-password`, owned by `futures-bot`, mode 0600,
regular/singly linked, with no final symlink. The bootstrap reads
`--password-file` without prompting or printing the password. Never pass the
password itself in argv, logs, or the secretless environment file. Remove the
file through your secret provisioning system after successful bootstrap.
Omitting `--password-file` provides interactive `getpass` confirmation instead.

Set `RELEASE=/opt/futures-platform/releases/FULL_40_CHARACTER_COMMIT_SHA` in your
operator shell. These are separate, deliberate deployment commands:

```sh
sudo systemctl stop futures-backup.timer futures-backup.service \
  futures-paper-worker.service futures-platform.service
# For an existing database, take and verify a backup BEFORE migrating (below).
sudo -u futures-bot sh -c '
  umask 077 &&
  cd "$1" &&
  BOT_DATABASE_URL=sqlite:////var/lib/futures-platform/platform.sqlite3 \
    .venv/bin/alembic -c alembic.ini upgrade head
' sh "$RELEASE"
sudo -u futures-bot "$RELEASE/.venv/bin/python" -m futures_bot.operations verify \
  --database /var/lib/futures-platform/platform.sqlite3 \
  --expected-revision 20261016_0006
sudo -u futures-bot "$RELEASE/.venv/bin/python" -m futures_bot.web.bootstrap \
  --database-url sqlite:////var/lib/futures-platform/platform.sqlite3 \
  --username OWNER --password-file /run/futures-platform/owner-password
sudo nginx -t
sudo systemd-analyze verify /etc/systemd/system/futures-platform.service \
  /etc/systemd/system/futures-paper-worker.service \
  /etc/systemd/system/futures-backup.service /etc/systemd/system/futures-backup.timer \
  nginx.service
sudo systemctl daemon-reload
sudo systemctl enable --now futures-platform.service futures-paper-worker.service \
  futures-backup.timer
sudo systemctl reload nginx
sudo systemctl is-active futures-platform.service futures-paper-worker.service
curl --fail --silent --show-error https://binancebot.albotero.com/
```

Allow startup to complete before checking readiness (a successful
`systemctl start` alone does not prove HTTP responsiveness). Check through the
HTTPS origin, or supply its exact Host header when probing loopback: Host
validation intentionally rejects an unconfigured `127.0.0.1` host. Verify the
database and any WAL/SHM files remain private to the service account; the state
directory must stay mode 0700. Do not weaken origin checks for health probes.

Run owner bootstrap only for the first installation; it intentionally refuses to
replace existing credentials. First installation must handle absent units rather
than masking unrelated `systemctl stop` failures. Run Alembic from the release
directory if required by the deployed `alembic.ini` script path. Set up monitoring
for unit failures and backup disk usage before relying on the timer.

### Bootstrap from an existing owner-only local file

After installing the release and migrating, the following local commands use an
already-provisioned `/secure/owner-password` file (replace that path with your
real local file path). They never place password contents in argv or environment,
never print them, and do not use remote access. The source file must already be
owner-private; no password generation or secret provisioning is automatic.

```sh
RELEASE=/opt/futures-platform/releases/FULL_40_CHARACTER_COMMIT_SHA
sudo install -d -o futures-bot -g futures-bot -m 0700 /run/futures-platform
sudo install -o futures-bot -g futures-bot -m 0600 \
  /secure/owner-password /run/futures-platform/owner-password
sudo -u futures-bot "$RELEASE/.venv/bin/python" -m futures_bot.operations verify \
  --database /var/lib/futures-platform/platform.sqlite3 \
  --expected-revision 20261016_0006
sudo -u futures-bot "$RELEASE/.venv/bin/python" -m futures_bot.web.bootstrap \
  --database-url sqlite:////var/lib/futures-platform/platform.sqlite3 \
  --username owner --password-file /run/futures-platform/owner-password
# Only after successful bootstrap:
sudo rm -- /run/futures-platform/owner-password
```

Retain/protect the source password file according to your credential provisioning
policy. Bootstrap is a one-time operation, not an idempotent credential reset.
It rejects wrong ownership, symlinks, multiple links, or group/other permissions.

To replay the retained runtime package set into a compatible fresh venv, use
`pip install --no-deps -r RELEASE/deploy/requirements.resolved.txt`, then
`pip install --no-deps RELEASE` and `pip check` through that venv's Python.
The application must still come from the same approved commit. This does not
retroactively pin isolated build dependencies or verify package hashes.

## Online backups and verification

The operational CLI uses **filesystem paths**, never database URLs. Parent
directories must already exist. Snapshots use SQLite's online backup API, include
every table/index/view/trigger and committed WAL data, preserve Alembic revision
metadata, and check both `integrity_check` and `foreign_key_check`. Output is 0600,
fsynced, and atomically published without overwriting an existing file. Source
writes may continue; uncommitted transactions are excluded.
Destination directories must belong to the invoking user and must not be
group/world-writable; run as `futures-bot` for the installed state/backup paths.

```sh
sudo -u futures-bot "$RELEASE/.venv/bin/python" -m futures_bot.operations backup \
  --database /var/lib/futures-platform/platform.sqlite3 \
  --output /var/backups/futures-platform/BEFORE_UPGRADE.sqlite3 \
  --expected-revision OLD_ALEMBIC_HEAD
sudo -u futures-bot "$RELEASE/.venv/bin/python" -m futures_bot.operations verify \
  --database /var/backups/futures-platform/BEFORE_UPGRADE.sqlite3 \
  --expected-revision OLD_ALEMBIC_HEAD
sudo systemctl start futures-backup.service
sudo journalctl -u futures-backup.service --no-pager -n 20
```

`--expected-revision` is repeatable for multiple Alembic heads; its exact set must
match the database. Without it, verification still requires valid, nonempty,
unique Alembic revision rows but does **not** claim compatibility with current
application code. Backup failure never falls back to copying the main file.
Backups contain password/session hashes and private business data: retain their
0600 permissions and use an independently approved encrypted off-site process.

## Restore / rollback rehearsal

Rehearse on a separate destination first. Stop the app, worker, timer, any running
backup, and **all** other SQLite users for a real restore. The two restore flags
are mandatory operator assertions, not proof that systemd/other hosts are stopped.

```sh
sudo -u futures-bot "$RELEASE/.venv/bin/python" -m futures_bot.operations restore \
  --database /var/lib/futures-platform/restore-rehearsal.sqlite3 \
  --backup /var/backups/futures-platform/BEFORE_UPGRADE.sqlite3 \
  --expected-revision OLD_ALEMBIC_HEAD --service-stopped --confirm-restore
sudo -u futures-bot "$RELEASE/.venv/bin/python" -m futures_bot.operations verify \
  --database /var/lib/futures-platform/restore-rehearsal.sqlite3 \
  --expected-revision OLD_ALEMBIC_HEAD
```

For the real restore, replace the rehearsal destination with `platform.sqlite3`
only after all users are stopped. Restore verifies a staged standalone snapshot
before atomically replacing the destination, preserving the old destination on
validation/staging failure. A power/I/O failure after publication can mean a
complete replacement exists even when the command reports failure: re-verify
before restarting anything.

Any existing destination `-wal`, `-shm`, or `-journal` causes refusal. Do not delete
them manually: they may contain committed data. If services have exited but a WAL
remains, use the old compatible application/SQLite tooling to checkpoint and close
the destination cleanly while all other users remain stopped, then retry. Never
use `immutable=1` against an active WAL database or copy only its main file.

For rollback, select the old root-owned application release/systemd templates and
old schema expectation along with the old backup; never start new code against an
unverified old schema. Re-enable app, worker and backup timer only after verification.
