const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const path=require('node:path'),fs=require('node:fs'),assert=require('node:assert/strict');
const out=path.resolve('outputs/production-hardening-20261103');
const report=JSON.parse(fs.readFileSync(path.join(out,'actual-11/验收结果.json'),'utf8'));
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1500,height:1000}}), errors=[], statuses=[];
 page.setDefaultTimeout(30000);
 page.on('pageerror',e=>errors.push(e.message));
 page.on('response',r=>{if(r.url().includes('/maintenance/download'))statuses.push(r.status())});
 try{
  await page.goto(`http://127.0.0.1:5178/#task/${report.task_id}`);
  await page.getByLabel('账号',{exact:true}).fill('acceptance');
  await page.getByLabel('密码',{exact:true}).fill('isolated-acceptance-password');
  await page.getByRole('button',{name:'进入工作台',exact:true}).click();
  await page.locator('button[title="查看维护数据"]').click();
  const button=page.getByRole('button',{name:'下载最新维护表',exact:true});
  const received=page.waitForEvent('download',{timeout:180000});
  await button.click();
  await page.getByText('操作已提交，正在排队或后台处理，请勿重复提交。',{exact:true}).waitFor();
  await page.screenshot({path:path.join(out,'后台处理状态.png'),fullPage:false});
  const file=await received;await file.saveAs(path.join(out,'浏览器下载维护表.xlsx'));
  assert(statuses.includes(202),JSON.stringify(statuses));
  assert(fs.statSync(path.join(out,'浏览器下载维护表.xlsx')).size>1000000);
  await page.reload();
  await page.getByText('最近处理记录（刷新或重新打开页面后可继续查看结果）',{exact:true}).click();
  const recovered=page.waitForEvent('download',{timeout:180000});
  await page.getByRole('button',{name:'下载维护表',exact:true}).first().click();
  await recovered;
  assert.equal(errors.length,0,JSON.stringify(errors));
  await page.screenshot({path:path.join(out,'刷新后继续下载.png'),fullPage:false});
  fs.writeFileSync(path.join(out,'浏览器验收.json'),JSON.stringify({passed:true,async_http_202:true,download_completed:true,resume_after_reload:true,page_errors:errors},null,2));
  console.log('PASS async download, visible status, reload and resumed download');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
