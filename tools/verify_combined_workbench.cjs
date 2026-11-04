// Settings writes are intercepted: this check never changes real model credentials.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const output = path.resolve('var/acceptance/combined-workbench');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:5173/#workbench');
    await page.getByRole('heading', { name: '工作台', exact: true }).waitFor();
    const history = await (await page.request.get('http://127.0.0.1:5173/api/v1/workflow-tasks')).json();
    assert.ok(history.total > 0, 'original SOP history is available');
    await page.getByText(history.items[0].filename, { exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, 'sop-desktop.png'), fullPage: true });
    await page.getByRole('button', { name: 'SOP 流程配置', exact: true }).click();
    await page.getByRole('heading', { name: '模板中心', exact: true }).waitFor();
    await page.getByRole('button', { name: '工作台', exact: true }).click();
    await page.getByRole('tab', { name: '平台字段核对', exact: true }).click();
    await page.getByRole('heading', { name: '供应商 Excel 核对', exact: true }).waitFor();
    assert.ok(page.url().includes('mode=reconciliation'));
    await page.reload();
    await page.getByRole('heading', { name: '供应商 Excel 核对', exact: true }).waitFor();
    await page.getByRole('tab', { name: 'SOP 整理', exact: true }).click();
    await page.getByText(history.items[0].filename, { exact: true }).waitFor();
    await page.getByRole('button', { name: /继续处理|查看结果/ }).last().click();
    await page.waitForURL('**/#task/*');
    await page.getByRole('button', { name: /返回工作台/ }).click();
    await page.getByRole('heading', { name: '工作台', exact: true }).waitFor();

    for (const viewport of [{ width: 375, height: 812 }, { width: 812, height: 375 }]) {
      await page.setViewportSize(viewport);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({ path: path.join(output, `sop-${viewport.width}.png`), fullPage: true });
    }

    const settingsPage = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    settingsPage.on('pageerror', error => errors.push(error.message));
    let httpAllowed = true, saved = 0, modelRequests = 0;
    let state = { base_url: '', model: '', has_key: false, json_mode: true, include_samples: false };
    await settingsPage.route('**/api/v1/workflow-settings', route => route.fulfill({ json: { ai: { ...state, http_allowed: httpAllowed }, can_manage_templates: true, storage: 'local' } }));
    await settingsPage.route('**/api/v1/workflow-settings/ai', async route => {
      assert.equal(route.request().method(), 'PUT');
      const body = route.request().postDataJSON();
      saved++;
      state = { ...state, base_url: body.base_url, model: body.model, has_key: true };
      await new Promise(resolve => setTimeout(resolve, 350));
      await route.fulfill({ json: state });
    });
    await settingsPage.route('**/api/v1/workflow-settings/ai/models', async route => {
      modelRequests++;
      await new Promise(resolve => setTimeout(resolve, 500));
      await route.fulfill({ json: { models: ['test-model'] } });
    });
    await settingsPage.goto('http://127.0.0.1:5173/#settings');
    await settingsPage.getByLabel('接口地址', { exact: true }).fill('http://models.example:18080/v1');
    await settingsPage.getByLabel('API 密钥', { exact: true }).fill('test-placeholder-not-a-real-key');
    await settingsPage.getByLabel('模型名称', { exact: true }).fill('test-model');
    await settingsPage.getByRole('button', { name: '连接并读取模型', exact: true }).click();
    await settingsPage.getByRole('button', { name: '正在连接…', exact: true }).waitFor();
    await settingsPage.getByText('连接成功，读取到 1 个模型。', { exact: true }).waitFor();
    assert.equal(saved, 1);
    assert.equal(modelRequests, 1);
    await settingsPage.getByText('已保存密钥', { exact: true }).waitFor();
    await settingsPage.getByRole('button', { name: '保存模型配置', exact: true }).click();
    await settingsPage.getByText('模型配置已保存。', { exact: true }).waitFor();
    await settingsPage.screenshot({ path: path.join(output, 'settings-http.png'), fullPage: true });
    assert.equal(saved, 2);

    httpAllowed = false;
    await settingsPage.reload();
    await settingsPage.getByRole('button', { name: '保存模型配置', exact: true }).click();
    await settingsPage.getByRole('alert').filter({ hasText: '当前环境未允许 HTTP 模型接口' }).waitFor();
    assert.equal(saved, 2, 'blocked HTTP is not submitted');
    await settingsPage.setViewportSize({ width: 375, height: 812 });
    assert.equal(await settingsPage.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    await settingsPage.screenshot({ path: path.join(output, 'settings-blocked-mobile.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ originalSopRecords: history.total, settingsWrites: 'mocked only', browserErrors: errors, screenshots: output }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
