// Exercise shipped analytics functions with deferred replies and a virtual clock.
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function fixture(html) {
  let clock = Date.parse('2026-10-18T12:00:01+08:00');
  class ClockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock])); }
    static now() { return clock; }
  }
  function source(name) {
    const match = new RegExp('(?:async )?function ' + name + '\\(').exec(html);
    assert(match, 'missing shipped function: '+name);
    const end = html.indexOf('\n}', match.index);
    assert(end > match.index);
    return html.slice(match.index, end + 2);
  }
  const state = {range:'d3',analytics:null,series:null};
  const urls = [], pending = [], rendered = [], errors = [];
  const kpis = {style:{opacity:1}};
  let automatic = false;
  const response = id => ({id, series:[{requests:1,total_tokens:10,at:1700000000}]});
  const c = vm.createContext({Date:ClockDate, state, $:()=>kpis,
    console:{error:(...args)=>errors.push(args)},
    api:url=>{
      urls.push(url);
      if(automatic) return Promise.resolve(response('auto'));
      return new Promise((resolve,reject)=>pending.push({resolve,reject}));
    }, renderKpis:()=>rendered.push(state.analytics && state.analytics.id),
    renderSeriesMeta(){}, drawSeries(){}, drawSparks(){}, renderDim(){}, loadRecent(){}});
  const names=['prevRange','loadPrevSummary','rangeParams','loadRange'];
  let code='let prevSummary=null; let WBA_GEN=0; const POLL_UNTIL_BUCKET=15;\n';
  if(html.includes('function pollUntil(')) code += source('pollUntil')+'\n';
  code += names.map(source).join('\n');
  vm.runInContext(code,c);
  return {c,state,urls,pending,rendered,errors,kpis,
    setClock:value=>{clock=value;}, automatic:()=>{automatic=true;},
    settle:(start,id,error)=>pending.slice(start,start+3).forEach((row,i)=>{
      if(error && i===0) row.reject(Error('synthetic old response'));
      else row.resolve(response(id));
    })};
}

async function check(html) {
  let f=fixture(html), older=f.c.loadRange();
  f.state.range='d30'; const newer=f.c.loadRange();
  assert.equal(f.pending.length,6);
  f.settle(3,'new'); await newer;
  f.settle(0,'old'); await older;
  assert.equal(f.state.analytics.id,'new','late old range overwrote the current range');
  assert.deepStrictEqual(f.rendered,['new']);

  f=fixture(html); older=f.c.loadRange();
  const next=f.c.loadRange();
  f.settle(0,'old'); await older;
  assert.equal(f.kpis.style.opacity,.55,'old reply cleared the newer loading indicator');
  assert.equal(f.rendered.length,0);
  f.settle(3,'new'); await next;
  assert.equal(f.kpis.style.opacity,1);

  f=fixture(html); older=f.c.loadRange();
  const success=f.c.loadRange(); f.settle(3,'new'); await success;
  f.settle(0,'old',true); await older;
  assert.deepStrictEqual(f.rendered,['new']); assert.equal(f.errors.length,0);

  const base=Math.floor(1700000000/15)*15;
  for(const range of ['d3','d30','all']) {
    f=fixture(html); f.automatic(); f.state.range=range;
    f.setClock((base+1)*1000); await f.c.loadRange(true);
    const first=f.urls.splice(0);
    f.setClock((base+6)*1000); await f.c.loadRange(true);
    const second=f.urls.splice(0);
    assert.deepStrictEqual(second,first,range+' polling URLs changed inside one cache bucket');
    f.setClock((base+16)*1000); await f.c.loadRange(true);
    assert.notDeepStrictEqual(f.urls,first,range+' polling did not advance at the next bucket');
  }
  f=fixture(html); f.automatic();
  f.setClock((base+1)*1000); await f.c.loadRange(false);
  const first=f.urls.splice(0);
  f.setClock((base+6)*1000); await f.c.loadRange(false);
  assert.notDeepStrictEqual(f.urls,first,'manual range selection must retain exact time');

  // The supplied window boundary must determine its calendar even if the
  // request crosses local midnight, Monday, or the first day of the month.
  const boundaries=[['today','2026-10-18T00:00:00'], ['week','2026-11-02T00:00:00'],
                    ['month','2026-11-01T00:00:00']];
  for(const [range,date] of boundaries) {
    f=fixture(html); f.state.range=range;
    const boundary=Date.parse(date), until=boundary/1000-15;
    f.setClock(until*1000); const expected=JSON.stringify(f.c.prevRange(until));
    f.setClock(boundary+2000);
    assert.equal(JSON.stringify(f.c.prevRange(until)),expected,
                 range+' comparison window used the current calendar instead of its supplied boundary');
  }
}

module.exports={fixture,check};
if(require.main===module) {
  const root=process.env.WB_OPS_DASHBOARD_ROOT;
  assert(root,'use scripts/test_dashboard.py');
  check(fs.readFileSync(path.join(root,'dashboard.html'),'utf8')).then(()=>{
    console.log('late success/error, loading ownership, cache buckets, manual precision and calendar boundaries passed; no network');
  }).catch(error=>{console.error(error);process.exitCode=1;});
}
