#!/usr/bin/env bash
# Upgrade (or roll back) one compose stack to an exact commit (docs/release.md §4.3,
# docs/operations.md §5). Normally run by `python -m scripts.release deploy`,
# which pipes the copy of this script from the target commit to the host:
#
#   cd /opt/akkiabot/staging && bash -s -- <sha> --migrations unchanged < upgrade.sh
#
# Usage: upgrade.sh <40-char sha> --migrations changed|unchanged
#                   [--allow-migration] [--release <label>] [--render-template <path in deploy/>]
#
# Steps; nothing on the stack changes before step 6, so an early failure needs
# no clean-up and the same command can simply be run again:
#   1. one upgrade at a time (flock), idempotent when the stack already runs <sha> and is healthy
#   2. fetch compose.yaml, observability/config.alloy, backup.sh, restore.sh and the
#      config examples of <sha>; optionally render the app.env template (1Password)
#   3. configuration check: every key of the new examples exists in .env / app.env
#   4. migration gate: a release that changes migrations needs --allow-migration
#      (the owner's confirmation); the database is never restored automatically
#   5. pull the new image (with the old compose files still in place)
#   6. backup.sh, snapshot of the current files, install the new ones, up --wait
#   7. health: /readyz on the loopback port
#   8. failure after step 6: without migrations, restore the snapshot and start the
#      old version again (exit 7); with migrations, stop and report (exit 6, §5.2)
# Every attempt is appended to releases.log (mode 600, server only).
#
# Exit codes: 0 ok, 2 usage, 3 refused by a gate, 4 preparation failed (nothing
# changed), 5 backup failed (nothing changed), 6 failed after a migration (manual),
# 7 failed and rolled back, 8 failed and the rollback failed too, 75 busy.
set -euo pipefail

# The script normally arrives on stdin (`bash -s`). Bash reads such a script as
# it goes, so a command that reads stdin (op inject does) would swallow the rest
# of it and the script would end silently with status 0 half way through (BOT-40
# staging rehearsal). Hence the whole body is one function, parsed completely
# before anything runs, and it runs with stdin from /dev/null.
main() {

usage() { echo "usage: upgrade.sh <sha> --migrations changed|unchanged [--allow-migration] [--release <label>] [--render-template <path>]" >&2; exit 2; }

[[ $# -ge 1 ]] || usage
NEW="$1"; shift
[[ "$NEW" =~ ^[0-9a-f]{40}$ ]] || { echo "upgrade: sha must be a full 40-character commit id" >&2; exit 2; }
MIGRATIONS=""; ALLOW_MIGRATION=0; LABEL="$NEW"; TEMPLATE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --migrations) MIGRATIONS="${2:-}"; shift 2 ;;
        --allow-migration) ALLOW_MIGRATION=1; shift ;;
        --release) LABEL="${2:-}"; shift 2 ;;
        --render-template) TEMPLATE="${2:-}"; shift 2 ;;
        *) usage ;;
    esac
done
[[ "$MIGRATIONS" == changed || "$MIGRATIONS" == unchanged ]] || usage

BASE_URL="${UPGRADE_BASE_URL:-https://raw.githubusercontent.com/AkkiaLab/AkkiaBot}"
HEALTH_TRIES="${UPGRADE_HEALTH_TRIES:-30}"
HEALTH_DELAY="${UPGRADE_HEALTH_DELAY:-2}"
compose=(docker compose)
umask 077

for f in compose.yaml .env app.env backup.sh; do
    [[ -f "$f" ]] || { echo "upgrade: $PWD/$f is missing; run from the stack directory" >&2; exit 4; }
done

