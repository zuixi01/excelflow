const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const webRoot = process.env.TASK_PERMISSIONS_WEB || 'http://127.0.0.1:5173';
  const output = path.resolve('var/acceptance/task-permissions');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const errors = [];
  try {
    for (const role of ['owner', 'operator']) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      const tasks = ['alice', 'bob'].map(username => ({
        id: `task_${username}`, name: `${username} 商品整理`, filename: `${username}.xlsx`,
        status: 'failed', template_name: '商品标准化', record_count: 0, error_count: 0,
        updated_at: '2026-10-16T08:00:00Z', revision: 0, error: '解析失败',
        created_by_id: `user_${username}`, created_by_username: username,
      }));
      const visible = role === 'owner' ? tasks : tasks.slice(0, 1);
      await page.route('**/api/v1/**', async route => {
        const pathname = new URL(route.request().url()).pathname;
        let body;
        if (pathname === '/api/v1/auth/session') body = { authenticated: true, auth_required: true, username: role === 'owner' ? 'admin' : 'alice', user_id: role === 'owner' ? 'user_admin' : 'user_alice', role };
        else if (pathname === '/api/v1/workflow-settings') body = { can_manage_templates: role === 'owner' };
        else if (pathname === '/api/v1/workflow-definitions') body = { items: [] };
        else if (pathname === '/api/v1/field-profile') body = { payload: { version: 1, published: { fixed_fields: [], local_categories: [] } } };
        else if (pathname === '/api/v1/workflow-tasks') body = { items: visible, total: visible.length };
        else if (pathname === '/api/v1/workflow-tasks/task_alice') body = {
          ...tasks[0], exports: [], history: [], payload: {
            name: tasks[0].name, filename: tasks[0].filename, workflow: 'standardization',
            config: { name: '商品标准化', fields: [], outputs: [] }, template_version: 1,
            record_count: 0, error_count: 0, error: '解析失败',
          },
        };
        else throw new Error(`Unexpected request: ${pathname}`);
        await route.fulfill({ json: body });
      });
      await page.goto(webRoot);
      await page.getByRole('button', { name: tasks[0].name, exact: true }).waitFor();
      assert.equal(await page.locator('.data-table tbody tr').count(), visible.length);
      assert.equal(await page.getByRole('columnheader', { name: '创建账号', exact: true }).count(), role === 'owner' ? 1 : 0);
      if (role === 'owner') {
        assert.deepEqual(await page.locator('td.task-creator').allTextContents(), ['alice', 'bob']);
      }
      for (const [label, width, height] of [['desktop', 1440, 1000], ['mobile', 375, 812]]) {
        await page.setViewportSize({ width, height });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `${role} ${label} list overflow`);
        await page.screenshot({ path: path.join(output, `${role}-list-${label}.png`), fullPage: true });
      }
      await page.getByRole('button', { name: tasks[0].name, exact: true }).click();
      await page.getByRole('heading', { name: tasks[0].name, exact: true }).waitFor();
      const creator = page.locator('.page-heading .task-creator');
      assert.equal(await creator.count(), role === 'owner' ? 1 : 0);
      if (role === 'owner') assert.equal(await creator.textContent(), '创建账号：alice');
      for (const [label, width, height] of [['desktop', 1440, 1000], ['mobile', 375, 812]]) {
        await page.setViewportSize({ width, height });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `${role} ${label} detail overflow`);
        await page.screenshot({ path: path.join(output, `${role}-detail-${label}.png`), fullPage: true });
      }
      await context.close();
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ roles: ['owner', 'operator'], browserErrors: errors, screenshots: output }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
