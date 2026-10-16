#!/usr/bin/env bash
# Prepare a pinned release and configuration. Never starts/reloads a service.
set -euo pipefail
umask 077

fail() { printf '%s\n' "$1" >&2; exit 1; }
usage() {
    cat <<'USAGE'
Usage: deploy/install.sh --source CHECKOUT --revision FULL_COMMIT_SHA \
  --python ABSOLUTE_PYTHON \
  --schema-revision ALEMBIC_HEAD --lan-name DNS_NAME \
  --lan-address RFC1918_IPV4 --lan-cidr RFC1918_CIDR \
  --tls-certificate ABSOLUTE_CERT_PATH --tls-key ABSOLUTE_KEY_PATH
Requires root, Linux/systemd, npm on PATH, and pinned direct dependencies in pyproject.toml.
Retains pip freeze in the release; first-resolution transitives are NOT hash-locked.
Builds as futures-build, runs as futures-bot.
Does not migrate, bootstrap credentials, enable/start/reload services, or use SSH.
USAGE
}

source_checkout= revision= python= schema_revision=20261016_0006
lan_name= lan_address= lan_cidr= tls_certificate= tls_key=
while (($#)); do
    if [[ $1 == --help ]]; then usage; exit 0; fi
    (($# >= 2)) || fail "Every option requires a value."
    case "$1" in
        --source) source_checkout=$2 ;;
        --revision) revision=$2 ;;
        --python) python=$2 ;;
        --schema-revision) schema_revision=$2 ;;
        --lan-name) lan_name=$2 ;;
        --lan-address) lan_address=$2 ;;
        --lan-cidr) lan_cidr=$2 ;;
        --tls-certificate) tls_certificate=$2 ;;
        --tls-key) tls_key=$2 ;;
        *) fail "Unknown option; use --help." ;;
    esac
    shift 2
