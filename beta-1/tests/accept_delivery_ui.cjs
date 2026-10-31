// SPDX-License-Identifier: AGPL-3.0-only
// Real bundled UI in headless Chromium; synthetic API responses, no external calls.
const fs=require('fs'),path=require('path'),http=require('http'),assert=require('assert');
const {chromium}=require(process.env.STATION_TEST_PLAYWRIGHT);
const [source,output]=process.argv.slice(2);fs.mkdirSync(output,{recursive:true});
const checks=[],calls=[];const id='11111111-1111-4111-8111-111111111111',meeting='22222222-2222-4222-8222-222222222222';
let records=[],uploads=[];let server,browser;
(async()=>{try{
 server=http.createServer((req,res)=>{const file={'/':'index.html','/operator.js':'operator.js','/operator.css':'operator.css'}[req.url];if(!file){res.writeHead(404);return res.end();}res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(path.join(source,file)));});await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const base=`http://127.0.0.1:${server.address().port}`;
 browser=await chromium.launch({headless:true});const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());assert.equal(url.origin,base,'No external browser requests');
  const p=url.pathname;if(!p.startsWith('/v1/')&&!p.startsWith('/operator/'))return route.continue();
  let value={};if(p==='/operator/catalog')value={meetings:[{id:meeting,title:'Synthetic council meeting'}],agenda:[],requests:[],can_capture:true,can_attach:false,max_seconds:60};
  else if(p==='/v1/live-sources')value=[{id:'chamber',label:'Synthetic chamber'}];
  else if(p==='/v1/civiccast-feeds')value=[{id:'peg',label:'Separate CivicCast'}];
  else if(p==='/v1/recordings'&&req.method()==='GET')value=records;
  else if(p==='/v1/youtube-uploads')value=uploads;
  else if(p==='/v1/schedules'||p==='/v1/playouts')value=[];
  else if(p==='/v1/on-air')value={mode:'schedule'};
  if(req.method()==='POST'){
   const body=req.postDataJSON();calls.push({path:p,body});
   if(p==='/v1/recordings')value={id,status:'queued'};
   if(p.endsWith('/youtube')){uploads=[{id:'upload-synthetic',recording_id:id,status:'uploaded',details:{privacy:body.privacy,video_id:'TestVideo01'}}];value=uploads[0];}
  }
  await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(value)});
 });
 await page.goto(base+'/#ticket=synthetic-browser-ticket');await page.locator('#meeting-id').selectOption(meeting);await page.locator('#civiccast-feed').selectOption('peg');await page.locator('#start').click();
 await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('Recording accepted'));
 const capture=calls.find(c=>c.path==='/v1/recordings');assert.equal(capture.body.civiccast_feed_id,'peg');assert.equal(capture.body.live_source_id,'chamber');checks.push('Compiled operator UI selects separate CivicCast feed and sends it with live capture');
 records=[{id,status:'complete',control:{},meeting_id:meeting,details:{meeting:{title:'Synthetic council meeting'},duration_seconds:5,size_bytes:123456,sha256:'a'.repeat(64),civiccast_delivery:{state:'stopped',bytes_sent:123456}}}];
 await page.locator('section#recordings').getByRole('button',{name:'Refresh',exact:true}).click();const form=page.locator('form.youtube-upload');await form.waitFor();assert.equal(await form.locator('select[name=privacy]').inputValue(),'private');await form.locator('[name=title]').fill('Reviewed synthetic recording');await form.locator('[name=approval_note]').fill('Synthetic staff approval');await form.getByRole('button',{name:'Upload to YouTube',exact:true}).click();
 await page.locator('[data-upload-status=uploaded]').waitFor();const upload=calls.find(c=>c.path.endsWith('/youtube'));assert.equal(upload.body.title,'Reviewed synthetic recording');assert.equal(upload.body.privacy,'private');assert.equal(upload.body.approval_note,'Synthetic staff approval');assert.ok(upload.body.idempotency_key);assert.equal(await page.getByRole('link',{name:'Open video',exact:true}).getAttribute('href'),'https://www.youtube.com/watch?v=TestVideo01');checks.push('Compiled operator UI requires explicit completed-recording upload, defaults private, submits approval/title/idempotency and shows returned video link');
 assert.deepEqual(errors,[]);fs.writeFileSync(path.join(output,'browser-result.json'),JSON.stringify({passed:true,checks,calls,headless:true,synthetic_api:true,external_requests:0},null,2));console.log(JSON.stringify(checks));
}finally{if(browser)await browser.close();if(server)await new Promise(r=>server.close(r));}})().catch(e=>{console.error(e);process.exitCode=1});
