// Real Vue + real read-only preview compiler; profile reads are isolated fixtures.
// No configuration saves, publications, task changes or model calls are permitted.
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const web = process.env.PREVIEW_TEST_WEB || 'http://127.0.0.1:5173';
let browser;
before(async () => { browser = await chromium.launch({ headless: true, channel: 'msedge' }); });
after(async () => { await browser?.close(); });
function field(id, title, extra = {}) {
  return { id, title, group: '', description: '', aliases: [], type: 'string', enabled: true, required: false,
    editable: true, default: null, enum_values: [], min: null, max: null, ...extra };
}
function profileFixture(state) {
  return { name: '供应商标准化', fixed_fields: [
    field('product_name', '商品名称', { required: true }), field('sku', '货号', { type: 'id_code' }),
    field('warranty', '质保天数', { group: '售后保障', type: 'integer' }), field('stock', '停用库存', { enabled: false }),
    ...(state.wide ? Array.from({ length: 40 }, (_, i) => field(`extra${i}`, `补充字段${i + 1}`)) : []),
  ], local_categories: state.fixedOnly ? [] : [
    { id: 'windows', title: '封窗', attributes: [field('color', '颜色'), field('material', '材质')], specifications: [field('size', '尺寸')] },
    { id: 'tiles', title: '瓷砖', attributes: [field('origin', '产地')], specifications: [field('model', '型号')] },
  ] };
}
async function setup(t, state = {}) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
  const errors = [], writes = [];
  page.on('pageerror', e => errors.push(e.message));
  t.after(async () => { await page.close(); assert.deepEqual(errors, []); assert.deepEqual(writes, [], 'preview must never save or publish'); });
  await page.route('**/api/v1/**', async route => {
    const req = route.request(), url = new URL(req.url());
    if (url.pathname.endsWith('/workflow-settings')) return route.fulfill({ json: { ai: { configured: false }, can_manage_templates: true, storage: 'local' } });
    if (url.pathname.endsWith('/field-profile') && req.method() === 'GET') {
      const profile = profileFixture(state);
      return route.fulfill({ json: { id: 'fieldconfig_fixture', revision: 3, status: 'draft', payload: { draft: profile, published: profile, version: 1 } } });
    }
    if (url.pathname.endsWith('/field-profile/preview') && req.method() === 'POST') {
      if (state.waitPreview) await state.waitPreview;
      if (state.rejectPreview) return route.fulfill({ status: 400, json: { detail: '字段名称重复，请调整后重试。', code: 'WORKFLOW_INVALID' } });
      // Forward only this stateless compilation request to the existing local API.
      return route.continue();
    }
    writes.push(req.method() + ' ' + url.pathname);
    return route.fulfill({ status: 500, json: { detail: 'Unexpected request in read-only preview test' } });
  });
  await page.goto(web + '/#fields');
  await page.getByRole('textbox', { name: '字段名称 1', exact: true }).first().waitFor();
  return page;
}
async function open(page) {
  await page.getByRole('button', { name: '预览结构', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '整体结构预览', exact: true });
  await dialog.waitFor({ timeout: 3000 });
  await dialog.getByRole('table', { name: /输出表头/ }).waitFor();
  return dialog;
}

test('preview opens a modal containing unsaved edits without saving or publishing', async t => {
  const page = await setup(t);
  const title = page.getByRole('textbox', { name: '字段名称 1', exact: true }).first();
  await title.fill('商品标题（草稿）');
  const dialog = await open(page);
  assert.equal(await dialog.evaluate(node => node instanceof HTMLDialogElement && node.open), true);
  assert.equal(await dialog.getByRole('columnheader', { name: /商品标题（草稿）/ }).count(), 1);
  assert.match(await dialog.innerText(), /未保存/);
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(await title.inputValue(), '商品标题（草稿）');
  assert.equal(await page.getByRole('button', { name: '预览结构', exact: true }).evaluate(node => node === document.activeElement), true);
});