exec 9>.upgrade.lock
flock -n 9 || { echo "upgrade: another upgrade is running in $PWD" >&2; exit 75; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OLD="$(sed -n 's/^AKKIABOT_TAG=//p' .env | tail -n1)"
PORT="$(sed -n 's/^AKKIABOT_HTTP_PORT=//p' .env | tail -n1)"
PORT="${PORT:-8000}"
BACKUP_FILE=""

record() {
    touch releases.log && chmod 600 releases.log
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$STAMP" "$LABEL" "${OLD:-?}" "$NEW" "${BACKUP_FILE:--}" "$1" >>releases.log
}

healthy() {
    local i
    for ((i = 0; i < HEALTH_TRIES; i++)); do
        if curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$PORT/readyz"; then
            return 0
        fi
        sleep "$HEALTH_DELAY"
    done
    return 1
}

# 1. Idempotent retry: already on <sha> and healthy.
if [[ "$OLD" == "$NEW" ]] && healthy; then
    echo "upgrade: already running $NEW and /readyz is healthy"
    record "already-current"
    exit 0
fi

# 2. Files of the target commit (into a private work directory).
WORK=".upgrade/$NEW"
rm -rf "$WORK" && mkdir -p "$WORK/observability"
fetch() { curl -fsSL --retry 3 -o "$WORK/$2" "$BASE_URL/$NEW/deploy/$1"; }
if ! { fetch compose.yaml compose.yaml && fetch observability/config.alloy observability/config.alloy \
    && fetch backup.sh backup.sh && fetch restore.sh restore.sh \
    && fetch .env.example env.example && fetch app.env.example app.env.example; }; then
    echo "upgrade: could not fetch the deploy files of $NEW" >&2
    record "fetch-failed"
    exit 4
fi
if [[ -n "$TEMPLATE" ]]; then
    if ! fetch "$TEMPLATE" app.env.tpl || ! op inject -f -i "$WORK/app.env.tpl" -o "$WORK/app.env" >/dev/null; then
        echo "upgrade: could not render $TEMPLATE of $NEW" >&2
        record "render-failed"
        exit 4
    fi
fi

# 3. Configuration check: keys only, values are never printed.
keys() { sed -n 's/^\([A-Z_][A-Z0-9_]*\)=.*/\1/p' "$1" | sort -u; }
missing="$(comm -23 <(keys "$WORK/env.example") <(keys .env) | tr '\n' ' ')"
if [[ -n "$TEMPLATE" ]]; then
    app_missing=""   # the rendered template is the complete app.env
else
    app_missing="$(comm -23 <(keys "$WORK/app.env.example") <(keys app.env) | tr '\n' ' ')"
fi
if [[ -n "${missing// /}" || -n "${app_missing// /}" ]]; then
    echo "upgrade: configuration incomplete for $NEW; add these keys first: .env: ${missing:-none}; app.env: ${app_missing:-none}" >&2
    record "config-missing"
    exit 3
fi

# 4. Migration gate.
if [[ "$MIGRATIONS" == changed && "$ALLOW_MIGRATION" != 1 ]]; then
    echo "upgrade: $OLD -> $NEW changes database migrations; needs the owner's confirmation (--allow-migration)" >&2
    record "migration-refused"
    exit 3
fi

# 5. Pull with the current files: a failure leaves everything untouched.
if ! AKKIABOT_TAG="$NEW" "${compose[@]}" pull --quiet migrate app; then
    echo "upgrade: pulling the image of $NEW failed" >&2
    record "pull-failed"
    exit 4
fi

# 6. Backup, snapshot, apply.
if ! out="$(./backup.sh)"; then
    echo "upgrade: backup failed; nothing was changed" >&2
    record "backup-failed"
    exit 5
fi
echo "$out"
BACKUP_FILE="$(sed -n 's/^backup: wrote \([^ ]*\).*/\1/p' <<<"$out" | tail -n1)"

SNAP=".upgrade/rollback-$STAMP"
mkdir -p "$SNAP/observability"
cp -p compose.yaml .env app.env backup.sh "$SNAP/"
[[ -f restore.sh ]] && cp -p restore.sh "$SNAP/"
[[ -f observability/config.alloy ]] && cp -p observability/config.alloy "$SNAP/observability/"

install -m 644 "$WORK/compose.yaml" compose.yaml
mkdir -p observability && install -m 644 "$WORK/observability/config.alloy" observability/config.alloy
install -m 755 "$WORK/backup.sh" backup.sh
install -m 755 "$WORK/restore.sh" restore.sh
[[ -n "$TEMPLATE" ]] && install -m 600 "$WORK/app.env" app.env
sed -i "s/^AKKIABOT_TAG=.*/AKKIABOT_TAG=$NEW/" .env

# 7. Start and check.
if "${compose[@]}" up -d --wait && healthy; then
    echo "upgrade: $OLD -> $NEW done; backup $BACKUP_FILE"
    record "ok"
    exit 0
fi

# 8. Failure after the switch.
echo "upgrade: $NEW did not become healthy" >&2
"${compose[@]}" ps >&2 || true
if [[ "$MIGRATIONS" == changed ]]; then
    echo "upgrade: migrations may have run; NOT rolling back automatically. Follow docs/operations.md §5.2 (backup $BACKUP_FILE, old version $OLD)." >&2
    record "failed-after-migration"
    exit 6
fi
cp -p "$SNAP"/compose.yaml "$SNAP"/.env "$SNAP"/app.env "$SNAP"/backup.sh .
[[ -f "$SNAP/restore.sh" ]] && cp -p "$SNAP/restore.sh" .
[[ -f "$SNAP/observability/config.alloy" ]] && cp -p "$SNAP/observability/config.alloy" observability/
if "${compose[@]}" up -d --wait && healthy; then
    echo "upgrade: rolled back to $OLD (no migrations involved)" >&2
    record "failed-rolled-back"
    exit 7
fi
echo "upgrade: rollback to $OLD failed as well; stack needs manual attention (snapshot $SNAP)" >&2
record "failed-rollback-failed"
exit 8
}

main "$@" </dev/null
