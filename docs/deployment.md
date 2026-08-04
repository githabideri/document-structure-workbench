# Deployment and post-deployment verification

DSW is an application repository. Deployment orchestration belongs to the
environment that owns the target: a parent infrastructure repository, an
Ansible/Terraform project, a container platform, or a host-local service
manager. This repository intentionally does not contain environment-specific
hostnames, filesystem paths, inventories, or secret-store locations.

The deployment adapter for an environment should do four things:

1. choose a DSW commit or image;
2. render private configuration and secrets;
3. install/migrate/restart the web and worker services;
4. run the post-deployment checks described below.

The adapter may live beside this repository in a parent checkout, on a
development laptop, or inside a prototype LXC. Its location is supplied by
the operator, for example:

```bash
export DSW_DEPLOY_WRAPPER=/path/to/environment/deploy-dsw.sh
"$DSW_DEPLOY_WRAPPER" --development
```

The target URL is likewise an environment value, never a value inferred from
the local development server:

```bash
export DSW_DEPLOY_URL=https://staging.example.test
```

## Before deploying

Run the application checks from this repository:

```bash
./.venv/bin/python app/manage.py check
./.venv/bin/python -m pytest -q
git diff --check
```

The deployment archive is created from the DSW git commit selected by the
wrapper. Uncommitted files are not included in a release archive. Therefore,
changes intended for deployment must be committed in this submodule and the
parent homelab repository must point at that commit.

Do not commit private `.env` files, API keys, passwords, provider endpoints,
or browser credentials. Private values belong in the homelab configuration
path referenced by the deployment wrapper.

## Deployment modes

Use the deployment adapter for the environment. A typical adapter exposes
these semantics:

```bash
"$DSW_DEPLOY_WRAPPER" --development
"$DSW_DEPLOY_WRAPPER" --release
"$DSW_DEPLOY_WRAPPER" --rollback COMMIT_SHA
```

Use `--check --diff` to inspect the Ansible changes before applying them:

```bash
"$DSW_DEPLOY_WRAPPER" --development --check --diff
```

Development mode is useful for validating infrastructure changes. Release
mode requires clean homelab and DSW trees and deploys the exact submodule HEAD.
Rollback switches to an already-built release; database migrations are not
automatically reversed.

## What deployment configures

The private deployment environment supplies the Django secret, allowed hosts,
database path, release marker, Docling endpoint, chat endpoint/model, worker
leases, and Gunicorn settings. The public generic schema is [.env.example](../.env.example);
environment-specific secret files and paths must remain outside this
repository.

### Laptop, parent checkout, and prototype LXC

All three environments use the same contract:

- the laptop can run the adapter locally or invoke it over SSH;
- a parent infrastructure checkout can package this repository at a pinned
  commit and deploy it with its own inventory;
- a prototype LXC can run the adapter locally with its own systemd/container
  units and private environment file.

The application-side commands are identical. Only the adapter, target URL,
service manager, and secret provider change. Record those values in the
environment’s private operations documentation, not in this public repo.

Changing chat configuration must restart both the Gunicorn service and the
processing worker. After deployment, verify that both processes received the
new environment rather than assuming a config file update was sufficient.

## Post-deployment checks

Use `DSW_DEPLOY_URL` or the environment’s equivalent to run the public health
check:

```bash
curl -fsS "$DSW_DEPLOY_URL/api/v1/health/"
```

The response should report the expected release SHA, database `ok`, worker
status, queue depth, and chat configuration state. From a machine with a
scoped operator token:

```bash
export DSW_API_BASE_URL="$DSW_DEPLOY_URL/api/v1"
export DSW_API_TOKEN='provided-out-of-band'
scripts/dsw doctor --json
```

Then run the explicit live smoke test with a dedicated project/source fixture:

```bash
scripts/dsw smoke --live \
  --project PROJECT_ID \
  --source SOURCE_ID \
  --question "What does the fixture document discuss?"
```

For the complete operator check, use the composed workflow. It saves all
machine-readable artifacts locally and can include the browser scenario:

```bash
scripts/dsw verify-deployed \
  --project PROJECT_ID --source SOURCE_ID \
  --question "What does the fixture document discuss?" \
  --browser --json
```

The health response actively checks provider reachability and exact model
availability through the provider's `/models` endpoint. Generation, evidence,
and citation validation are still verified by the live run.

Live smoke is not part of ordinary CI and never chooses arbitrary production
documents.

## Browser verification after deployment

Use a dedicated browser-test account and a dedicated fixture project. Pin the
project and source IDs:

```bash
export DSW_BROWSER_BASE_URL="$DSW_DEPLOY_URL"
export DSW_BROWSER_USERNAME=dsw-browser-user
export DSW_BROWSER_PASSWORD='provided-out-of-band'
export DSW_BROWSER_PROJECT_ID=PROJECT_ID
export DSW_BROWSER_SOURCE_IDS=SOURCE_ID_A,SOURCE_ID_B

scripts/dsw-browser-smoke --allow-remote
```

The runner refuses remote execution without `--allow-remote` and explicit
source IDs. It writes screenshots, accessibility snapshots, command logs, and
`result.json`; retain those artifacts with the deployment verification record.

## Debugging a failed deployment check

1. Compare the health response release SHA with the deployed commit.
2. Check the web and worker service state on the target host.
3. Check the worker heartbeat and queued/stale run counts.
4. Check chat provider configuration and exact model availability.
5. Inspect the run through `scripts/dsw chat inspect --run RUN_ID`.
6. Export an audited support bundle only with maintainer authorization.
7. If configuration changed without a release change, restart both affected
   services and rerun health plus live/browser smoke explicitly.

See [Browser smoke and debugging](browser-smoke.md) for fixture setup and
browser-specific diagnostics.
