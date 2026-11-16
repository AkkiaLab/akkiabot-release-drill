# AkkiaBot task runner. Run `just` to list recipes.

set shell := ["bash", "-euo", "pipefail", "-c"]

compose_dev := "docker compose -f deploy/compose.dev.yaml"

[private]
default:
    @just --list

# Install Python + web dependencies and git hooks
setup: setup-py setup-web
    @if git rev-parse --git-dir >/dev/null 2>&1; then uv run pre-commit install || echo "warning: pre-commit hooks not installed"; fi

setup-py:
    uv sync --all-packages --locked

setup-web:
    pnpm install --frozen-lockfile

# Lint everything (ruff, import-linter, biome)
lint: lint-py lint-web

lint-py:
    uv run ruff check .
    uv run ruff format --check .
    uv run lint-imports

lint-web:
    pnpm exec biome check .

# Scan every commit reachable from HEAD for credentials (same as the CI `secret-scan` job)
secret-scan:
    uv run pre-commit run gitleaks-history --hook-stage manual --all-files

# Auto-format and apply safe fixes
fmt:
    uv run ruff check --fix .
    uv run ruff format .
    pnpm exec biome check --write .

# Static type checks (pyright, tsc)
typecheck: typecheck-py typecheck-web

typecheck-py:
    uv run pyright

typecheck-web:
    pnpm -r typecheck

# Run all tests (pytest, vitest)
test: test-py test-web

test-py *args:
    uv run pytest {{ args }}

test-web:
    pnpm -r test

# Production build of the Mini App, then check it carries no dev-only Telegram mock
build-web:
    pnpm --filter @akkiabot/webapp build
    bash apps/webapp/scripts/check-no-dev-mock.sh apps/webapp/dist

# Run pytest against the compose postgres/redis (start them with `just dev` or `just services`)
test-integration *args:
    AKKIABOT_TEST_DATABASE_URL="postgresql+asyncpg://akkiabot:akkiabot@127.0.0.1:5432/akkiabot" \
    AKKIABOT_TEST_REDIS_URL="redis://127.0.0.1:6379/15" \
    uv run pytest {{ args }}

# Apply database migrations to DB_URL (reads .env)
migrate revision="head":
    uv run akkiabot migrate {{ revision }}

# Run the bot and HTTP API (reads .env; run `just migrate` first)
serve:
    uv run akkiabot serve

# Autogenerate a new Alembic revision against DB_URL (database must be at head)
db-revision message:
    uv run python -c 'import sys; from akkiabot_core.config import load_database_settings; from akkiabot_core.db.migrate import revision; revision(load_database_settings().url, sys.argv[1])' "{{ message }}"

# Start local postgres/redis only
services:
    {{ compose_dev }} up -d --wait

# Start local postgres/redis and the Mini App dev server
dev:
    {{ compose_dev }} up -d --wait
    pnpm --filter @akkiabot/webapp dev

# Stop local dev services
dev-down:
    {{ compose_dev }} --profile observability down

# --- Real Telegram acceptance in the Telegram TEST environment (docs/e2e-acceptance.md). Never run in CI.

# Store this machine's 1Password service account token (prompted, not echoed)
e2e-token-init *args:
    uv run akkiabot-e2e token-init {{ args }}

# Connect to the Telegram test environment and confirm test_mode (no account needed)
e2e-check-env:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/login.env.tpl -- akkiabot-e2e check-env

# Interactive login of a test-environment account (role: admin | member); session goes to 1Password
e2e-login role *args:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/login.env.tpl -- akkiabot-e2e login --role {{ role }} {{ args }}

# Store a session file from `e2e-login <role> --out <file>` into 1Password (via stdin)
e2e-session-store role *args:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/login.env.tpl -- akkiabot-e2e session-store --role {{ role }} {{ args }}

# Create the test bot via the test-environment BotFather and write it back to 1Password
e2e-bootstrap-bot *args:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/staging.env.tpl -- akkiabot-e2e bootstrap-bot {{ args }}

# Start the bot (test environment) from this checkout and run the live M2/M3 acceptance
e2e *args:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/staging.env.tpl -- akkiabot-e2e run --serve {{ args }}

# Local end-to-end check of the captcha with Cloudflare's official Turnstile test keys:
# real widget (headless Chromium), local bot and Mini App, Telegram test environment.
# Needs `just e2e-browser-setup` once per machine. Not a substitute for the staging real-widget run.
e2e-turnstile-test-keys *args:
    just e2e --scenarios m7.captcha-test-keys {{ args }}

# Install the headless Chromium for `e2e-turnstile-test-keys` (system libraries via sudo)
e2e-browser-setup:
    uv run playwright install chromium-headless-shell
    sudo "$(uv run which playwright)" install-deps chromium-headless-shell

# Evaluate the antispam prompt on the offline data set (calls the real LLM; LLM_* / ANTISPAM_* from env or .env)
antispam-eval *args:
    uv run akkiabot-antispam-eval {{ args }}

# Staging release acceptance: full run against the deployed staging stack (no local bot).
# Add `--record-status <sha>` to write the GitHub commit status that production promotion requires.
# Every run creates a group of its own (--fresh-group): the staging database persists between runs.
e2e-staging *args:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/staging-release.env.tpl -- akkiabot-e2e run --fresh-group {{ args }}

# Delete old staging release acceptance groups (keeps the newest 2); dry run unless `--apply`
e2e-prune-groups *args:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/staging-release.env.tpl -- akkiabot-e2e prune-groups {{ args }}

# Regenerate the OpenAPI document and the frontend API types (commit both)
gen-api:
    uv run akkiabot openapi --output apps/webapp/openapi.json
    pnpm --filter @akkiabot/api-types gen-api

# Build the backend image and print its version
docker-build tag="akkiabot:dev":
    docker build -f deploy/Dockerfile -t {{ tag }} .
    docker run --rm {{ tag }}

# Local Grafana (otel-lgtm) + Alloy with the AkkiaBot dashboard and alerts: http://127.0.0.1:3000
dev-observability:
    {{ compose_dev }} --profile observability up -d

# Upload dashboards and alert rules to Grafana (GRAFANA_URL / GRAFANA_SA_TOKEN; docs/observability.md)
grafana-sync *args:
    uv run python -m scripts.grafana_sync {{ args }}

# --- Version branches and releases (docs/release.md)

# Release flow CLI: status, create, cascade, rc, check-rc, baseline, seal, window, … (`just release --help`)
release *args:
    uv run python -m scripts.release {{ args }}

# Release flow CLI as the release App (needed for `create`: only the App may create release branches)
release-app *args:
    scripts/release/as-app.sh uv run python -m scripts.release {{ args }}

# Deploy an RC to staging on this machine, then accept it in the same lock (scenarios: comma-separated; empty = full)
release-staging tag scenarios="":
    uv run python -m scripts.release deploy staging {{ tag }} --accept {{ if scenarios != "" { "--scenarios " + scenarios } else { "" } }}

# Deploy an owner-approved formal tag (release-production.yml `approve` job) to production
release-deploy env tag *args:
    uv run python -m scripts.release deploy {{ env }} {{ tag }} {{ args }}
