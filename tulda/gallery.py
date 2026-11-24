"""Generated index page: data inlined as JSON, rendered client-side with textContent."""

import json

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tulda__TITLE__</title>
<style>
:root{--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--card:#f2f2f5;--acc:#2e8b57;--line:#d8d8de}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--fg:#eee;--mut:#9a9aa2;--card:#1d1d21;--acc:#4fc58a;--line:#33333a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
header{padding:12px 16px;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0 0 8px}h1 a{color:inherit;text-decoration:none}
nav,.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
nav a,.chip,.btn{padding:6px 12px;border:1px solid var(--line);border-radius:16px;color:var(--fg);
text-decoration:none;background:var(--card);font-size:14px;cursor:pointer;user-select:none}
nav a.on,.chip.on,.btn.on{background:var(--acc);border-color:var(--acc);color:#fff}
.lbl{color:var(--mut);font-size:12px;align-self:center;margin-right:2px}
main{padding:12px 16px}
.grp{margin:0 0 22px;border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.gh{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px;margin-bottom:8px}
.gh h2{font-size:16px;margin:0;overflow-wrap:anywhere}.gh h2 a{color:inherit}
.gh .t{color:var(--mut);font-size:12px}
.src{display:flex;flex-wrap:wrap;gap:4px;margin:0 0 8px}
.src span{font-size:12px;padding:1px 8px;border-radius:10px;background:var(--card);border:1px solid var(--line);overflow-wrap:anywhere}
.cmd{font:11px/1.3 ui-monospace,monospace;color:var(--mut);overflow-wrap:anywhere;margin:0 0 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(200px,100%),1fr));gap:10px}
.card{position:relative;background:var(--card);border-radius:8px;overflow:hidden;cursor:pointer;border:2px solid var(--line)}
.card.pick{border-color:var(--acc)}
.card img{display:block;width:100%;aspect-ratio:4/3;object-fit:contain;background:#8884}
.card .m{padding:6px 8px;font-size:12px;color:var(--mut);overflow-wrap:anywhere}.card .m b{color:var(--fg)}
.badge{position:absolute;top:6px;left:6px;background:var(--acc);color:#fff;font-size:12px;font-weight:600;
padding:1px 8px;border-radius:10px}
.empty{color:var(--mut);padding:24px 0}
.more{display:block;margin:0 auto 24px}
#lb,#cmp{position:fixed;inset:0;background:#000d;display:none;flex-direction:column;align-items:center;
justify-content:center;padding:12px;z-index:9;color:#eee}
#lb.open,#cmp.open{display:flex}#lb img{max-width:100%;max-height:75vh;object-fit:contain;background:#fff1}
.cap{margin-top:10px;font-size:13px;max-width:90vw;text-align:center;overflow-wrap:anywhere}
#lb button,#cmp button{position:absolute;background:#0008;color:#fff;border:1px solid #fff4;border-radius:8px;
font-size:22px;padding:8px 14px;cursor:pointer}
#lb .x,#cmp .x{top:10px;right:10px}#lb .p{left:8px;top:50%}#lb .n{right:8px;top:50%}
.pair{display:flex;gap:8px;width:100%;max-height:85vh;justify-content:center;align-items:flex-start;margin-top:40px}
.pair figure{margin:0;flex:1 1 0;min-width:0;text-align:center}
.pair img{width:100%;max-height:70vh;object-fit:contain;background:#fff1}
@media (max-width:600px){.pair{flex-direction:column;overflow:auto}.pair img{max-height:36vh}}
</style></head><body>
<header><h1><a href="/">tulda</a></h1><nav id="nav"></nav><div class="chips" id="chips"></div></header>
<main><div id="groups"></div><button class="btn more" id="more" hidden>Show more requests</button>
<div class="empty" id="empty" hidden>No renders yet.</div></main>
<div id="lb"><button class="x" aria-label="Close">&times;</button><button class="p" aria-label="Previous">&lsaquo;</button>
<img id="lbimg" alt=""><div class="cap" id="cap"></div><button class="n" aria-label="Next">&rsaquo;</button></div>
<div id="cmp"><button class="x" aria-label="Close">&times;</button><div class="pair" id="pair"></div></div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const items=D.items,project=D.project,groupId=D.group;
const sel={adapter:new Set(),stage:new Set(),view:new Set()};
const PAGE=20;let shown=PAGE,compare=false,picks=[];
const $=id=>document.getElementById(id);
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function fmt(s){if(!s)return '';const d=new Date(s);return isNaN(d)?s:d.toLocaleString([], {dateStyle:'medium',timeStyle:'short'})}
function nav(){const n=$('nav');n.textContent='';const counts={};items.forEach(i=>counts[i.project]=(counts[i.project]||0)+1);
 const a=el('a',(project||groupId)?'':'on','Recent requests');a.href='/';n.append(a);
 if(groupId){n.append(Object.assign(el('a','on','Request '+groupId),{href:'#'}))}
 Object.keys(counts).sort().forEach(p=>{const l=el('a',p===project?'on':'','Project '+p+' ('+counts[p]+')');
  l.href='/'+encodeURIComponent(p)+'/';n.append(l)});
 const c=el('span','btn'+(compare?' on':''),'Compare');c.onclick=()=>{compare=!compare;picks=[];nav();groups()};n.append(c)}
const scope=()=>items.filter(i=>(!project||i.project===project)&&(!groupId||i.group_id===groupId));
function chips(){const c=$('chips');c.textContent='';
 ['adapter','stage','view'].forEach(k=>{const vals=[...new Set(scope().map(i=>i[k]).filter(Boolean))].sort();
  if(vals.length<2&&!sel[k].size)return;c.append(el('span','lbl',k));
  vals.forEach(v=>{const x=el('span','chip'+(sel[k].has(v)?' on':''),v);
   x.onclick=()=>{sel[k].has(v)?sel[k].delete(v):sel[k].add(v);chips();groups()};c.append(x)})})}
const pass=i=>['adapter','stage','view'].every(k=>!sel[k].size||sel[k].has(i[k]));
let view=[];
function buildGroups(){const m=new Map();
 scope().filter(pass).forEach(i=>{let g=m.get(i.group_id);
  if(!g){g={id:i.group_id,title:i.group_title,sources:i.group_sources||[],cmd:i.request_cmd,legacy:i.legacy,
   created:i.group_created_utc,items:[],mt:0};m.set(i.group_id,g)}
  g.items.push(i);g.mt=Math.max(g.mt,i.mtime)});
 const gs=[...m.values()];gs.forEach(g=>g.items.sort((a,b)=>(a.seq-b.seq)||(a.mtime-b.mtime)||a.file.localeCompare(b.file)));
 gs.sort((a,b)=>b.mt-a.mt);return gs}
function groups(){const root=$('groups');root.textContent='';
 const gs=buildGroups();view=[];$('empty').hidden=gs.length>0;
 const vis=groupId?gs:gs.slice(0,shown);$('more').hidden=groupId||gs.length<=shown;
 vis.forEach(g=>{const s=el('section','grp');const h=el('div','gh');
  const t=el('h2');const title=g.title||(g.legacy?'':g.sources.join(', '))||g.id;
  if(groupId)t.textContent=title;else{const a=el('a','',title);a.href='/_g/'+encodeURIComponent(g.id)+'/';t.append(a)}
  h.append(t,el('span','t',(g.created?fmt(g.created):fmt(new Date(g.mt*1000).toISOString()))+' | '+g.items.length+' image'+(g.items.length==1?'':'s')));
  s.append(h);
  if(g.sources.length){const r=el('div','src');g.sources.forEach(x=>r.append(el('span','',x)));s.append(r)}
  if(g.cmd&&groupId)s.append(el('div','cmd',g.cmd));
  const gr=el('div','grid');
  g.items.forEach(i=>{const ix=view.length;view.push(i);const c=el('div','card'+(picks.includes(i)?' pick':''));
   const im=el('img');im.src=i.url;im.loading='lazy';im.alt=i.file;c.append(im);
   if(i.view)c.append(el('span','badge',i.view));
   const m=el('div','m');m.append(el('b','',i.source||i.file),el('div','',i.adapter+' / '+i.stage+(project?'':' / '+i.project)));
   c.append(m);c.onclick=()=>compare?pick(i):open(ix);gr.append(c)});
  s.append(gr);root.append(s)})}
$('more').onclick=()=>{shown+=PAGE;groups()};
function pick(i){const k=picks.indexOf(i);if(k>=0)picks.splice(k,1);else{picks.push(i);if(picks.length>2)picks.shift()}
 groups();if(picks.length===2)openCmp()}
function caption(i){const bits=[i.file,i.adapter+' / '+i.stage+(i.view?' / '+i.view:'')];
 if(i.source)bits.push('source '+i.source+(i.git_sha?' @'+i.git_sha+(i.git_dirty?'*':''):''));
 if(i.tool_version)bits.push(i.tool_version);if(i.created_utc)bits.push(i.created_utc);return bits.join('  |  ')}
function openCmp(){const p=$('pair');p.textContent='';picks.forEach(i=>{const f=el('figure');const im=el('img');im.src=i.url;im.alt=i.file;
 f.append(im,el('div','cap',caption(i)));p.append(f)});$('cmp').classList.add('open')}
$('cmp').querySelector('.x').onclick=()=>{$('cmp').classList.remove('open');picks=[];groups()};
let cur=-1;
function open(ix){cur=(ix+view.length)%view.length;const i=view[cur];$('lbimg').src=i.url;
 $('cap').textContent=caption(i);$('lb').classList.add('open')}
function close(){$('lb').classList.remove('open');cur=-1}
$('lb').querySelector('.x').onclick=close;$('lb').querySelector('.p').onclick=()=>open(cur-1);
$('lb').querySelector('.n').onclick=()=>open(cur+1);
$('lb').onclick=e=>{if(e.target.id==='lb')close()};
document.addEventListener('keydown',e=>{if(e.key==='Escape'){$('cmp').classList.remove('open');if(cur>=0)close();return}
 if(cur<0)return;if(e.key==='ArrowLeft')open(cur-1);else if(e.key==='ArrowRight')open(cur+1)});
nav();chips();groups();
</script></body></html>
"""


def render_index(items: list, project: str | None, group: str | None = None) -> str:
    data = json.dumps({"items": items, "project": project, "group": group})
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    title = f" - {project}" if project else (f" - request {group}" if group else "")
    from html import escape
    head, tail = PAGE.split("__DATA__", 1)
    return head.replace("__TITLE__", escape(title)) + data + tail
