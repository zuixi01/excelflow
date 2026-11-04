// Run against the local demo API and Vite server with Playwright available in NODE_PATH.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const output = path.resolve('var/field-demo/acceptance');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:5173/#workbench?mode=reconciliation');
    await page.getByText('演示平台 · 商品字段', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '模板中心', exact: true }).count(), 0);
    await page.screenshot({ path: path.join(output, 'desktop-upload.png'), fullPage: true });
    await page.getByLabel('选择供应商 Excel 文件').setInputFiles(path.resolve('var/field-demo/supplier-demo.xlsx'));
    await page.getByRole('button', { name: '开始核对', exact: true }).click();
    await page.getByRole('button', { name: '确认字段并继续', exact: true }).waitFor({ timeout: 30000 });
    await page.getByLabel('供应商报价第4列对应平台字段').selectOption('material');
    await page.getByRole('button', { name: '确认字段并继续', exact: true }).click();
    await page.getByText('通过 2 条 · 不通过 1 条 · 待确认 0 条', { exact: true }).waitFor({ timeout: 30000 });
    await page.getByRole('tab', { name: '问题 1', exact: true }).click();
    await page.getByText('待确认', { exact: true }).first().waitFor();
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: '结果 Excel', exact: true }).click();
    const download = await downloadPromise;
    await download.saveAs(path.join(output, 'result.xlsx'));
    assert.ok(fs.statSync(path.join(output, 'result.xlsx')).size > 1000);
    const taskUrl = page.url();
    assert.ok(taskUrl.includes('mode=reconciliation'));
    await page.reload();
    await page.getByText('通过 2 条 · 不通过 1 条 · 待确认 0 条', { exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, 'desktop-result.png'), fullPage: true });
    for (const viewport of [{ width: 375, height: 812 }, { width: 812, height: 375 }, { width: 768, height: 1024 }]) {
      await page.setViewportSize(viewport);
      await page.emulateMedia({ reducedMotion: 'reduce' });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
      assert.equal(overflow, false, `page overflow at ${viewport.width}x${viewport.height}`);
      const outside = await page.locator('.reconcile-page button:visible').evaluateAll(buttons => buttons.some(button => !button.closest('.table-scroll') && button.getBoundingClientRect().right > innerWidth + 1));
      assert.equal(outside, false, 'action outside viewport');
      await page.screenshot({ path: path.join(output, `result-${viewport.width}x${viewport.height}.png`), fullPage: true });
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ taskUrl, download: path.join(output, 'result.xlsx'), browserErrors: errors, screenshots: output }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
