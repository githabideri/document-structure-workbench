# Browser smoke and debugging

The repository has two complementary acceptance layers:

| Tool | Use it for | Data source |
| --- | --- | --- |
| `pytest app/workbench/tests/test_fake_provider.py` | Provider wire behavior and API-to-worker integration | Disposable Django test database and real local HTTP fake provider |
| `scripts/dsw smoke --fake` | Operator-facing deterministic fake smoke | The same real HTTP integration suite |
| `scripts/dsw-browser-smoke` | Rendered UI, login, HTMX polling, citations, screenshots | Local or explicitly selected deployed fixture |
| `scripts/dsw smoke --live ...` | Explicit real-provider validation | User-supplied project/source IDs |

Django tests are still useful for model and view contracts, but they do not
prove that a browser can log in, render the page, follow HTMX polling, or open
a citation. The browser scenario exists for those boundaries.

## Local browser run

Create a generic fixture in a development database:

```bash
cd app
python manage.py migrate
python manage.py seed_browser_fixture \
  --username dsw-browser-user \
  --password 'use-a-local-only-password' \
  --json
```

The command is idempotent. It creates a dedicated integration project, owner
membership, `source-a.pdf`, `source-b.pdf`, one-page processed revisions, and
search passages. It does not require real PDF files because the browser flow
needs searchable processed data, not document ingestion.

Start the web server and worker in separate terminals. Configure the fake
provider in another terminal, then run:

```bash
scripts/dsw fake-provider --port 18081
```

Set the application environment to `DSW_CHAT_BASE_URL=http://127.0.0.1:18081/v1`
and `DSW_CHAT_MODEL=fake-qwen`, restart the web/worker processes, then run:

```bash
export DSW_BROWSER_BASE_URL=http://127.0.0.1:8000
export DSW_BROWSER_USERNAME=dsw-browser-user
export DSW_BROWSER_PASSWORD='use-a-local-only-password'
scripts/dsw browser-smoke
```

Artifacts are written to `/tmp/dsw-browser-smoke-*` by default:

- `result.json` — overall status, scenarios, release, URL, and screenshot paths;
- `*.snapshot` — accessibility snapshots at major workflow points;
- `*.log` — command output for failed steps;
- `conversation.png`, `cited-document.png`, `returned-chat.png` — visual evidence.

The runner also saves `console.log`, `errors.log`, `network.har`, and
`ui-diagnostics.json`. The latter is the browser-side manifest from
`window.__DSW_UI_DIAGNOSTICS__`; `?debug_ui=1` adds non-interactive labels and
outlines for the major semantic areas. These artifacts are part of the
acceptance record rather than merely evidence that a screenshot was taken.

Use `scripts/dsw-browser-smoke --help` for all options. `scripts/dsw --help`
lists the operator commands.

For an administrator fixture, set `DSW_BROWSER_EXPECT_MAINTAINER=1` to add
run-inspector regression checks. They verify that closing the inspector
restores selected evidence and originating focus, and that closing it without
selected evidence restores the source-scope summary. Ordinary fixture accounts
skip these maintainer-only checks.

## Deployed or staging run

Do not let a remote browser smoke test select arbitrary user documents. Create
a dedicated fixture account/project on the target deployment, then pin the
fixture IDs:

```bash
export DSW_BROWSER_BASE_URL=https://staging.example.test
export DSW_BROWSER_USERNAME=dsw-browser-user
export DSW_BROWSER_PASSWORD='provided-out-of-band'
export DSW_BROWSER_PROJECT_ID=123
export DSW_BROWSER_SOURCE_IDS=456,457
scripts/dsw-browser-smoke --allow-remote
```

Remote execution requires both `--allow-remote` and explicit source IDs. The
runner verifies that those IDs are visible in the UI and belong to the pinned
project before asking a question. Credentials are never written to artifacts.

The fixture should be isolated from ordinary user data and should use a
dedicated account with only the permissions needed for the scenario. A
deployment pipeline may provision it once, or an operator may run
`seed_browser_fixture` explicitly on the target. The browser runner itself
does not seed or delete remote data.

For a deployed smoke run, do not use `fake-provider`; the target's configured
provider and worker are what the test is intended to validate. Use
`scripts/dsw smoke --live` first when provider readiness itself is the question.

The streamlined deployed workflow is:

```bash
scripts/dsw verify-deployed \
  --project PROJECT_ID --source SOURCE_ID \
  --question "What does the fixture document discuss?" \
  --browser
```

This writes health, live-run, evidence, support-bundle, and browser artifacts
under `/tmp/dsw-verify-*`. `--browser` uses the existing
`DSW_BROWSER_*` variables and still requires `--allow-remote` internally for a
non-local URL. For CI or agents, add `--json` to receive a stable summary.

## Debug flow

When a run fails:

1. Open `result.json` and identify the first failed scenario.
2. Read that scenario's `.log` and the nearest `.snapshot`.
3. Re-run with a fixed `DSW_BROWSER_OUTPUT_DIR` so artifacts are easy to compare.
4. Check `/api/v1/health/` for release, database, worker queue, and provider configuration.
5. Use `scripts/dsw chat inspect --run RUN_ID` and `scripts/dsw chat evidence --run RUN_ID` if a run ID was produced.
6. Export an audited support bundle only when authorized:

```bash
scripts/dsw support-bundle --run RUN_ID --format markdown --output ./artifacts/run.md
```

The health endpoint actively probes the configured provider's `/models`
endpoint and reports whether the exact configured model is available. A
successful live smoke remains the definitive generation test.

For citation failures, inspect the cited URL in the snapshot and verify that
the immutable revision/page parameters are present. For polling failures,
inspect the run state and worker heartbeat before debugging the browser.

## Safety and cleanup

Local fixture data belongs in a disposable database. Remote fixture threads
are real application data; keep the account/project isolated and clean them up
according to the deployment's retention policy. Live provider smoke is never
run by ordinary CI and must always receive explicit project, source, and
question parameters.
