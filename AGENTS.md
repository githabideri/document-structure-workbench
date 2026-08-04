# Agent workflow for Document Structure Workbench

This is a Django application with a separately owned deployment environment.
Treat a change as complete only when the requested acceptance level has
passed; local tests alone do not prove deployment or browser behavior.

## Preflight

Before editing, inspect `README.md`, `docs/deployment.md`, and
`docs/browser-smoke.md`. If deployment is in scope, inspect the parent
homelab deployment adapter too. Check `git status` in both the submodule and
parent checkout, then record the target URL, deployment mode, fixture project
and source IDs, and available credentials. Never infer these from a local
development server.

## Credential map

- `DSW_DOCLING_API_KEY` authenticates the Docling processing service.
- `DSW_CHAT_API_KEY` authenticates the configured model provider.
- `DSW_API_KEY` is application configuration; it is not automatically a REST
  bearer token.
- `ApiToken` is the REST credential sent as `Authorization: Bearer ...`.
- Web username/password is session authentication for browser tests.

REST chat verification requires an `ApiToken` with `chat:read`, `chat:write`,
`chat:retry`, `support:read`, and `support:export` as applicable. Never print
raw tokens, commit them, or put operator tokens in the service environment
unless explicitly required. Prefer a separate ignored operator file with
mode `600`, such as `.env.operator`, or the environment secret manager.

## Implementation workflow

1. Translate the request into explicit acceptance gates before coding.
2. Read the existing implementation and tests before designing abstractions.
3. Implement the smallest coherent vertical slice and preserve compatibility.
4. Add contract tests with the implementation.
5. Track implemented, verified, and deferred plan items separately.
6. Run `git diff --check` and inspect the final diff for unrelated changes.

For chat/retrieval changes, test authorization, immutable run scope snapshots,
empty evidence, citation validation, provider rejection/fallback, bounded
tool calls, and prompt-injection text as untrusted evidence. Scope changes
apply to the next run and must not mutate an existing run.

## Local gates

```bash
./.venv/bin/python app/manage.py check
./.venv/bin/python -m pytest
git diff --check
```

Use the fake-provider integration tests and `scripts/dsw smoke --fake` for
provider wire behavior. Do not call a live provider from ordinary CI or
unconstrained tests.

## Deployment gates

Deployment archives are built from a committed submodule revision. Therefore:

1. Commit the intended application changes in this submodule.
2. Confirm dirty parent-checkout files are understood and unrelated.
3. Use the environment deployment adapter, not an ad hoc copy.
4. Record the deployed commit SHA.
5. Confirm migrations, web, and worker services completed successfully.

Development deployment may tolerate dirty parent infrastructure; release
deployment requires clean trees. Never claim deployment from a local test or
archive build alone.

## Deployed gates

Run health, authenticated API smoke, and browser verification separately:

```bash
curl -fsS "$DSW_DEPLOY_URL/api/v1/health/"
scripts/dsw doctor --json
scripts/dsw smoke --live \
  --project PROJECT_ID --source SOURCE_ID \
  --question "What does the fixture document discuss?"
scripts/dsw verify-deployed \
  --project PROJECT_ID --source SOURCE_ID \
  --question "What does the fixture document discuss?" \
  --browser --json
```

Use a dedicated fixture project/account and explicit source IDs. Verify a
readable answer, non-empty persisted evidence, citations, immutable
revision/page navigation, and return-to-chat behavior. Preserve health, run,
evidence, support-bundle, and browser artifacts under `/tmp/dsw-verify-*` or
the agreed operator artifact location.

If browser tooling fails, verify its socket directory and namespace, then use
`scripts/agent-browser-dsw`. Remote runs require `--allow-remote`; never let
the runner select arbitrary user documents.

## Reporting and handoff

Report the exact commit/release, local test results, deployed health/API/
browser results separately, fixture IDs without credentials, remaining
blockers or deferred gates, and artifact locations. Do not call work “end to
end verified” when only unit tests or deployment health passed. If a required
credential or fixture is missing, name it explicitly and complete every safe
verification that does not require it.
