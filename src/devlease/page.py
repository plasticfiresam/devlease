"""The single page the service serves."""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>devlease</title>
<style>
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--card:#fff;--ok:#16a34a;--warn:#d97706;--bad:#dc2626;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#fafaf9;--muted:#a8a29e;--line:#44403c;--card:#292524;--accent:#60a5fa}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0 0 12px}h2{font-size:15px;margin:24px 0 8px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
.big{font-size:22px;font-weight:600}.muted{color:var(--muted)}
.bar{height:6px;background:var(--line);border-radius:3px;margin-top:6px;overflow:hidden}.bar>i{display:block;height:100%;background:var(--accent)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);font-weight:500}
tr:last-child td{border-bottom:0}.scroll{overflow-x:auto}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;border:1px solid var(--line)}
.free{color:var(--ok)}.held{color:var(--warn)}.bad{color:var(--bad)}
button{font:inherit;padding:3px 10px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;cursor:pointer}
button:hover{border-color:var(--accent)}button:disabled{opacity:.5;cursor:default}
code{font:12px ui-monospace,monospace}#err{color:var(--bad);margin:8px 0}
</style></head><body><main>
<h1>devlease <span class="muted" id="stamp"></span></h1><div id="err"></div>
<div class="cards" id="cards"></div>
<h2>Devices</h2><div class="scroll"><table id="devices"></table></div>
<h2>Waiting for a device</h2><div class="scroll"><table id="waiters"></table></div>
<h2>Requests to create a device</h2><div class="scroll"><table id="requests"></table></div>
<h2>Caches <button id="recount">Recount sizes</button> <button id="sync">Rescan devices</button></h2>
<div class="scroll"><table id="caches"></table></div>
<h2>Recent leases</h2><div class="scroll"><table id="history"></table></div>
</main>
<script>
const $=id=>document.getElementById(id);
const gb=b=>b==null?'n/a':(b/1e9).toFixed(1)+' GB';
const mb=b=>(b/1048576).toFixed(0)+' MB';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const when=t=>t?new Date(t*1000).toLocaleTimeString():'';
const left=(t,now)=>{const s=Math.max(0,Math.round(t-now));return Math.floor(s/60)+'m '+s%60+'s'};
async function api(path,body){
  const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'X-Devlease':'1','Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();if(!r.ok&&!j.status)throw new Error(j.error||r.status);return j;
}
function table(el,head,rows){
  el.innerHTML='<tr>'+head.map(h=>'<th>'+h+'</th>').join('')+'</tr>'+
    (rows.length?rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join(''):'<tr><td colspan="'+head.length+'" class="muted">none</td></tr>');
}
async function refresh(){
  try{
    const s=await api('/api/state');$('err').textContent='';$('stamp').textContent='updated '+when(s.now);
    const m=s.memory,pct=(a,b)=>b?Math.round(a/b*100):0;
    const devMem=Object.values(s.device_memory).reduce((a,b)=>a+b,0);
    $('cards').innerHTML=
      '<div class="card"><div class="muted">Memory in use</div><div class="big">'+gb(m.used)+' / '+gb(m.total)+'</div><div class="bar"><i style="width:'+pct(m.used,m.total)+'%"></i></div></div>'+
      '<div class="card"><div class="muted">Disk free</div><div class="big">'+gb(m.disk_free)+'</div><div class="bar"><i style="width:'+(100-pct(m.disk_free,m.disk_total))+'%"></i></div></div>'+
      '<div class="card"><div class="muted">Simulators and emulators</div><div class="big">'+mb(devMem)+'</div><div class="muted">resident memory of running devices</div></div>'+
      '<div class="card"><div class="muted">Limits</div>iOS at once: <b>'+s.limits.limit_ios+'</b>, Android at once: <b>'+s.limits.limit_android+'</b><br>Longest lease: <b>'+Math.round(s.limits.max_ttl/60)+' min</b></div>'+
      '<div class="card"><div class="muted">Largest processes</div>'+s.top.slice(0,5).map(p=>esc(p.name)+' <span class="muted">'+mb(p.rss)+'</span>').join('<br>')+'</div>';
    table($('devices'),['Kind','Device','OS','Serial / id','Status','Memory','Holder','Left',''],s.devices.map(d=>{
      const l=d.lease,mem=s.device_memory[d.native_id]||s.device_memory[d.name];
      return [d.kind,esc(d.name),esc(d.os),'<code>'+esc(d.serial)+'</code>',
        l?'<span class="pill held">leased</span>':'<span class="pill free">free</span>',
        mem?mb(mem):'',l?esc(l.holder)+'<br><span class="muted">'+esc(l.note)+'</span>':'',
        l?left(l.expires_at,s.now):'',l?'<button data-release="'+l.id+'">Release</button>':''];
    }));
    table($('waiters'),['Kind','Wants','Holder','Waiting since','Task'],s.waiters.map(w=>[w.kind,esc([w.profile,w.os,w.device].filter(Boolean).join(' ')||'any'),esc(w.holder),when(w.since),esc(w.note)]));
    table($('requests'),['#','Kind','Name','Profile / OS','Reason','Status',''],s.requests.map(r=>[r.id,r.kind,esc(r.name),esc(r.profile+' / '+r.os),esc(r.reason),
      '<span class="pill '+(r.status=='failed'||r.status=='rejected'?'bad':'')+'">'+r.status+'</span>'+(r.result&&r.result!='ok'?'<br><span class="muted">'+esc(r.result)+'</span>':''),
      r.status=='pending'?'<button data-approve="'+r.id+'">Approve</button> <button data-reject="'+r.id+'">Reject</button>':'']));
    table($('history'),['Device','Holder','Task','From','To','Ended by'],s.history.map(h=>[esc(h.name),esc(h.holder),esc(h.note),when(h.acquired_at),when(h.released_at),esc(h.end_reason)]));
  }catch(e){$('err').textContent='Cannot reach the service: '+e.message}
}
async function caches(fresh){
  const list=await api('/api/caches'+(fresh?'?refresh=1':''));
  table($('caches'),['Cache','Size','What it is',''],list.map(c=>[esc(c.label),gb(c.size),esc(c.note),'<button data-clean="'+c.key+'">Clean</button>']));
}
document.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b)return;
  try{
    if(b.dataset.release)await api('/api/leases/'+b.dataset.release+'/release',{});
    else if(b.dataset.approve)await api('/api/requests/'+b.dataset.approve+'/approve',{});
    else if(b.dataset.reject)await api('/api/requests/'+b.dataset.reject+'/reject',{});
    else if(b.dataset.clean){
      b.disabled=true;let r=await api('/api/caches/'+b.dataset.clean+'/clean',{});
      if(r.status=='busy'&&confirm('Running now: '+r.processes.join(', ')+'. Clean anyway?'))r=await api('/api/caches/'+b.dataset.clean+'/clean',{force:true});
      if(r.status=='ok')$('err').textContent='';else if(r.status!='busy')$('err').textContent='Clean failed: '+(r.error||r.status);
      await caches(true);
    }
    else if(b.id=='recount')await caches(true);
    else if(b.id=='sync')await api('/api/sync',{});
    refresh();
  }catch(err){$('err').textContent=err.message;b.disabled=false}
});
refresh();caches(false);setInterval(refresh,3000);
</script></body></html>
"""
