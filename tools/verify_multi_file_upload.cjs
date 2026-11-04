const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// Uses mocked APIs: this check never creates or modifies real business data.
(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const output = path.resolve('var/acceptance/multi-file-upload');
  fs.mkdirSync(output, { recursive: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [], requests = [];
  let rejectNextUpload = false;
  const temporaryFiles = [];
  function sizedFile(name, size) {
    const filename = path.join(output, name), fd = fs.openSync(filename, 'w');
    try { fs.ftruncateSync(fd, size); } finally { fs.closeSync(fd); }
    temporaryFiles.push(filename);
    return filename;
  }
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.uploadedFiles = [];
    const originalFetch = window.fetch;
    window.fetch = (url, init) => {
      if (String(url).endsWith('/standardization-tasks') && init?.body instanceof FormData) {
        window.uploadedFiles = Array.from(init.body.entries()).filter(([key]) => key === 'excel_file').map(([, value]) => ({ name: value.name, size: value.size }));
      }
      return originalFetch(url, init);
    };
  });
  const profile = { name: '商品标准字段', fixed_fields: [], local_categories: [] };
  const task = { id: 'test-multi', revision: 1, status: 'mapping', exports: [], history: [],
    payload: { name: '多文件测试', filename: 'A.xlsx；C.xlsx', workflow: 'standardization',
      config: { fields: [], outputs: [], preserve_extras: true }, sheets: [], record_count: 0, error_count: 0 } };
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), pathname = new URL(request.url()).pathname;
    let data;
    if (pathname.endsWith('/auth/session')) data = { authenticated: true, role: 'owner', username: '测试管理员' };
    else if (pathname.endsWith('/auth/csrf')) data = { csrf_token: 'test' };
    else if (pathname.endsWith('/workflow-settings')) data = { can_manage_templates: true };
    else if (pathname.endsWith('/field-profiles')) data = {items:[{id:'profile-test',is_default:true,revision:1,payload:{version:1,published:profile,draft:profile,published_template_name:'商品标准字段'}}]};
    else if (pathname.endsWith('/field-profile')) data = { revision: 1, payload: { version: 1, published: profile, draft: profile } };
    else if (pathname.endsWith('/standardization-tasks')) {
      requests.push(request.method());
      if (rejectNextUpload) { rejectNextUpload = false; return route.fulfill({ status: 429, json: {detail:'当前处理名额已满，请稍后重试。',code:'RESOURCE_BUSY'} }); }
      data = task;
    }
    else if (pathname.endsWith('/workflow-tasks/test-multi')) data = task;
    else if (pathname.endsWith('/workflow-tasks')) data = { items: [], total: 0 };
    else throw new Error(`Unexpected API: ${pathname}`);
    await route.fulfill({ status: pathname.endsWith('/standardization-tasks') ? 202 : 200, json: data });
  });
  const file = (name, size = 8) => ({ name, mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.alloc(size) });
  try {
    await page.goto((process.env.STANDARDIZATION_WEB || 'http://127.0.0.1:5173') + '/#workbench');
    await page.getByRole('button', { name: '新建任务', exact: true }).first().click();
    const input = page.getByLabel('选择原始Excel', { exact: true });
    assert.equal(await input.getAttribute('multiple'), '');
    await input.setInputFiles([file('A.xlsx'), file('B.xlsx')]);
    await page.getByRole('status').filter({ hasText: '已选择 2 个文件' }).waitFor();
    await page.getByRole('button', { name: '移除第2个文件 B.xlsx', exact: true }).click();
    await input.setInputFiles(file('C.xlsx'));
    assert.deepEqual(await page.locator('.selected-files li > span').allTextContents(), ['A.xlsx', 'C.xlsx']);
    await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    await page.getByRole('button', { name: '上传并分析', exact: true }).click();
    await page.waitForURL('**/#task/test-multi');
    assert.equal(requests.length, 1);
    assert.equal(requests[0], 'POST');
    assert.deepEqual(await page.evaluate(() => window.uploadedFiles), [{ name: 'A.xlsx', size: 8 }, { name: 'C.xlsx', size: 8 }]);
    await page.goto((process.env.STANDARDIZATION_WEB || 'http://127.0.0.1:5173') + '/#workbench');
    await page.getByRole('button', { name: '新建任务', exact: true }).first().click();
    await input.setInputFiles(Array.from({ length: 31 }, (_, i) => file(`${i}.xlsx`)));
    await page.getByRole('alert').filter({ hasText: '最多选择 30 个' }).waitFor();
    assert.equal(await page.getByRole('button', { name: '上传并分析' }).isDisabled(), true);
    while (await page.locator('.selected-files li').count()) await page.locator('.selected-files button').first().click();
    await input.setInputFiles(sizedFile('accepted-21MiB.xlsx', 21 * 1024 * 1024));
    assert.equal(await page.getByRole('button', { name: '上传并分析' }).isDisabled(), false);
    await page.locator('.selected-files button').first().click();
    await input.setInputFiles(sizedFile('accepted-100MiB.xlsx', 100 * 1024 * 1024));
    assert.equal(await page.getByRole('button', { name: '上传并分析' }).isDisabled(), false);
    await page.locator('.selected-files button').first().click();
    await input.setInputFiles(sizedFile('too-large.xlsx', 100 * 1024 * 1024 + 1));
    await page.getByRole('alert').filter({ hasText: '单个 Excel 文件不能超过 100 MiB' }).waitFor();
    assert.equal(await page.getByRole('button', { name: '上传并分析' }).isDisabled(), true);
    await page.locator('.selected-files button').first().click();
    for (let pair = 0; pair < 3; pair++) {
      await input.setInputFiles([sizedFile(`total-${pair}-a.xlsx`, 85 * 1024 * 1024), sizedFile(`total-${pair}-b.xlsx`, 85 * 1024 * 1024)]);
    }
    await page.getByRole('alert').filter({ hasText: '合计超过 500 MiB' }).waitFor();
    assert.equal(await page.getByRole('button', { name: '上传并分析' }).isDisabled(), true);
    await page.locator('.selected-files button').first().click();
    assert.equal(await page.getByRole('button', { name: '上传并分析' }).isDisabled(), false);
    while (await page.locator('.selected-files li').count()) await page.locator('.selected-files button').first().click();
    await input.setInputFiles(file('wrong.csv'));
    await page.getByRole('alert').filter({ hasText: '请选择 .xlsx 文件' }).waitFor();
    assert.equal(await page.getByRole('button', { name: '上传并分析' }).isDisabled(), true);
    await page.locator('.selected-files button').first().click();
    await input.setInputFiles(file('retry.xlsx'));
    rejectNextUpload = true;
    await page.getByRole('button', { name: '上传并分析', exact: true }).click();
    await page.getByRole('status').filter({ hasText: '正在等待空闲上传通道' }).waitFor();
    await page.waitForURL('**/#task/test-multi', {timeout: 15000});
    assert.deepEqual(requests, ['POST', 'POST', 'POST']);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, filesInRequest: 2, requests: requests.length, browserErrors: errors, screenshots: output }));
  } finally { await browser.close(); for (const filename of temporaryFiles) fs.unlinkSync(filename); }
})().catch(error => { console.error(error); process.exitCode = 1; });
