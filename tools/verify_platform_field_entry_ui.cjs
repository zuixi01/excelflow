// Isolated browser check: all API calls are mocked; formal tasks are untouched.
const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');

(async()=>{
 const out=path.resolve('outputs/platform-field-entry-20261104');fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1400,height:1100}}),errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 const field=(id,title)=>({id,title,group:'',description:'',aliases:[],type:'string',required:false,editable:true,enabled:true,default:null,enum_values:[],min:null,max:null});
 let saved=0,published=0,connectionSaved=0,connectionPreviewed=0;
 let direct={base_url:'',path:'/fields',method:'GET',has_key:false,configured:false,http_allowed:false,private_network_allowed:false};
 let record={id:'fieldconfig_qa',revision:0,status:'draft',is_default:true,payload:{template_name:'通用商品模板',published_template_name:'通用商品模板',version:0,published:null,draft:{name:'商品标准化',fixed_fields:[field('product_name','商品名称')],local_categories:[],platform_source:null}}};
 try{
  await page.route('**/platform-field-entry-test',route=>route.fulfill({contentType:'text/html',body:`<html><meta charset="utf-8"><body class="sop" style="display:block;padding:24px"><div id="test"></div><script type="module">import {createApp} from '/node_modules/.vite/deps/vue.js';import Component from '/src/workflow/FieldConfiguration.vue';import '/src/workflow/style.css';createApp(Component,{canManage:true}).mount('#test');</script></body></html>`}));
  await page.route('**/api/v1/**',async route=>{
   const url=new URL(route.request().url()),endpoint=url.pathname,method=route.request().method();
   if(endpoint.endsWith('/auth/csrf'))return route.fulfill({json:{csrf_token:'isolated-qa'}});
   if(endpoint.endsWith('/field-profile/platform-direct')&&method==='GET')return route.fulfill({json:direct});
   if(endpoint.endsWith('/field-profile/platform-direct')&&method==='PUT'){
    const body=route.request().postDataJSON();connectionSaved++;assert.equal(body.api_key,'test-ui-key');assert.equal(body.base_url,'https://api.example.com');assert.equal(body.path,'/fields');
    direct={...direct,base_url:body.base_url,path:body.path,method:body.method,has_key:true,configured:true};
    return route.fulfill({json:direct});
   }
   if(endpoint.endsWith('/field-profile/platform-direct/preview')){
    connectionPreviewed++;return route.fulfill({json:{connection_id:'direct-platform',title:'直接连接的平台',schema_version:'v2',fetched_at:'2026-11-04T00:00:00+00:00',category_count:1,field_count:2,categories:[{id:'lighting',title:'灯具',attributes:[field('brand','品牌')],specifications:[field('power','功率')]}]}});
   }
   if(endpoint.endsWith('/field-profile/platform-connections'))return route.fulfill({json:{configured:true,items:[{id:'product-platform',title:'平台商品字段'}]}});
   if(endpoint.endsWith('/field-profile/platform-preview'))return route.fulfill({json:{connection_id:'product-platform',title:'平台商品字段',schema_version:'v2',fetched_at:'2026-11-04T00:00:00+00:00',category_count:1,field_count:2,categories:[{id:'lighting',title:'灯具',attributes:[field('brand','品牌')],specifications:[field('power','功率')]}]}});
   if(endpoint.endsWith('/field-profile')&&method==='GET')return route.fulfill({json:record});
   if(endpoint.endsWith('/field-profile')&&method==='PUT'){
    saved++;const body=route.request().postDataJSON();record={...record,revision:record.revision+1,payload:{...record.payload,draft:body.profile,template_name:body.profile.name}};
    return route.fulfill({json:record});
   }
   if(endpoint.endsWith('/field-profile/publish')&&method==='POST'){
    published++;record={...record,revision:record.revision+1,status:'published',payload:{...record.payload,published:record.payload.draft,version:1}};
    return route.fulfill({json:record});
   }
   return route.fulfill({status:404,json:{detail:'Unexpected API path: '+endpoint}});
  });
  await page.goto('http://127.0.0.1:5173/platform-field-entry-test');
  await page.getByRole('button',{name:'对接平台字段'}).click();
  const panel=page.getByRole('region',{name:'对接平台字段'});
  await panel.getByRole('textbox',{name:'字段接口完整地址'}).fill('https://api.example.com/fields');
  assert.equal(await panel.getByRole('textbox',{name:'字段接口路径'}).count(),0);
  await panel.getByLabel('API 密钥').fill('test-ui-key');
  await panel.getByRole('button',{name:'保存连接'}).click();
  await page.reload();
  await page.getByRole('button',{name:'对接平台字段'}).click();
  await panel.getByRole('textbox',{name:'字段接口完整地址'}).waitFor();
  assert.equal(await panel.getByRole('textbox',{name:'字段接口完整地址'}).inputValue(),'https://api.example.com/fields');
  await panel.getByRole('button',{name:'连接并预览字段'}).click();
  await page.getByText('1 个字段组 · 2 个字段',{exact:false}).waitFor();
  await page.getByText('商品属性：品牌',{exact:true}).waitFor({state:'hidden'});
  await page.getByText('查看字段名称').click();
  await page.getByText('商品属性：品牌',{exact:true}).waitFor();
  assert.equal(saved,0);assert.equal(published,0);
  await page.screenshot({path:path.join(out,'平台字段预览.png'),fullPage:true});
  await page.getByRole('button',{name:'导入到模板草稿'}).click();
  await page.getByRole('status').getByText(/已将 1 个字段组、2 个字段导入草稿/).waitFor();
  assert.equal(saved,0,'import must not auto-save');
  await page.getByRole('button',{name:'保存草稿'}).click();
  await page.getByRole('status').getByText(/模板草稿已保存/).waitFor();
  assert.equal(record.payload.draft.platform_source.connection_id,'direct-platform');
  await page.getByRole('button',{name:'发布模板'}).click();
  await page.getByRole('status').getByText(/已发布/).waitFor();
  assert.equal(saved,1);assert.equal(published,1);
  assert.equal(connectionSaved,1);assert.equal(connectionPreviewed,1);
  assert.equal(record.payload.published.local_categories[0].specifications[0].title,'功率');
  assert.equal(record.payload.published.platform_source.connection_id,'direct-platform');
  assert.deepEqual(errors,[]);
  console.log('PASS: preview platform categories → import draft → explicit save → explicit publish');
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
