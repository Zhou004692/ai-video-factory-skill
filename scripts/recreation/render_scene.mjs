/** Owned headless-browser task renderer. Node 22 built-ins only. */
import {createServer} from 'node:http';
import {readFile,writeFile,mkdtemp,mkdir,rm} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import path from 'node:path';
import os from 'node:os';

const options={};
for(let i=2;i<process.argv.length;i+=2)options[process.argv[i].replace(/^--/,'')]=process.argv[i+1];
for(const key of ['scene','assets','output','browser'])if(!options[key])throw Error('Missing --'+key);
if(Number(process.versions.node.split('.')[0])<22)throw Error('Node 22+ required');
const assets=path.resolve(options.assets),output=path.resolve(options.output);
const scene=await readFile(options.scene);
const profile=await mkdtemp(path.join(os.tmpdir(),'animal-recreation-render-'));
let child,ws,server,stderr='';
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const pending=new Map();let serial=0;
const deadline=Date.now()+Number(options['timeout-ms']||170000);
function checkDeadline(){if(Date.now()>=deadline)throw Error('Owned canvas render exceeded its deadline');}
function command(method,params={}){
  checkDeadline();
  const id=++serial;
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timed out: '+method));},Math.min(120000,deadline-Date.now()));
    pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));
  });
}
async function evaluate(expression){
  const result=await command('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
  if(result.exceptionDetails)throw Error('Canvas task failed: '+JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
try{
  server=createServer(async(req,res)=>{
    try{
      const pathname=new URL(req.url,'http://127.0.0.1').pathname;
      if(pathname==='/'){
        res.setHeader('Content-Type','text/html');
        res.end('<!doctype html><meta charset="utf-8"><canvas></canvas><script>window.addEventListener("error",e=>window.taskError=String(e.message||"Module load failed"));window.addEventListener("unhandledrejection",e=>window.taskError=String(e.reason));</script><script type="module" src="/assets/scene.mjs"></script>');return;
      }
      if(pathname==='/scene.json'){res.setHeader('Content-Type','application/json');res.end(scene);return;}
      if(!pathname.startsWith('/assets/'))throw Error('Unknown route');
      const file=path.resolve(assets,decodeURIComponent(pathname.slice(8)));
      if(!file.startsWith(assets+path.sep))throw Error('Asset path escape');
      res.setHeader('Content-Type',file.endsWith('.js')||file.endsWith('.mjs')?'text/javascript':'text/plain');
      res.end(await readFile(file));
    }catch{res.statusCode=404;res.end('Not found');}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const url='http://127.0.0.1:'+server.address().port+'/';
  child=spawn(options.browser,['--headless=new','--remote-debugging-port=0',
    '--remote-debugging-address=127.0.0.1','--user-data-dir='+profile,
    '--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader',
    '--disable-background-networking','--no-first-run','--no-default-browser-check',
    '--disable-extensions','--disable-component-update','--disable-sync',
    '--disable-dev-shm-usage','about:blank'],{windowsHide:true,stdio:['ignore','ignore','pipe']});
  let spawnError;
  child.on('error',error=>{spawnError=error;});
  child.stderr.on('data',chunk=>{stderr=(stderr+chunk.toString()).slice(-8000);});
  let port;
  for(let i=0;i<300;i++){
    checkDeadline();
    if(spawnError)throw spawnError;
    if(child.exitCode!==null)throw Error('Owned browser exited: '+stderr);
    try{port=Number((await readFile(path.join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0]);break;}catch{}
    await delay(100);
  }
  if(!port)throw Error('Owned browser did not publish its private debug port: '+stderr);
  const target=await (await fetch('http://127.0.0.1:'+port+'/json/new?'+encodeURIComponent(url),{method:'PUT'})).json();
  ws=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
  ws.addEventListener('message',event=>{
    const data=JSON.parse(event.data),request=pending.get(data.id);
    if(!request)return;clearTimeout(request.timer);pending.delete(data.id);
    if(data.error)request.reject(Error(JSON.stringify(data.error)));else request.resolve(data.result);
  });
  await command('Runtime.enable');
  let info;
  for(let i=0;i<1200;i++){
    checkDeadline();
    info=await evaluate('window.taskError ? {error:window.taskError} : window.taskRenderer?.info');
    if(info?.error)throw Error('Task canvas initialization failed: '+info.error);
    if(info)break;
    await delay(100);
  }
  if(!info)throw Error('Task canvas initialization timed out: '+stderr);
  if(!/swiftshader|software/i.test(info.renderer))throw Error('CPU software WebGL required; received '+info.renderer);
  await mkdir(path.join(output,'frames'),{recursive:false});
  for(let i=0;i<info.frame_count;i++){
    checkDeadline();
    const png=await evaluate('window.taskRenderer.renderFrame('+i+')');
    await writeFile(path.join(output,'frames',String(i).padStart(6,'0')+'.png'),Buffer.from(png,'base64'),{flag:'wx'});
  }
  await writeFile(path.join(output,'render-facts.json'),JSON.stringify({...info,browser_session:'isolated ephemeral owned headless process',browser_stderr:stderr},null,2),{flag:'wx'});
  console.log(JSON.stringify({frame_count:info.frame_count,width:info.width,height:info.height,renderer:info.renderer}));
}finally{
  for(const request of pending.values()){clearTimeout(request.timer);request.reject(Error('Owned renderer closed'));}
  // Request graceful shutdown of this owned browser, independent of deadline.
  // No other browser process/session is addressed or enumerated.
  if(ws?.readyState===WebSocket.OPEN){
    ws.send(JSON.stringify({id:++serial,method:'Browser.close'}));
    for(let i=0;i<20&&child?.exitCode===null;i++)await delay(100);
  }
  ws?.close();
  if(child&&child.exitCode===null){
    child.kill();
    for(let i=0;i<50&&child.exitCode===null;i++)await delay(100);
  }
  if(server){server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
  // Chromium may retain a profile lock briefly after process exit on Windows.
  await rm(profile,{recursive:true,force:true,maxRetries:12,retryDelay:250});
}
