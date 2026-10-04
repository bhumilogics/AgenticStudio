const { test, expect } = require('@playwright/test');

const apiOrigin = 'http://localhost:8000';
const user = { id: 'user-test', email: 'qa@example.test', role: 'admin' };
const job = {
  id: 'qa_regression_job',
  name: 'QA Regression Pass',
  description: 'Run the QA team on a regression scope.',
  trigger_type: 'manual',
  status: 'published',
  agents: ['qa_manager', 'qa_lead'],
  tasks: [
    { id: 'strategy', agent: 'qa_manager', name: 'Define test strategy', instructions: 'Scope this pass.', depends_on: [] },
    { id: 'plan', agent: 'qa_lead', name: 'Build the test plan', instructions: 'Create test cases.', depends_on: ['strategy'] },
  ],
};

async function json(route, body, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

async function mockApi(page) {
  await page.route(`${apiOrigin}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === '/health') return json(route, { status: 'ok' });
    if (path === '/auth/me') return json(route, user);
    if (path === '/auth/register' || path === '/auth/login') {
      return json(route, { token: 'playwright-token', user });
    }
    if (path === '/jobs' && request.method() === 'GET') return json(route, { jobs: [job] });
    if (path === `/jobs/${job.id}` && request.method() === 'GET') return json(route, job);
    if (path === `/jobs/${job.id}/graph`) {
      return json(route, {
        nodes: [
          { id: 'qa_manager', position: 0 },
          { id: 'qa_lead', position: 1 },
        ],
        edges: [{ from: 'qa_manager', to: 'qa_lead' }],
      });
    }
    if (path === `/jobs/${job.id}/runs`) return json(route, { runs: [] });
    if (path === '/agents') {
      return json(route, { agents: [
        { name: 'qa_manager', display_name: 'QA Manager', reports_to: null, hands_off_to: 'qa_lead', tools: [] },
        { name: 'qa_lead', display_name: 'QA Lead', reports_to: 'qa_manager', hands_off_to: 'qa_automation_engineer', tools: ['productivity'] },
      ] });
    }
    if (path === '/agents/qa_lead') {
      return json(route, { name: 'qa_lead', instructions: 'Turns strategy into an executable test plan.' });
    }
    if (path === '/schedules') return json(route, { schedules: [] });
    if (path.startsWith('/runs/') && path.endsWith('/events')) return json(route, { events: [] });

    return json(route, { detail: `Unhandled test API request: ${request.method()} ${path}` }, 404);
  });
}

test('registers an account and opens the Jobs page', async ({ page }) => {
  await mockApi(page);
  await page.goto('/');

  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  await page.getByRole('button', { name: 'Create an account instead' }).click();
  await page.getByPlaceholder('you@example.com').fill(user.email);
  await page.getByPlaceholder('••••••••').fill('example-password');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();

  await expect(page.getByRole('heading', { name: 'Jobs' })).toBeVisible();
  await expect(page.getByRole('link', { name: job.name })).toBeVisible();
});

test.describe('authenticated app', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('agenticstudio_token', 'playwright-token'));
    await mockApi(page);
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Jobs' })).toBeVisible();
  });

  test('shows a Job and its task workflow', async ({ page }) => {
    await page.getByRole('link', { name: job.name }).click();

    await expect(page.getByRole('heading', { name: job.id })).toBeVisible();
    await expect(page.getByText('Define test strategy')).toBeVisible();
    await expect(page.getByText('Build the test plan')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Run this Job' })).toBeVisible();
  });

  test('opens an Agent detail', async ({ page }) => {
    await page.getByRole('link', { name: 'Agents' }).click();
    await expect(page.getByRole('heading', { name: 'Agents' })).toBeVisible();
    await page.getByRole('button', { name: 'View' }).nth(1).click();

    await expect(page.getByRole('heading', { name: 'qa_lead' })).toBeVisible();
    await expect(page.getByText('Turns strategy into an executable test plan.')).toBeVisible();
  });

  test('opens the Scheduler and renders its empty state', async ({ page }) => {
    await page.getByRole('link', { name: 'Scheduler' }).click();

    await expect(page.getByRole('heading', { name: 'Scheduler' })).toBeVisible();
    await expect(page.getByText('No schedules yet.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Create' })).toBeVisible();
  });

  test('opens the Log Explorer', async ({ page }) => {
    await page.getByRole('link', { name: 'Log Explorer' }).click();

    await expect(page.getByRole('heading', { name: 'Log Explorer' })).toBeVisible();
    await expect(page.getByPlaceholder('run_id')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Load' })).toBeVisible();
  });
});