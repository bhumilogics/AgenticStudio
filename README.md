# AgenticStudio Playwright Guide

This guide gives a browser-testing agent the application map, stable UI entry points, and conventions for writing and running end-to-end tests.

## Application Map

- The frontend is a no-build, plain JavaScript single-page app: `frontend/index.html`, `frontend/app.js`, and `frontend/styles.css`.
- The frontend uses hash navigation: `#/jobs`, `#/jobs/<job-id>`, `#/jobs/run/<run-id>`, `#/agents`, `#/schedules`, and `#/logs`.
- The API is a FastAPI app at `interface.api:app`, normally served at `http://localhost:8000`.
- The frontend is normally served as static files from `frontend/` at `http://localhost:8080`.
- The UI requires authentication. The first registered account on a fresh instance is an admin. Admin-only areas and operations include user management, schedule management, and editing/deleting Agent or Job definitions.
- Main UI workflows: register/sign in; browse and run Jobs; inspect a Job's workflow/tasks and run history; view Agent definitions; manage recurring schedules; filter run events in Log Explorer; resume approval-paused runs or mark stale runs failed.
- LLM execution is external and potentially credentialed. Avoid invoking real Job runs in routine UI tests unless the test explicitly targets execution and supplies a deterministic backend.

## Start The App

From the repository root, start both processes in separate terminals:

```powershell
python -m uvicorn interface.api:app --reload
```

```powershell
Set-Location frontend
python -m http.server 8080
```

Open `http://localhost:8080`. The sidebar API base should be `http://localhost:8000`. Configure `.env` and the required LLM credentials only for tests that actually execute agents; the included UI tests do not need them.

## Playwright Setup And Commands

Install JavaScript dependencies and the Chromium browser once:

```powershell
npm.cmd install
npx.cmd playwright install chromium
```

Run all browser tests:

```powershell
npm.cmd run test:e2e
```

Run one test file or open the interactive runner:

```powershell
npx.cmd playwright test playwright/tests/app.spec.js
npx.cmd playwright test --ui
```

The Playwright config expects the frontend to be running at port 8080. `playwright/tests/app.spec.js` intercepts API calls at port 8000 with deterministic fixtures, so the tests do not mutate the local database, create a real user, or call an LLM. Test artifacts and failure traces are managed by Playwright.

## Test Authoring Guidance

- Prefer user-visible locators: `getByRole`, `getByLabel`, `getByPlaceholder`, and visible text. Avoid selectors tied to CSS layout or generated DOM structure.
- For UI behavior, stub the API using `page.route('http://localhost:8000/**', ...)`. Keep fixtures representative of the API responses documented/implemented in `interface/api.py`.
- The UI sends a Bearer token from `localStorage` as `agenticstudio_token`; authenticated tests can seed a test token before navigation and mock `GET /auth/me`.
- Keep tests isolated. Do not use a real account, production data, live model calls, or mutating API requests unless explicitly testing that integration.
- Important backend routes include `/auth/register`, `/auth/login`, `/auth/me`, `/jobs`, `/jobs/{id}`, `/jobs/{id}/graph`, `/jobs/{id}/runs`, `/agents`, `/agents/{name}`, `/schedules`, `/runs/{id}`, and `/runs/{id}/events`.
- To add coverage, include a deterministic fixture for each API request the page makes, then assert the resulting user-visible state. Cover validation/error states as well as successful paths where relevant.
- The example Job in `jobs/qa_regression_job.yaml` currently has a dependency cycle. Do not treat it as a runnable Job fixture; mocked UI browsing is safe, but a real run is expected to fail until its dependencies are corrected.

## Existing Smoke Coverage

The starter suite verifies account-registration UI (against a stub), authenticated Job listing and task details, Agent details, the Scheduler empty state, and Log Explorer navigation. Extend it as workflows are changed.