test('all configured groups appear together in one complete ordered target structure', async t => {
  const page = await setup(t);
  const dialog = await open(page);
  assert.equal(await dialog.getByRole('combobox', { name: '预览类目', exact: true }).count(), 0);
  const table = dialog.getByRole('table', { name: /输出表头/ });
  const headers = await table.locator('thead th').allTextContents();
  assert.ok(headers.indexOf('固定字段') < headers.indexOf('动态字段'));
  assert.deepEqual(await table.locator('thead tr').nth(2).locator('th').allTextContents(), ['2', '商品名称*', '货号', '售后保障', '商品属性', '商品规格']);
  assert.equal(await table.getByRole('columnheader', { name: '固定字段', exact: true }).getAttribute('colspan'), '3');
  assert.equal(await table.getByRole('columnheader', { name: '动态字段', exact: true }).getAttribute('colspan'), '5');
  assert.equal(await table.getByRole('columnheader', { name: /商品名称/ }).getAttribute('rowspan'), '3');
  assert.equal(await table.getByRole('columnheader', { name: /停用库存/ }).count(), 0);
  assert.match(await dialog.locator('.schema-sheet-summary').innerText(), /8 个字段/);
  assert.equal(await table.getByRole('columnheader', { name: '产地', exact: true }).count(), 1);
  assert.equal(await table.getByRole('columnheader', { name: '型号', exact: true }).count(), 1);
  assert.equal(await table.getByRole('columnheader', { name: '颜色', exact: true }).count(), 1);
  assert.match(await dialog.innerText(), /全部动态字段/);
});

test('fixed-only preview explains that no dynamic fields are configured', async t => {
  const page = await setup(t, { fixedOnly: true });
  const dialog = await open(page);
  assert.equal(await dialog.getByRole('columnheader', { name: '动态字段', exact: true }).count(), 0);
  assert.match(await dialog.innerText(), /尚未配置动态字段/);
  await dialog.getByRole('button', { name: '关闭结构预览', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
});

test('preview opens immediately with loading feedback and can close before the response', async t => {
  let release;
  const state = { waitPreview: new Promise(resolve => { release = resolve; }) };
  const page = await setup(t, state);
  await page.getByRole('button', { name: '预览结构', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '整体结构预览', exact: true });
  try {
    await dialog.getByRole('status').filter({ hasText: /正在.*预览/ }).waitFor({ timeout: 3000 });
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
  } finally { release(); }
  await page.getByRole('button', { name: '预览结构', exact: true }).waitFor();
  assert.equal(await dialog.count(), 0, 'a late response must not reopen the preview');
});

test('preview failure is shown in the modal and retry can recover without publishing', async t => {
  const state = { rejectPreview: true };
  const page = await setup(t, state);
  await page.getByRole('button', { name: '预览结构', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '整体结构预览', exact: true });
  await dialog.getByRole('alert').waitFor({ timeout: 3000 });
  assert.match(await dialog.getByRole('alert').innerText(), /重复/);
  state.rejectPreview = false;
  await dialog.getByRole('button', { name: '重新预览', exact: true }).click();
  await dialog.getByRole('table', { name: /输出表头/ }).waitFor();
});

test('wide previews keep scrolling inside the modal and retain reachable close controls', async t => {
  const page = await setup(t, { wide: true });
  const dialog = await open(page);
  const output = path.resolve('var/acceptance/field-structure-preview');
  fs.mkdirSync(output, { recursive: true });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  for (const viewport of [{ width: 1440, height: 950 }, { width: 375, height: 812 }, { width: 812, height: 375 }]) {
    await page.setViewportSize(viewport);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    const box = await dialog.boundingBox();
    assert.ok(box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width && box.y + box.height <= viewport.height);
    const close = await dialog.getByRole('button', { name: '关闭结构预览', exact: true }).boundingBox();
    assert.ok(close.y >= 0 && close.y + close.height <= viewport.height);
    assert.ok(close.x > box.x + box.width - 90, 'close control must stay at the dialog top-right');
    const scroll = dialog.getByRole('region', { name: '输出表头横向预览', exact: true });
    assert.equal(await scroll.evaluate(node => node.scrollWidth > node.clientWidth), true);
    const groupLabel = await dialog.getByRole('columnheader', { name: '固定字段', exact: true }).evaluate(node => {
      const range = document.createRange(); range.selectNodeContents(node);
      const rect = range.getBoundingClientRect(); return { left: rect.left, right: rect.right };
    });
    assert.ok(groupLabel.left >= box.x && groupLabel.right <= box.x + box.width, 'merged group text must be visible even in a wide sheet');
    await scroll.evaluate(node => { node.scrollLeft = node.scrollWidth; });
    assert.ok(await scroll.evaluate(node => node.scrollLeft) > 0);
    await scroll.evaluate(node => { node.scrollLeft = 0; });
    await page.screenshot({ path: path.join(output, `preview-${viewport.width}.png`) });
  }
  await dialog.getByRole('button', { name: '关闭结构预览', exact: true }).focus();
  await page.keyboard.press('Shift+Tab');
  assert.equal(await dialog.evaluate(node => node.contains(document.activeElement)), true);
});
