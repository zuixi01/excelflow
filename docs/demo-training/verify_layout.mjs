import {createRequire} from 'node:module';
import {fileURLToPath, pathToFileURL} from 'node:url';
import path from 'node:path';
const dir=path.dirname(fileURLToPath(import.meta.url));
const require=createRequire('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/_resolve.cjs');
const {chromium}=require('playwright');
const browser=await chromium.launch({headless:true,channel:'msedge'});
try {
 const page=await browser.newPage({viewport:{width:1200,height:900},deviceScaleFactor:1});
 const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(pathToFileURL(path.join(dir,'演示讲义.html')).href);
 await page.screenshot({path:path.join(dir,'reading-preview.png')});
 const check=await page.evaluate(()=>({headings:document.querySelectorAll('h2').length,diagrams:document.querySelectorAll('figure svg').length,brokenImages:[...document.images].filter(i=>!i.complete||i.naturalWidth===0).length,overflow:document.documentElement.scrollWidth>innerWidth}));
 if(check.headings!==12||check.diagrams!==3||check.brokenImages||check.overflow) throw Error(JSON.stringify(check));
 for(const name of ['architecture','workflow','mapping']){
   await page.goto(pathToFileURL(path.join(dir,`${name}.svg`)).href);
   const size=await page.locator('svg').evaluate(el=>({width:el.viewBox.baseVal.width,height:el.viewBox.baseVal.height}));
   await page.setViewportSize(size);
   await page.screenshot({path:path.join(dir,`${name}.png`)});
   const clipped=await page.locator('svg').evaluate(el=>[...el.querySelectorAll('text')].filter(t=>{const b=t.getBBox();return b.x<0||b.y<0||b.x+b.width>el.viewBox.baseVal.width||b.y+b.height>el.viewBox.baseVal.height}).map(t=>t.textContent));
   if(clipped.length)throw Error(`${name}: ${JSON.stringify(clipped)}`);
 }
 await page.setViewportSize({width:390,height:844});
 await page.goto(pathToFileURL(path.join(dir,'演示讲义.html')).href);
 const mobileOverflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
 console.log(JSON.stringify({desktop:check,mobileOverflow,pageErrors:errors}));
 if(mobileOverflow||errors.length)throw Error('layout check failed');
} finally {await browser.close();}