done
[[ $EUID -eq 0 ]] || fail "Run the installer as root on the deployment host."
[[ $source_checkout == /* && -d $source_checkout ]] || fail "Checkout must be an absolute directory."
[[ $revision =~ ^[a-f0-9]{40}$ ]] || fail "A full immutable Git commit SHA is required."
[[ $python =~ ^/[A-Za-z0-9_./-]+$ && -x $python ]] || fail "Invalid Python executable path."
[[ $schema_revision =~ ^[A-Za-z0-9_.-]{1,255}$ ]] || fail "Invalid schema revision."
[[ $lan_name =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || fail "Invalid LAN DNS name."
for path in "$tls_certificate" "$tls_key"; do
    [[ $path =~ ^/[A-Za-z0-9_./-]+$ ]] || fail "TLS settings must be absolute file paths."
done

ipv4_number() {
    local ip=$1 a b c d
    [[ $ip =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || fail "Invalid IPv4 address."
    IFS=. read -r a b c d <<< "$ip"
    for octet in "$a" "$b" "$c" "$d"; do
        ((10#$octet <= 255)) || fail "Invalid IPv4 octet."
    done
    printf '%s' "$(((10#$a << 24) | (10#$b << 16) | (10#$c << 8) | 10#$d))"
}
private_prefix() {
    local address=$1 second
    case "$address" in
        10.*) printf 8 ;;
        192.168.*) printf 16 ;;
        172.*)
            second=${address#172.}
            second=${second%%.*}
            ((10#$second >= 16 && 10#$second <= 31)) || fail "LAN must be RFC1918."
            printf 12 ;;
        *) fail "LAN must be RFC1918." ;;
    esac
}
[[ $lan_cidr =~ ^[0-9.]+/[0-9]{1,2}$ ]] || fail "Invalid LAN CIDR."
address_number=$(ipv4_number "$lan_address")
network=${lan_cidr%/*}
network_number=$(ipv4_number "$network")
prefix=${lan_cidr#*/}
minimum_prefix=$(private_prefix "$network")
private_prefix "$lan_address" > /dev/null
((10#$prefix >= minimum_prefix && 10#$prefix <= 32)) || fail "CIDR extends outside private LAN."
mask=$(((0xffffffff << (32 - 10#$prefix)) & 0xffffffff))
(( (network_number & mask) == network_number )) || fail "CIDR must use the network address."
(( (address_number & mask) == network_number )) || fail "LAN address is outside allowed CIDR."

for command in git tar install sha256sum useradd runuser npm; do
    command -v "$command" > /dev/null || fail "A required installation tool is missing."
done
[[ $(git -C "$source_checkout" rev-parse --verify "$revision^{commit}") == "$revision" ]] \
    || fail "Requested commit is unavailable."
python_hash=$(sha256sum "$python" | cut -d ' ' -f 1)
fingerprint="$revision:$python_hash:pinned-direct-freeze-v1"
app_root="/opt/futures-platform/releases/$revision"

getent passwd futures-bot > /dev/null \
    || useradd --system --user-group --home-dir /var/lib/futures-platform \
        --shell /usr/sbin/nologin futures-bot
getent passwd futures-build > /dev/null \
    || useradd --system --user-group --home-dir /var/cache/futures-build \
        --shell /usr/sbin/nologin futures-build
[[ $(id -u futures-bot) != 0 && $(id -u futures-build) != 0 ]] \
    || fail "Service/build accounts must not be privileged."
install -d -o root -g root -m 0755 /opt/futures-platform /opt/futures-platform/releases
install -d -o futures-build -g futures-build -m 0700 /var/cache/futures-build
install -d -o futures-bot -g futures-bot -m 0700 \
    /var/lib/futures-platform /var/backups/futures-platform
install -d -o root -g futures-bot -m 0750 /etc/futures-platform

build_as_user() {
    runuser -u futures-build -- env -i HOME=/var/cache/futures-build \
        PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
        npm_config_userconfig=/dev/null npm_config_cache=/var/cache/futures-build/npm "$@"
}
if [[ -e $app_root ]]; then
    [[ ! -L $app_root && -f $app_root/.build-fingerprint ]] \
        || fail "Refusing to reuse an unrecognized release directory."
    [[ $(cat "$app_root/.build-fingerprint") == "$fingerprint" ]] \
        || fail "Existing release has a different installation policy or interpreter."
else
    install -d -o futures-build -g futures-build -m 0755 "$app_root"
    git -C "$source_checkout" archive "$revision" \
        src frontend pyproject.toml README.md alembic alembic.ini deploy \
        | tar --extract --no-same-owner --no-same-permissions --directory "$app_root"
    printf '%s\n' "$fingerprint" > "$app_root/.build-fingerprint"
fi
if [[ ! -f $app_root/.installed ]]; then
    [[ -f $app_root/frontend/package-lock.json ]] || fail "npm lockfile is required."
    if grep -Eq '://[^ /"]*@' "$app_root/frontend/package-lock.json"; then
        fail "npm lockfile must not contain credential-bearing URLs."
    fi
    chown -R futures-build:futures-build "$app_root"
    build_release() {
        build_as_user "$python" -m venv "$app_root/.venv" || return
        # All project direct dependencies must be exact public package pins.
        build_as_user "$app_root/.venv/bin/python" \
            "$app_root/deploy/validate_dependencies.py" "$app_root/pyproject.toml" || return
        if [[ -f $app_root/deploy/requirements.resolved.txt ]]; then
            build_as_user "$app_root/.venv/bin/python" -m pip --isolated install \
                --no-deps -r "$app_root/deploy/requirements.resolved.txt" || return
        fi
        build_as_user "$app_root/.venv/bin/python" -m pip --isolated install \
            "$app_root" || return
        build_as_user "$app_root/.venv/bin/python" -m pip --isolated check || return
        build_as_user "$app_root/.venv/bin/python" -m pip --isolated freeze \
            --all --exclude binance-futures-platform \
            > "$app_root/deploy/requirements.resolved.txt.pending" || return
        mv -T "$app_root/deploy/requirements.resolved.txt.pending" \
            "$app_root/deploy/requirements.resolved.txt" || return
        (cd "$app_root/frontend" && build_as_user npm ci --ignore-scripts --no-audit --no-fund \
            && build_as_user npm run build)
    }
    if ! build_release > "$app_root/build.log" 2>&1; then
        chmod 0600 "$app_root/build.log"
        fail "Build failed; inspect the release's owner-only build.log. No service was started."
    fi
    chmod 0600 "$app_root/build.log"
    printf '%s\n' "$fingerprint" > "$app_root/.installed"
fi

render() {
    local staged="${2}.pending.$$"
    [[ ! -L $2 ]] || fail "Refusing a symlink configuration destination."
    (set -o noclobber; : > "$staged") || fail "Configuration staging path already exists."
    if ! sed -e "s|@APP_ROOT@|$app_root|g" \
        -e "s|@SCHEMA_REVISION@|$schema_revision|g" \
        -e "s|@LAN_NAME@|$lan_name|g" \
        -e "s|@LAN_ADDRESS@|$lan_address|g" \
        -e "s|@LAN_CIDR@|$lan_cidr|g" \
        -e "s|@TLS_CERTIFICATE@|$tls_certificate|g" \
        -e "s|@TLS_KEY@|$tls_key|g" "$1" > "$staged"; then
        rm -f -- "$staged"
        fail "Configuration rendering failed."
    fi
    chmod 0644 "$staged"
    mv -T -- "$staged" "$2"
}
render "$app_root/deploy/backup.sh.in" "$app_root/deploy/backup.sh"
for unit in futures-platform.service futures-paper-worker.service futures-backup.service \
    futures-backup.timer; do
    render "$app_root/deploy/systemd/$unit.in" "/etc/systemd/system/$unit"
done
install -d -o root -g root -m 0755 /etc/systemd/system/nginx.service.d
render "$app_root/deploy/systemd/nginx-network.conf.in" \
    /etc/systemd/system/nginx.service.d/futures-platform.conf
render "$app_root/deploy/runtime.env.in" /etc/futures-platform/runtime.env
[[ ! -L /etc/futures-platform/runtime.env ]] || fail "Runtime environment must not be a symlink."
chown root:futures-bot /etc/futures-platform/runtime.env
chmod 0640 /etc/futures-platform/runtime.env
install -d -o root -g root -m 0755 /etc/nginx/conf.d
render "$app_root/deploy/nginx/futures-platform.conf.in" \
    /etc/nginx/conf.d/futures-platform.conf
chown -R root:root "$app_root"
find "$app_root" -type d -exec chmod 0755 {} +
find "$app_root" -type f ! -name build.log -exec chmod a+r {} +
printf '%s\n' "Release prepared. Migration, owner bootstrap, nginx validation and activation remain explicit."
