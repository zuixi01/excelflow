const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const out=path.resolve('outputs/return-diff-20261103');fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 try{
  await page.route('**/api/v1/workflow-tasks/qa/maintenance/returns/preview/details?*',async route=>{
   const url=new URL(route.request().url()),kind=url.searchParams.get('kind'),offset=Number(url.searchParams.get('offset'));
   const total=kind==='changed'?12:1;
   const items=Array.from({length:Math.min(10,total-offset)},(_,i)=>({id:`row_${offset+i}`,name:`测试筒灯 ${offset+i+1}`,sku:`L${offset+i+1}`,location:{sheet:'维护表',row:offset+i+4},original_location:{sheet:'维护表',row:offset+i+4},cells:{price:{sheet:'维护表',row:offset+i+4,column:3,letter:'C',address:`C${offset+i+4}`},power:{sheet:'维护表',row:offset+i+4,column:4,letter:'D',address:`D${offset+i+4}`}},...(kind==='changed'?{before:{price:'10',power:'3W'},after:{price:'12',power:'5W'}}:{values:{product_name:'测试筒灯',price:'12'}})}));
   await route.fulfill({json:{items,total,offset,limit:10}});
  });
  await page.route('**/diff-test',route=>route.fulfill({contentType:'text/html',body:`<html><meta charset="utf-8"><body class="sop" style="display:block;padding:24px"><div id="test"></div><script type="module">
   import {createApp,reactive} from '/node_modules/.vite/deps/vue.js';
   import Component from '/src/workflow/ReturnDiffDetails.vue';
   import '/src/workflow/style.css';
   const props=reactive({taskId:'qa',previewId:'preview',counts:{changed:12,added:1,missing:1},deleteMissing:false,fieldTitles:{product_name:'商品名称',price:'售价',power:'功率'},complete:true,fallback:{changed:[],added:[],missing:[]}});
   window.qaProps=props;createApp(Component,props).mount('#test');
  </script></body></html>`}));
  await page.goto('http://127.0.0.1:5178/diff-test');
  await page.getByText('测试筒灯 1',{exact:true}).first().waitFor();
  assert.equal(await page.locator('.diff-table tbody tr').count(),20);
  assert.equal(await page.locator('.diff-before').first().innerText(),'10');
  assert.equal(await page.locator('.diff-after').first().innerText(),'12');
  await page.getByRole('button',{name:'下一页',exact:true}).click();
  await page.getByText('测试筒灯 12',{exact:true}).first().waitFor();
  assert.equal(await page.locator('.diff-table tbody tr').count(),4);
  await page.getByRole('button',{name:'新增行（1 行）',exact:true}).click();
  await page.getByText('查看这一行内容',{exact:true}).click();
  await page.getByText('商品名称：测试筒灯',{exact:true}).waitFor();
  await page.getByRole('button',{name:'文件中删除的行（1 行）',exact:true}).click();
  await page.getByText('这 1 条记录默认保留，不会自动删除',{exact:true}).waitFor();
  await page.screenshot({path:path.join(out,'缺少记录默认保留.png')});
  // Root props are copied by Vue: update the mounted component's reactive parent.
  await page.evaluate(()=>{document.querySelector('#test').__vue_app__._instance.props.deleteMissing=true;});
  await page.getByText('确认后将删除 1 条缺少记录',{exact:true}).waitFor();
  await page.getByRole('button',{name:'修改内容（12 行）',exact:true}).click();
  await page.getByText('测试筒灯 1',{exact:true}).first().waitFor();
  await page.screenshot({path:path.join(out,'修改前后对照.png')});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(out,'UI验证.json'),JSON.stringify({passed:true,paging:true,added:true,missing_default_preserve:true,delete_warning:true,before_after:true,errors},null,2));
  console.log('PASS: before/after, pagination, added, missing, delete warning; no page errors');
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1;});
