// Installed dashboard contracts, synthetic DOM/fetch only.
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const root = process.env.WB_OPS_DASHBOARD_ROOT;
assert(root, 'use scripts/test_dashboard.py with an isolated source');
const html = fs.readFileSync(path.join(root, 'dashboard.html'), 'utf8');
const start = html.indexOf('let GATEWAY_DOWN = false;');
const end = html.indexOf('async function getJSON(', start);
assert(start >= 0 && end > start);
assert(/<button[^>]*id="btnGatewayShutdown"[^>]*disabled/.test(html));
assert(/loadAgentsAvailability\(\), initConsoleButtons\(\)/.test(html));

function fixture() {
  const elements = Object.fromEntries(['btnConsoleToggle', 'btnGatewayShutdown', 'statusDot'].map(k =>
    [k, {disabled:k === 'btnGatewayShutdown', title:'', style:{}}]));
  const calls = [], notices = [], probes = [];
  const c = vm.createContext({window:{open:()=>({focus(){}})},
    document:{getElementById:k=>elements[k]}, AbortController,
    setTimeout, clearTimeout, confirm:()=>true, toast:(...args)=>notices.push(args),
    getJSON:async()=>({supported:true, mode:'web', shutdown_supported:true, visible:false}),
    postJSON:async(url)=>{calls.push(url); return {ok:true, shutting_down:true};},
    fetch:async(url)=>{probes.push(url); return {ok:true};}});
  vm.runInContext(html.slice(start, end), c);
  return {c,elements,calls,notices,probes,down:()=>vm.runInContext('GATEWAY_DOWN',c)};
}
const poll = {pollMs:0, tries:2};
(async()=>{
  let f = fixture();
  await f.c.shutdownGateway(poll); assert.equal(f.calls.length,0);
  f.c.getJSON = async()=>({supported:true, mode:'web', shutdown_supported:false});
  await f.c.initConsoleButtons();
  assert(!f.elements.btnConsoleToggle.disabled && f.elements.btnGatewayShutdown.disabled);
  f.c.toggleConsoleWindow(); assert.equal(f.notices.at(-1)[1],'ok');
  await f.c.shutdownGateway(poll); assert.equal(f.calls.length,0);
  f.c.window.open = ()=>null; f.c.toggleConsoleWindow(); assert.equal(f.notices.at(-1)[1],'bad');

  for (const response of [null, {}, {ok:false}, {ok:true}, {ok:true,shutting_down:false}]) {
    f = fixture(); await f.c.initConsoleButtons(); f.c.postJSON = async()=>response;
    f.c.fetch = async()=>{throw Error('network disconnected');};
    await f.c.shutdownGateway(poll); assert.equal(f.down(),false);
    assert.equal(f.notices.at(-1)[1],'bad'); assert.equal(f.probes.length,0);
  }
  for (const message of ['unauthorized', 'HTTP 403', 'Failed to fetch']) {
    f = fixture(); await f.c.initConsoleButtons(); f.c.postJSON = async()=>{throw Error(message);};
    await f.c.shutdownGateway(poll); assert.equal(f.down(),false);
    assert.equal(f.notices.at(-1)[1],'bad'); assert.equal(f.probes.length,0);
  }
  f = fixture(); await f.c.initConsoleButtons();
  f.c.getJSON = async()=>{throw Error('unreachable');}; await f.c.initConsoleButtons();
  assert(f.elements.btnGatewayShutdown.disabled);

  f = fixture(); await f.c.initConsoleButtons(); f.c.confirm = ()=>false;
  await f.c.shutdownGateway(poll); assert.equal(f.calls.length,0);

  f = fixture(); await f.c.initConsoleButtons(); await f.c.shutdownGateway(poll);
  assert.equal(f.down(),false); assert.equal(f.probes.length,2);
  assert(f.notices.at(-1)[0].includes('仍在运行')); assert(!f.elements.btnGatewayShutdown.disabled);

  f = fixture(); await f.c.initConsoleButtons(); f.c.fetch=async()=>({ok:false});
  await f.c.shutdownGateway(poll); assert.equal(f.down(),false);
  assert.equal(f.notices.at(-1)[1],'bad');

  f = fixture(); await f.c.initConsoleButtons();
  f.c.setTimeout = (fn,ms)=>setTimeout(fn,ms===2000?0:ms);
  f.c.fetch = async(_url,options)=>new Promise((_resolve,reject)=>{
    options.signal.addEventListener('abort',()=>reject(Error('aborted')));
  });
  await f.c.shutdownGateway(poll); assert.equal(f.down(),false);
  assert(f.notices.at(-1)[0].includes('超时'));

  f = fixture(); await f.c.initConsoleButtons(); let sequence=0;
  f.c.fetch=async()=>{if(++sequence===1) throw Error('temporary'); return {ok:true};};
  await f.c.shutdownGateway(poll); assert.equal(f.down(),false);

  f = fixture(); await f.c.initConsoleButtons();
  f.c.fetch=async()=>{throw Error('connection refused');};
  await f.c.shutdownGateway(poll); assert.equal(f.down(),true);
  assert.equal(f.calls.length,1); assert(f.elements.btnGatewayShutdown.disabled);
  await f.c.shutdownGateway(poll); assert.equal(f.calls.length,1);
  console.log('web logs, managed shutdown, strict acknowledgement and bounded health probes passed; no network');
})().catch(error=>{console.error(error);process.exitCode=1;});
