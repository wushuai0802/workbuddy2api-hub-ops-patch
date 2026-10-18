// Run the shipped dashboard functions with synthetic fetch responses only.
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
for (const id of ['btnConsoleToggle', 'btnGatewayShutdown']) {
  assert(new RegExp('<button[^>]*id="' + id + '"[^>]*disabled').test(html), 'controls must start disabled');
}
function fixture() {
  const elements = Object.fromEntries(['btnConsoleToggle', 'btnGatewayShutdown', 'statusDot'].map(k =>
    [k, {disabled:true, title:'', style:{}, querySelector:()=>({innerHTML:''})}]));
  const calls = [], notices = [];
  const c = vm.createContext({window:{}, document:{getElementById:k=>elements[k]},
    confirm:()=>true, toast:(...args)=>notices.push(args),
    getJSON:async()=>({supported:true, shutdown_supported:true, visible:true}),
    postJSON:async(url)=>{calls.push(url); return {ok:true, shutting_down:true, visible:true};}});
  vm.runInContext(html.slice(start, end), c);
  return {c,elements,calls,notices,down:()=>vm.runInContext('GATEWAY_DOWN',c)};
}
(async()=>{
  let f = fixture();
  await f.c.shutdownGateway(); await f.c.toggleConsoleWindow();
  assert.equal(f.calls.length,0);
  f.c.getJSON = async()=>({supported:false, shutdown_supported:false, visible:false});
  await f.c.initConsoleButtons();
  assert(f.elements.btnConsoleToggle.disabled && f.elements.btnGatewayShutdown.disabled);
  await f.c.shutdownGateway(); assert.equal(f.calls.length,0);

  f = fixture(); f.c.getJSON = async()=>({supported:false, shutdown_supported:true, visible:false});
  await f.c.initConsoleButtons();
  assert(f.elements.btnConsoleToggle.disabled && !f.elements.btnGatewayShutdown.disabled);

  for (const response of [null, {}, {ok:false}, {ok:true}, {ok:true,shutting_down:false}]) {
    f = fixture(); await f.c.initConsoleButtons(); f.c.postJSON = async()=>response;
    await f.c.shutdownGateway(); assert.equal(f.down(),false);
    assert.equal(f.elements.statusDot.style.background,undefined);
    assert.equal(f.notices.at(-1)[1],'bad');
  }
  for (const message of ['unauthorized', 'HTTP 403', 'Failed to fetch']) {
    f = fixture(); await f.c.initConsoleButtons(); f.c.postJSON = async()=>{throw Error(message);};
    await f.c.shutdownGateway(); assert.equal(f.down(),false);
    assert.equal(f.notices.at(-1)[1],'bad');
  }
  f = fixture(); await f.c.initConsoleButtons();
  f.c.getJSON = async()=>{throw Error('unreachable');}; await f.c.initConsoleButtons();
  assert(f.elements.btnConsoleToggle.disabled && f.elements.btnGatewayShutdown.disabled);

  f = fixture(); await f.c.initConsoleButtons(); f.c.confirm = ()=>false;
  await f.c.shutdownGateway(); assert.equal(f.calls.length,0); assert.equal(f.down(),false);

  f = fixture(); await f.c.initConsoleButtons(); await f.c.shutdownGateway();
  assert.equal(f.down(),true); assert.equal(f.calls.length,1);
  assert(f.elements.btnConsoleToggle.disabled && f.elements.btnGatewayShutdown.disabled);
  assert.equal(f.notices.at(-1)[1],'warn');
  await f.c.shutdownGateway(); assert.equal(f.calls.length,1);

  f = fixture(); await f.c.initConsoleButtons(); f.c.postJSON = async()=>({ok:false,visible:false});
  await f.c.toggleConsoleWindow(); assert.equal(f.notices.at(-1)[1],'bad');
  console.log('dashboard control capability and shutdown acknowledgement contracts passed; no network');
})().catch(error=>{console.error(error);process.exitCode=1;});
