#!/usr/bin/env bash
# Run a command as the workspace agent App akkialab-agent with a *release*
# purpose token (docs/release.md §1, §7; AKKIA-25), e.g.
#   scripts/release/as-app.sh uv run python -m scripts.release create v1.4.1
# Needed where the release rulesets only let the App write: creating release
# branches (required checks are enforced on creation) and v* tags.
# The token is requested with exactly the release permissions (same set as
# PURPOSES["release"] in scripts/reviewer_gh.py and the release-setup action).
# The App id and private key come from 1Password (AkkiaBot/release-app) through
# the machine's service account token (same file as the acceptance tool). The
# installation token is passed to gh (GH_TOKEN) and git (http.extraheader via
# GIT_CONFIG_*) in the environment only: never in arguments, files or output.
set -euo pipefail

repo="${RELEASE_REPO:-AkkiaLab/AkkiaBot}"
token_file="${AKKIABOT_OP_TOKEN_FILE:-$HOME/.config/akkiabot/op-service-account-token}"
OP_SERVICE_ACCOUNT_TOKEN="$(cat "$token_file")"
export OP_SERVICE_ACCOUNT_TOKEN

app_id="$(op read 'op://AkkiaBot/release-app/app_id')"
# The key never touches the disk: it stays in a shell variable and reaches
# openssl through a pipe (process substitution), so no file can be left behind
# on any exit path. The 1Password hidden field folds the PEM's line breaks into
# spaces: rebuild a standard PEM first.
key="$(op read 'op://AkkiaBot/release-app/private_key' | python3 -I -c '
import re, sys
m = re.match(r"(-----BEGIN [A-Z ]+-----)(.*?)(-----END [A-Z ]+-----)", sys.stdin.read().strip(), re.S)
if not m:
    sys.exit("release-app/private_key is not a PEM private key")
body = "".join(m.group(2).split())
print(m.group(1))
for i in range(0, len(body), 64):
    print(body[i:i + 64])
print(m.group(3))
')"

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
now=$(date +%s)
header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 60)) $((now + 540)) "$app_id" | b64url)
signature=$(printf '%s.%s' "$header" "$payload" |
  openssl dgst -sha256 -sign <(printf '%s\n' "$key") -binary | b64url)
unset key
jwt="$header.$payload.$signature"

# The JWT goes to curl as a config on stdin (`-K -`), never as an argument.
api() {
  printf 'header = "Authorization: Bearer %s"\n' "$jwt" |
    curl -sSf -K - -H 'Accept: application/vnd.github+json' "$@"
}
installation=$(api "https://api.github.com/repos/$repo/installation" | python3 -I -c 'import json, sys; print(json.load(sys.stdin)["id"])')
permissions='{"actions":"write","administration":"write","checks":"read","contents":"write","issues":"write","metadata":"read","pull_requests":"write","statuses":"read"}'
token=$(api -X POST -d "{\"repositories\":[\"${repo#*/}\"],\"permissions\":$permissions}" \
  "https://api.github.com/app/installations/$installation/access_tokens" |
  python3 -I -c 'import json, sys; print(json.load(sys.stdin)["token"])')
unset jwt

unset OP_SERVICE_ACCOUNT_TOKEN  # only op needs it, not the wrapped command
auth=$(printf 'x-access-token:%s' "$token" | base64 -w0)
export GH_TOKEN="$token" GITHUB_REPOSITORY="$repo"
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="http.https://github.com/.extraheader"
export GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $auth"
exec "$@"
