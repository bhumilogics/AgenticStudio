const { test, expect } = require('@playwright/test');

// TestDesk page, against a stubbed API (same approach as app.spec.js): no real
// database, user or model is touched.
const api = 'http://localhost:8000';
const user = { id: 'u1', email: 'qa@example.test', role: 'admin' };

async function mockApi(page, opts = {}) {
  const calls = { run: [], save: [] };
  const scenarios = [];
  const results = {};
  let polls = 0;

  await page.route(`${api}/**`, async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

    if (path === '/health') return json({ status: 'ok' });
    if (path === '/auth/me') return json(user);
    if (path === '/testdesk/targets') return json({ jobs: ['qa_regression_job', 'other_job'], agents: ['planner', 'qa_lead'] });
    if (path === '/testdesk/scenarios' && method === 'GET') return json({ scenarios });
    if (path === '/testdesk/scenarios' && method === 'POST') {
      const body = req.postDataJSON(); calls.save.push(body);
      if (opts.rejectSave) return json({ detail: 'describe the scenario to test (the \'scenario\' text is empty)' }, 422);
      const sc = { id: 'sc-' + (scenarios.length + 1), options: {}, expect_status: 'done', job_yaml: '', ...body };
      scenarios.push(sc); return json(sc);
    }
    if (path === '/testdesk/run' && method === 'POST') {
      const body = req.postDataJSON(); calls.run.push(body);
      const sc = body.scenario || scenarios.find((s) => s.id === body.scenario_id);
      const id = 'res-' + (calls.run.length);
      results[id] = { id, scenario_id: sc.id || null, scenario_name: sc.name || 'adhoc', scenario: sc, mode: body.mode, started_at: 1700000000, owner_id: 'u1' };
      return json({ result_id: id, mode: body.mode });
    }
    if (path === '/testdesk/results' && method === 'GET') {
      return json({ results: Object.values(results).map((r) => ({ ...r, state: polls > 0 ? 'finished' : 'running', verdict: polls > 0 ? 'pass' : null })) });
    }
    const m = path.match(/^\/testdesk\/results\/(.+)$/);
    if (m) {
      const r = results[m[1]]; polls += 1;
      if (polls < 2) return json({ ...r, state: 'running', verdict: null, checks: [], events: [], approvals: [] });
      return json({
        ...r, state: 'finished', verdict: 'pass', finished_at: 1700000012, run_status: 'done',
        final_answer: 'Login flow covered.', approvals: ['qa_lead'], sandbox_path: null,
        run_id: r.mode === 'prod' ? 'run-123' : null,
        checks: [{ name: 'run status is done', ok: true, detail: "run ended 'done'" }, { name: 'answer mentions “login”', ok: true, detail: 'found' }],
        events: [{ ts: 1700000001, level: 'INFO', agent_name: 'qa_lead', event_type: 'node', payload: 'ok' }],
      });
    }
    return json({});
  });
  return calls;
}

async function open(page, opts) {
  await page.addInitScript(() => localStorage.setItem('agenticstudio_token', 'tok'));
  const calls = await mockApi(page, opts);
  await page.goto('/#/testdesk');
  await expect(page.getByRole('heading', { name: 'TestDesk' })).toBeVisible();
  return calls;
}

test('shows the form with Test selected and a nav link', async ({ page }) => {
  const errors = []; page.on('pageerror', (e) => errors.push(String(e)));
  await open(page);
  await expect(page.getByRole('link', { name: 'TestDesk' })).toHaveClass(/active/);
  await expect(page.getByText('Test -- throwaway database')).toBeVisible();
  await expect(page.locator('.mode-card[data-mode="test"]')).toHaveClass(/selected/);
  await expect(page.getByRole('button', { name: 'Run in test sandbox' })).toBeVisible();
  await expect(page.getByText('No saved scenarios yet')).toBeVisible();
  expect(errors).toEqual([]);
});

test('prod is unavailable for agents and draft jobs', async ({ page }) => {
  await open(page);
  await page.locator('.mode-card[data-mode="prod"]').click();
  await expect(page.locator('.mode-card[data-mode="prod"]')).toHaveClass(/selected/);
  await page.locator('select').first().selectOption('agent');
  await expect(page.locator('.mode-card[data-mode="prod"]')).toHaveClass(/disabled/);
  await expect(page.locator('.mode-card[data-mode="test"]')).toHaveClass(/selected/);
  await expect(page.getByText('Prod runs a saved Job')).toBeVisible();
  await page.locator('select').first().selectOption('job');
  await page.getByText('Try a draft Job definition').click();
  await page.getByPlaceholder('id: my_draft_job').fill('id: draft\nagents: [planner]');
  await expect(page.locator('.mode-card[data-mode="prod"]')).toHaveClass(/disabled/);
});

