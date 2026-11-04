// Run with NODE_PATH pointing to the bundled Playwright packages.
// The real Vue page runs against deterministic HTTP fixtures; no user data is written.
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const web = process.env.TRANSFER_TEST_WEB || 'http://127.0.0.1:5173';
let browser;
before(async () => { browser = await chromium.launch({ headless: true, channel: 'msedge' }); });
after(async () => { await browser?.close(); });

function taskFixture(revision) {
  const fields = [
    { rule: { name: 'product_name', title: '商品名称', type: 'text', required: true, aliases: [], enum_values: [] }, editable: true, default: '', hidden: false },
    { rule: { name: 'warranty_days', title: '售后保障/质保天数', type: 'number', required: false, aliases: [], enum_values: [] }, editable: true, default: '', hidden: false },
  ];
  return {
    id: 'task_transfer_fixture', status: 'maintenance', revision, updated_at: '2026-10-15T08:00:00Z',
    payload: { name: '回传验收', filename: '供应商.xlsx', workflow: 'standardization', template_version: 1,
      template_hash: 'fixture', stage: 'maintenance', record_count: 20, error_count: 20,
      field_snapshot: { version: 1, dynamic_source: 'none', profile: { name: '验收', fixed_fields: [], local_categories: [] } },
      config: { name: '验收字段', description: '', fields, import_rules: [], outputs: [], unique_key: [], cross_checks: [], preserve_extras: false },
      sheets: [], mapping: { base_revision: 0, sheets: [] } }, exports: [], history: [],
  };
}
async function setup(t, state = {}) {
  state.revision ??= 2;
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  t.after(() => page.close());
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  t.after(() => assert.deepEqual(errors, []));
  await page.route('**/api/v1/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname;
    const json = body => route.fulfill({ json: body });
    if (path.endsWith('/workflow-settings')) return json({ ai: { configured: false, has_key: false }, can_manage_templates: true, storage: 'local' });
    if (path.endsWith('/task_transfer_fixture')) {
      const task = taskFixture(state.revision);
      if (state.exportFiles) task.exports = [{ id: 'export_fixture', status: 'completed', created_at: '2026-10-15T08:00:00Z',
        payload: { revision: state.revision, omitted_fields: [], artifacts: state.exportFiles.map(name => ({ name, size: 20, sheets: [] })) } }];
      return json(task);
    }
    if (path.includes('/exports/export_fixture/files/')) return route.fulfill({
      contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', body: Buffer.from('export fixture'),
    });
    if (path.endsWith('/maintenance/download')) return route.fulfill({
      contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      headers: { 'content-disposition': `attachment; filename*=utf-8''${encodeURIComponent(`回传验收_维护表_R${state.revision}_fixture.xlsx`)}` },
      body: Buffer.from('download fixture'),
    });
    if (path.endsWith('/maintenance/returns')) {
      if (state.waitUpload) await state.waitUpload;
      if (state.rejectUpload) return route.fulfill({ status: state.rejectUpload, json: { detail: state.rejectUpload === 409 ? '维护文件已过期，请下载最新版本并重新应用修改。' : '维护表列结构已改变，请保留原列和表头。', code: state.rejectUpload === 409 ? 'REVISION_CONFLICT' : 'WORKFLOW_ERROR' } });
      return json({ id: 'return_fixture', status: 'ready', revision: 0, payload: { task_id: 'task_transfer_fixture', base_revision: state.revision,
        counts: { changed: 1, added: 0, missing: 0 }, samples: [{ id: 'row_1', before: { warranty_days: '5840天' }, after: { warranty_days: '5840' } }] } });
    }
    if (path.endsWith('/maintenance/returns/apply')) {
      if (state.rejectApply) return route.fulfill({ status: 409, json: { detail: '数据已有新版本，请刷新后重新应用修改；本次修改未覆盖新数据。', code: 'REVISION_CONFLICT' } });
      state.revision++;
      state.applied = true;
      return json(taskFixture(state.revision));
    }
    if (path.endsWith('/maintenance')) return json({ revision: state.revision, total: 20,
      items: Array.from({ length: 20 }, (_, i) => ({ id: `row_${i + 1}`, ordinal: i + 1,
        values_json: { product_name: `商品${i + 1}`, warranty_days: state.applied ? '5840' : '5840天' },
        original: { product_name: `商品${i + 1}`, warranty_days: '5840天' }, source: { sheet: '封窗', row: i + 2 },
        issues: state.applied ? [] : [{ field: 'warranty_days', title: '质保天数', message: '请填写有效数值。', severity: 'error' }] })) });
    throw new Error(`Unexpected test API request: ${req.method()} ${path}`);
  });
  await page.goto(web + '/#task/task_transfer_fixture');
  await page.locator('.file-button input').waitFor();
  await page.locator('.sticky-actions').scrollIntoViewIfNeeded();
  return page;
}
const upload = page => page.locator('.file-button input').setInputFiles({ name: '旧维护表.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('upload fixture') });

for (const exportFiles of [['标准商品表.xlsx'], ['商品表.xlsx', '库存表.xlsx']]) {
  test(`exports download ${exportFiles.length} Excel file(s) directly without a ZIP action`, async t => {
    const page = await setup(t, { exportFiles });
    await page.getByRole('button', { name: '最终表与下载', exact: true }).click();
    for (const filename of exportFiles) {
      const pending = page.waitForEvent('download');
      await page.getByRole('button', { name: `↓ ${filename}`, exact: true }).click();
      assert.equal((await pending).suggestedFilename(), filename);
      assert.equal(await page.getByRole('button', { name: `预览 ${filename}`, exact: true }).count(), 1);
    }
    assert.equal(await page.getByRole('button', { name: /下载整套|压缩包|\.zip/i }).count(), 0, 'exports should expose direct file downloads only');
  });
}

test('download uses the server snapshot filename even after another edit changes the revision', async t => {
  const state = { revision: 2 };
  const page = await setup(t, state);
  state.revision = 3;
  const pending = page.waitForEvent('download');
  await page.getByRole('button', { name: /下载.*维护表/ }).first().click();
  const file = await pending;
  assert.equal(file.suggestedFilename(), '回传验收_维护表_R3_fixture.xlsx');
});

test('upload progress and stale-file recovery are visible beside the upload control', async t => {
  let release;
  const state = { rejectUpload: 409, waitUpload: new Promise(resolve => { release = resolve; }) };
  const page = await setup(t, state);
  await upload(page);
  try {
    await page.locator('.maintenance-transfer').getByRole('status').filter({ hasText: /正在.*旧维护表/ }).waitFor({ timeout: 3000 });
    assert.equal(await page.locator('.file-button input').isDisabled(), true);
  } finally { release(); }
  const alert = page.locator('.maintenance-transfer').getByRole('alert');
  await alert.waitFor({ timeout: 3000 });
  assert.match(await alert.innerText(), /过期/);
  assert.equal(await page.getByRole('dialog', { name: '维护表回传预览' }).count(), 0);
  const box = await alert.boundingBox();
  assert.ok(box.y >= 0 && box.y + box.height <= 768, 'error must be visible at the footer');
  const pending = page.waitForEvent('download');
  await alert.getByRole('button', { name: '下载最新版', exact: true }).click();
  assert.match((await pending).suggestedFilename(), /_R2_/);
  await page.locator('.maintenance-transfer').getByRole('status').filter({ hasText: /已下载/ }).waitFor();
});

test('successful upload opens preview and successful apply shows its new revision beside the control', async t => {
  const page = await setup(t);
  await upload(page);
  const dialog = page.getByRole('dialog', { name: '维护表回传预览' });
  await dialog.waitFor();
  await dialog.getByRole('button', { name: '确认并保存新修订' }).click();
  await dialog.waitFor({ state: 'hidden' });
  await page.locator('.maintenance-transfer').getByRole('status').filter({ hasText: /R3/ }).waitFor({ timeout: 3000 });
});

test('non-version upload errors stay local and retrying the same filename can succeed', async t => {
  const state = { rejectUpload: 400 };
  const page = await setup(t, state);
  await upload(page);
  const alert = page.locator('.maintenance-transfer').getByRole('alert');
  await alert.waitFor({ timeout: 3000 });
  assert.match(await alert.innerText(), /列结构/);
  assert.equal(await alert.getByRole('button', { name: '下载最新版' }).count(), 0);
  state.rejectUpload = 0;
  await upload(page);
  await page.getByRole('dialog', { name: '维护表回传预览' }).waitFor();
});

test('a concurrent edit during preview leaves an actionable error inside the dialog', async t => {
  const page = await setup(t, { rejectApply: true });
  await upload(page);
  const dialog = page.getByRole('dialog', { name: '维护表回传预览' });
  await dialog.waitFor();
  await dialog.getByRole('button', { name: '确认并保存新修订' }).click();
  await dialog.getByRole('alert').waitFor({ timeout: 3000 });
  assert.match(await dialog.getByRole('alert').innerText(), /新版本/);
  const pending = page.waitForEvent('download');
  await dialog.getByRole('button', { name: '下载最新版' }).click();
  assert.match((await pending).suggestedFilename(), /_R2_/);
  await dialog.waitFor({ state: 'hidden' });
});

test('the local error and recovery button fit desktop, phone and landscape viewports', async t => {
  const page = await setup(t, { rejectUpload: 409 });
  await upload(page);
  await page.locator('.maintenance-transfer').getByRole('alert').waitFor();
  const output = path.resolve('var/acceptance/maintenance-transfer');
  fs.mkdirSync(output, { recursive: true });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  for (const viewport of [{ width: 1366, height: 768 }, { width: 375, height: 812 }, { width: 812, height: 375 }]) {
    await page.setViewportSize(viewport);
    await page.locator('.maintenance-transfer').scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    const alert = await page.locator('.maintenance-transfer').getByRole('alert').boundingBox();
    assert.ok(alert.x >= 0 && alert.x + alert.width <= viewport.width, 'feedback must fit the viewport');
    const button = await page.getByRole('button', { name: '下载最新版', exact: true }).boundingBox();
    assert.ok(button.y >= 0 && button.y + button.height <= viewport.height, 'recovery must remain reachable');
    await page.screenshot({ path: path.join(output, `error-${viewport.width}.png`) });
  }
});
