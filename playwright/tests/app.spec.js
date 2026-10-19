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
  const users = [
    { id: user.id, email: user.email, role: user.role, created_at: 1700000000 },
    { id: 'user-other', email: 'other@example.test', role: 'end_user', created_at: 1700000100 },
  ];
  await page.route(`${apiOrigin}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === '/health') return json(route, { status: 'ok' });
    if (path === '/auth/me') return json(route, user);
    if (path === '/auth/register' || path === '/auth/login') {
      return json(route, { token: 'playwright-token', user });
    }
    if (path === '/auth/users' && request.method() === 'GET') return json(route, { users });
    if (path === '/auth/users' && request.method() === 'POST') {
      const body = request.postDataJSON();
      const created = { id: 'user-created', email: body.email, role: body.role, created_at: 1700000200 };
      users.push(created);
      return json(route, created, 201);
    }
    const userRoute = path.match(/^\/auth\/users\/([^/]+)$/);
    if (userRoute && request.method() === 'PATCH') {
      const target = users.find((item) => item.id === userRoute[1]);
      Object.assign(target, request.postDataJSON());
      return json(route, target);
    }
    if (userRoute && request.method() === 'DELETE') {
      const index = users.findIndex((item) => item.id === userRoute[1]);
      if (index >= 0) users.splice(index, 1);
      return json(route, { deleted: userRoute[1] });
    }
    if (/^\/auth\/users\/[^/]+\/password$/.test(path) && request.method() === 'PUT') {
      return json(route, { updated: path.split('/')[3] });
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

  test('manages users, roles, passwords, and deletion', async ({ page }) => {
    await page.getByRole('link', { name: 'Users' }).click();
    await expect(page.getByRole('heading', { name: 'User management' })).toBeVisible();
    await expect(page.getByLabel('Email for other@example.test')).toBeVisible();
<<<<<<< Updated upstream
    const selfRow = page.getByLabel(`Email for ${user.email}`).locator('xpath=ancestor::tr');
    await expect(selfRow.getByRole('button', { name: 'Delete' })).toBeDisabled();

    const otherRow = page.getByLabel('Role for other@example.test').locator('xpath=ancestor::tr');
    await otherRow.getByLabel('Email for other@example.test').fill('renamed@example.test');
    await page.getByLabel('Role for other@example.test').selectOption('admin');
    await otherRow.getByRole('button', { name: 'Save' }).click();

    const refreshedOtherRow = page.getByLabel('Role for renamed@example.test').locator('xpath=ancestor::tr');
    await refreshedOtherRow.getByLabel('New password for renamed@example.test').fill('replacement-password');
=======

    const otherRow = page.getByLabel('Role for other@example.test').locator('xpath=ancestor::tr');
    await page.getByLabel('Role for other@example.test').selectOption('admin');
    await otherRow.getByRole('button', { name: 'Save' }).click();

    const refreshedOtherRow = page.getByLabel('Role for other@example.test').locator('xpath=ancestor::tr');
    await refreshedOtherRow.getByLabel('New password for other@example.test').fill('replacement-password');
>>>>>>> Stashed changes
    await refreshedOtherRow.getByRole('button', { name: 'Set password' }).click();
    await expect(refreshedOtherRow.getByText('Password updated.')).toBeVisible();

    await page.getByPlaceholder('user@example.com').fill('new@example.test');
    await page.getByPlaceholder('Temporary password').fill('initial-password');
    await page.getByRole('button', { name: 'Add user' }).click();
    await expect(page.getByLabel('Email for new@example.test')).toBeVisible();

    page.on('dialog', (dialog) => dialog.accept());
    const createdRow = page.getByLabel('Role for new@example.test').locator('xpath=ancestor::tr');
    await createdRow.getByRole('button', { name: 'Delete' }).click();
    await expect(page.getByLabel('Email for new@example.test')).toHaveCount(0);
  });
});