test('runs a test-mode scenario and shows the report', async ({ page }) => {
  const calls = await open(page);
  await page.getByPlaceholder('Describe the scenario').fill('Check the login page handles a wrong password');
  await page.getByPlaceholder('One phrase per line').fill('login');
  await page.getByRole('button', { name: 'Run in test sandbox' }).click();

  await expect(page.getByText('Running in a sandbox')).toBeVisible();
  await expect(page.getByText('Passed in the sandbox. Nothing was saved to the app database.')).toBeVisible({ timeout: 10000 });
  await expect(page.getByText('answer mentions “login”')).toBeVisible();
  await expect(page.getByText('Login flow covered.')).toBeVisible();
  await expect(page.getByText('Auto-approved gates: qa_lead')).toBeVisible();

  expect(calls.run).toHaveLength(1);
  expect(calls.run[0].mode).toBe('test');
  expect(calls.run[0].scenario).toMatchObject({
    target_type: 'job', target_id: 'qa_regression_job', expectations: ['login'],
    task: 'Check the login page handles a wrong password',
    options: { block_external: true, auto_approve: true, keep_sandbox: false },
  });
});

test('prod needs a second click and never sends test options as prod', async ({ page }) => {
  const calls = await open(page);
  await page.getByPlaceholder('Describe the scenario').fill('Real run of the regression job');
  await page.locator('.mode-card[data-mode="prod"]').click();
  await page.getByRole('button', { name: 'Run for real (prod)' }).click();
  await expect(page.getByRole('button', { name: /Click again to confirm/ })).toBeVisible();
  expect(calls.run).toHaveLength(0);
  await page.getByRole('button', { name: /Click again to confirm/ }).click();
  await expect(page.getByText('Open the Run in the app')).toBeVisible({ timeout: 10000 });
  expect(calls.run).toHaveLength(1);
  expect(calls.run[0].mode).toBe('prod');
  await expect(page.getByRole('link', { name: /Open the Run in the app/ })).toHaveAttribute('href', '#/jobs/run/run-123');
});

test('changing the form disarms a pending prod confirmation', async ({ page }) => {
  const calls = await open(page);
  await page.getByPlaceholder('Describe the scenario').fill('x');
  await page.locator('.mode-card[data-mode="prod"]').click();
  await page.getByRole('button', { name: 'Run for real (prod)' }).click();
  await expect(page.getByRole('button', { name: /Click again to confirm/ })).toBeVisible();
  await page.getByPlaceholder('Describe the scenario').fill('x changed');
  await expect(page.getByRole('button', { name: 'Run for real (prod)' })).toBeVisible();
  expect(calls.run).toHaveLength(0);
});

test('saves a scenario, lists it, and loads it back', async ({ page }) => {
  const calls = await open(page);
  await page.getByPlaceholder('e.g. Checkout regression').fill('Login checks');
  await page.getByPlaceholder('Describe the scenario').fill('Wrong password shows an error');
  await page.getByRole('button', { name: 'Save scenario' }).click();
  await expect(page.getByRole('cell', { name: 'Login checks' })).toBeVisible();
  expect(calls.save[0]).toMatchObject({ name: 'Login checks', target_id: 'qa_regression_job' });

  await page.getByRole('button', { name: 'New scenario' }).click();
  await expect(page.getByPlaceholder('Describe the scenario')).toHaveValue('');
  await page.getByRole('button', { name: 'Load' }).click();
  await expect(page.getByPlaceholder('Describe the scenario')).toHaveValue('Wrong password shows an error');
  await page.getByRole('button', { name: 'Run prod…' }).click();
  await expect(page.getByRole('button', { name: /Click again to confirm/ })).toBeVisible();
});

test('shows server validation errors', async ({ page }) => {
  await open(page, { rejectSave: true });
  await page.getByRole('button', { name: 'Save scenario' }).click();
  await expect(page.getByText('describe the scenario to test')).toBeVisible();
});
