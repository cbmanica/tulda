"""Generated index page. Data (one page of groups from the catalog, facets, dates) is inlined
as JSON and rendered client-side with textContent; more pages come from /api/groups."""

import json

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tulda__TITLE__</title>
<style>
:root{--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--card:#f2f2f5;--acc:#2e8b57;--line:#d8d8de}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--fg:#eee;--mut:#9a9aa2;--card:#1d1d21;--acc:#4fc58a;--line:#33333a}}
[hidden]{display:none!important}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
header{padding:12px 16px;border-bottom:1px solid var(--line)}
.top{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px}
h1{font-size:18px;margin:0}h1 a{color:inherit;text-decoration:none}
form.s{display:flex;gap:6px;flex:1 1 220px;max-width:420px}
form.s input{flex:1;min-width:0;padding:6px 10px;border:1px solid var(--line);border-radius:16px;
background:var(--card);color:var(--fg);font:inherit;font-size:14px}
nav,.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
nav a,.chip,.btn{padding:6px 12px;border:1px solid var(--line);border-radius:16px;color:var(--fg);
text-decoration:none;background:var(--card);font:inherit;font-size:14px;cursor:pointer;user-select:none}
nav a.on,.chip.on,.btn.on{background:var(--acc);border-color:var(--acc);color:#fff}
a:focus-visible,.btn:focus-visible,summary:focus-visible,input:focus-visible,.card:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.lbl{color:var(--mut);font-size:12px;align-self:center;margin-right:2px}
.dates{margin:6px 0;font-size:14px}.dates>summary{cursor:pointer;color:var(--mut)}
.dates .yr{display:flex;flex-wrap:wrap;gap:4px 6px;margin:6px 0;align-items:center}
.dates a{padding:2px 9px;border:1px solid var(--line);border-radius:12px;color:var(--fg);text-decoration:none;background:var(--card)}
.dates a.on{background:var(--acc);border-color:var(--acc);color:#fff}
main{padding:12px 16px}
.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 12px}.bar .info{color:var(--mut);font-size:13px;margin-right:auto}
#fresh{display:block;margin:0 0 10px;background:var(--acc);color:#fff;border-color:var(--acc)}
.grp{margin:0 0 12px;border:1px solid var(--line);border-radius:10px;padding:0 12px}
.grp.trashed{opacity:.6;border-style:dashed}
.gh{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;padding:10px 0;min-height:44px}
summary.gh{cursor:pointer;list-style:none}summary.gh::-webkit-details-marker{display:none}
.caret::before{content:"\25B6";display:inline-block;font-size:11px;color:var(--mut);transition:transform .15s}
details[open]>summary .caret::before{transform:rotate(90deg)}
.gh h2{font-size:16px;margin:0;overflow-wrap:anywhere;flex:1 1 160px;min-width:0}
.gh .t{color:var(--mut);font-size:12px}.gh a.o{font-size:12px;color:var(--acc)}
.tg{font-size:11px;padding:0 7px;border-radius:9px;border:1px solid var(--line);color:var(--mut)}
.tg{text-decoration:none}a.tg:hover{border-color:var(--acc);color:var(--fg)}
.tg.src{font-size:12px;padding:1px 9px;color:var(--acc);border-color:var(--acc);font-weight:600}
.card .m a.tg{display:inline-block;margin-top:3px}
.star{color:#d9a400}
.ths{display:flex;gap:3px}.ths img{width:36px;height:27px;object-fit:contain;border-radius:3px;background:#8884}
details[open]>summary .ths{display:none}
.bd{padding:0 0 10px}
.src{display:flex;flex-wrap:wrap;gap:4px;margin:0 0 8px}
.src span{font-size:12px;padding:1px 8px;border-radius:10px;background:var(--card);border:1px solid var(--line);overflow-wrap:anywhere}
.cmd{font:11px/1.3 ui-monospace,monospace;color:var(--mut);overflow-wrap:anywhere;margin:0 0 8px}
.note{font-size:13px;color:var(--mut);margin:0 0 8px;white-space:pre-wrap}
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
@media (max-width:600px){.pair{flex-direction:column;overflow:auto}.pair img{max-height:36vh}.grp{padding:0 8px}}
</style></head><body>
<header><div class="top"><h1><a href="/">tulda</a></h1>
<form class="s" action="/search" method="get" role="search"><input type="search" name="q" id="q" placeholder="Search requests, sources, tags" aria-label="Search" maxlength="100"><button class="btn" type="submit">Search</button></form></div>
<nav id="nav" aria-label="Projects"></nav><details class="dates" id="dates"><summary>Browse by date (UTC)</summary><div id="dbody"></div></details>
<div class="chips" id="chips"></div></header>
<main><a class="btn" id="fresh" href="" hidden>New renders: reload</a>
<div class="bar" id="bar"><span class="info" id="info"></span>
<button class="btn" id="expall" type="button">Expand all</button><button class="btn" id="colall" type="button">Collapse all</button></div>
<div id="groups"></div><button class="btn more" id="more" type="button" hidden>Load more requests</button>
<div class="empty" id="empty" hidden>No renders here.</div></main>
<div id="lb"><button class="x" aria-label="Close">&times;</button><button class="p" aria-label="Previous">&lsaquo;</button>
<img id="lbimg" alt=""><div class="cap" id="cap"></div><button class="n" aria-label="Next">&rsaquo;</button></div>
<div id="cmp"><button class="x" aria-label="Close">&times;</button><div class="pair" id="pair"></div></div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const ctx=D.ctx,F=D.filters,single=ctx.kind==='group';
const $=id=>document.getElementById(id);
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
let compare=false,picks=[],view=[],next=D.next,hasMore=!!D.has_more&&!!D.next,loaded=D.groups.slice();
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function fmt(s){if(!s)return '';const d=new Date(s);return isNaN(d)?s:d.toLocaleString([], {dateStyle:'medium',timeStyle:'short'})}
// ---- fold state, per browser
const FOLD='tulda.fold.v1';let fold={};
try{fold=JSON.parse(localStorage.getItem(FOLD))||{}}catch(e){fold={}}
function saveFold(){try{const k=Object.keys(fold);if(k.length>1000)k.slice(0,k.length-1000).forEach(x=>delete fold[x]);
 localStorage.setItem(FOLD,JSON.stringify(fold))}catch(e){}}
// ---- url helpers (chips are plain links so they work without scripting state)
function href(mut){const u=new URLSearchParams(location.search);mut(u);const s=u.toString();return location.pathname+(s?'?'+s:'')}
function toggleVal(k,v){return href(u=>{const a=u.getAll(k);u.delete(k);(a.includes(v)?a.filter(x=>x!==v):[...a,v]).forEach(x=>u.append(k,x))})}
function toggleFlag(k){return href(u=>{u.has(k)?u.delete(k):u.set(k,'1')})}
function link(cls,text,h){const a=el('a',cls,text);a.href=h;return a}
function nav(){const n=$('nav');n.textContent='';
 n.append(link(ctx.kind==='recent'?'on':'','Recent requests','/'));
 D.facets.projects.forEach(([p,c])=>n.append(link(p===ctx.project?'on':'','Project '+p+' ('+c+')','/'+encodeURIComponent(p)+'/')));
 if(ctx.kind==='day')n.append(link('on','Date '+ctx.day,'#'));
 if(ctx.kind==='search')n.append(link('on','Search: '+ctx.q,'#'));
 if(single)n.append(link('on','Request '+ctx.group,'#'));
 const c=el('button','btn'+(compare?' on':''),'Compare');c.type='button';c.setAttribute('aria-pressed',compare);
 c.onclick=()=>{compare=!compare;picks=[];markPicks();nav()};n.append(c)}
function chips(){const c=$('chips');c.textContent='';
 const fx=D.facets;
 [['adapter',fx.adapters],['stage',fx.stages],['view',fx.views],['tag',fx.tags]].forEach(([k,vals])=>{
  const act=F[k]||[];if(vals.length<2&&!act.length)return;c.append(el('span','lbl',k));
  vals.forEach(([v,n])=>c.append(link('chip'+(act.includes(v)?' on':''),k==='tag'?v+' ('+n+')':v,toggleVal(k,v))));
  act.filter(v=>!vals.some(x=>x[0]===v)).forEach(v=>c.append(link('chip on',v,toggleVal(k,v))))});
 c.append(link('chip'+(F.starred?' on':''),'★ starred',toggleFlag('starred')));
 if(fx.trashed||F.trash)c.append(link('chip'+(F.trash?' on':''),'show trash ('+fx.trashed+')',toggleFlag('trash')))}
function dates(){const b=$('dbody');b.textContent='';const ys=new Map();
 D.dates.forEach(([d,n])=>{const y=d.slice(0,4),m=d.slice(0,7);if(!ys.has(y))ys.set(y,new Map());
  const ms=ys.get(y);ms.set(m,(ms.get(m)||0)+n)});
 const cur=ctx.day?ctx.day.slice(0,7):null;
 ys.forEach((ms,y)=>{const r=el('div','yr');r.append(el('b','',y));
  ms.forEach((n,m)=>r.append(link(m===cur?'on':'',MON[+m.slice(5)-1]+' ('+n+')','/_d/'+m+'/')));b.append(r)});
 if(cur){const r=el('div','yr');r.append(el('b','',cur));
  D.dates.filter(([d])=>d.startsWith(cur)).forEach(([d,n])=>r.append(link(d===ctx.day?'on':'',d.slice(8)+' ('+n+')','/_d/'+d+'/')));b.append(r)}
 if(!D.dates.length)b.append(el('span','lbl','no dates yet'));
 if(ctx.day)$('dates').open=true}
// ---- groups
function title(g){return g.title||(g.legacy?'':g.sources.join(', '))||g.id}
function tagLink(t,cls){const a=link('tg '+cls,t,'/?tag='+encodeURIComponent(t));a.title='show requests tagged '+t;
 a.onclick=e=>e.stopPropagation();return a}
function card(i){const c=el('div','card');c._i=i;c.tabIndex=0;c.setAttribute('role','button');
 const im=el('img');im.src=i.url;im.loading='lazy';im.decoding='async';im.alt=i.file;c.append(im);
 if(i.view)c.append(el('span','badge',i.view));
 const m=el('div','m');m.append(el('b','',i.source||i.file),el('div','',i.adapter+' / '+i.stage+(ctx.project?'':' / '+i.project)));
 if(i.tag)m.append(tagLink(i.tag,'src'));
 c.append(m);const go=()=>compare?pick(i):openCard(c);c.onclick=go;
 c.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();go()}};return c}
function buildBody(root,g){if(root._built)return;root._built=true;const b=root.querySelector('.bd');
 const gr=el('div','grid');g.items.forEach(i=>gr.append(card(i)));b.append(gr);markPicks()}
function groupEl(g,first){
 const root=el(single?'section':'details','grp'+(g.trashed?' trashed':''));
 const h=el(single?'div':'summary','gh');
 if(!single){const cr=el('span','caret');cr.setAttribute('aria-hidden','true');h.append(cr);h.setAttribute('aria-expanded','false')}
 h.append(el('h2','',title(g)));
 if(g.starred){const s=el('span','star','★');s.title='starred';h.append(s)}
 if(g.trashed)h.append(el('span','tg','trashed'));
 const au=g.auto_tags||[];au.forEach(t=>h.append(tagLink(t,'src')));g.tags.filter(t=>!au.includes(t)).forEach(t=>h.append(tagLink(t,'')));
 const srcs=g.sources.length||new Set(g.items.map(i=>i.source).filter(Boolean)).size;
 const when=g.created?fmt(g.created):fmt(new Date(g.mtime*1000).toISOString());
 h.append(el('span','t',when+' | '+srcs+' source'+(srcs==1?'':'s')+' | '+g.items.length+' image'+(g.items.length==1?'':'s')));
 if(!single){h.append(link('o','open','/_g/'+encodeURIComponent(g.id)+'/'));
  const th=el('span','ths');th.setAttribute('aria-hidden','true');
  g.items.slice(0,3).forEach(i=>{const im=el('img');im.src=i.url;im.loading='lazy';im.decoding='async';im.alt='';th.append(im)});h.append(th)}
 const b=el('div','bd');root.append(h,b);
 if(g.sources.length){const r=el('div','src');g.sources.forEach(x=>r.append(el('span','',x)));b.append(r)}
 if(g.cmd&&single)b.append(el('div','cmd',g.cmd));
 if(g.notes)b.append(el('div','note',g.notes));
 root._g=g;
 if(single){buildBody(root,g);return root}
 const open=g.id in fold?!!fold[g.id]:first;
 root.open=open;root._state=open;h.setAttribute('aria-expanded',open);if(open)buildBody(root,g);
 root.addEventListener('toggle',()=>{h.setAttribute('aria-expanded',root.open);if(root.open)buildBody(root,g);
  if(root.open===root._state)return;root._state=root.open;fold[g.id]=root.open;saveFold()});
 return root}
function addGroups(gs,first){gs.forEach((g,ix)=>$('groups').append(groupEl(g,first&&ix===0)));
 $('more').hidden=!(hasMore&&next);$('empty').hidden=loaded.length>0;
 $('info').textContent=loaded.length+' request'+(loaded.length==1?'':'s')+(hasMore&&next?' (more available)':'');
 $('bar').hidden=single}
function setAll(v){document.querySelectorAll('#groups details.grp').forEach(d=>{d.open=v})}
$('expall').onclick=()=>setAll(true);$('colall').onclick=()=>setAll(false);
$('more').onclick=async()=>{const b=$('more');b.disabled=true;
 try{const r=await fetch('/api/groups?'+D.qs+'&limit='+D.page_size+'&cursor='+encodeURIComponent(next));if(!r.ok)throw new Error(r.status);
  const j=await r.json();loaded.push(...j.groups);next=j.next;
  hasMore=!!j.has_more&&!!j.next&&j.groups.length>=D.page_size;addGroups(j.groups,false)}
 catch(e){$('info').textContent='Could not load more ('+e.message+')'}b.disabled=false};
// ---- lightbox / compare over the visible cards
const cards=()=>[...document.querySelectorAll('#groups .card')].filter(c=>c.offsetParent!==null);
function markPicks(){document.querySelectorAll('#groups .card').forEach(c=>c.classList.toggle('pick',picks.includes(c._i)))}
function pick(i){const k=picks.indexOf(i);if(k>=0)picks.splice(k,1);else{picks.push(i);if(picks.length>2)picks.shift()}
 markPicks();if(picks.length===2)openCmp()}
function caption(i){const bits=[i.file,i.adapter+' / '+i.stage+(i.view?' / '+i.view:'')];
 if(i.source)bits.push('source '+i.source+(i.git_sha?' @'+i.git_sha+(i.git_dirty?'*':''):''));
 if(i.tool_version)bits.push(i.tool_version);if(i.created_utc)bits.push(i.created_utc);return bits.join('  |  ')}
function openCmp(){const p=$('pair');p.textContent='';picks.forEach(i=>{const f=el('figure');const im=el('img');im.src=i.url;im.alt=i.file;
 f.append(im,el('div','cap',caption(i)));p.append(f)});$('cmp').classList.add('open')}
$('cmp').querySelector('.x').onclick=()=>{$('cmp').classList.remove('open');picks=[];markPicks()};
let cur=-1;
function openCard(c){view=cards().map(x=>x._i);open(view.indexOf(c._i))}
function open(ix){if(!view.length)return;cur=(ix+view.length)%view.length;const i=view[cur];$('lbimg').src=i.url;
 $('cap').textContent=caption(i);$('lb').classList.add('open')}
function close(){$('lb').classList.remove('open');cur=-1}
$('lb').querySelector('.x').onclick=close;$('lb').querySelector('.p').onclick=()=>open(cur-1);
$('lb').querySelector('.n').onclick=()=>open(cur+1);
$('lb').onclick=e=>{if(e.target.id==='lb')close()};
document.addEventListener('keydown',e=>{if(e.key==='Escape'){$('cmp').classList.remove('open');if(cur>=0)close();return}
 if(cur<0)return;if(e.key==='ArrowLeft')open(cur-1);else if(e.key==='ArrowRight')open(cur+1)});
// ---- new-render notice (cheap: /api/latest reads one row from the catalog)
function poll(){if(document.hidden)return;fetch('/api/latest').then(r=>r.json()).then(j=>{
 if(j.rev!==D.rev){const f=$('fresh');f.href=location.href;f.hidden=false}}).catch(()=>{})}
setInterval(poll,15000);
$('q').value=ctx.q||'';
nav();chips();dates();addGroups(loaded,!ctx.q);
</script></body></html>
"""


def render_index(data: dict) -> str:
    """data: ctx, groups, next, facets, dates, filters, qs, rev (see server.page_data)."""
    ctx = data["ctx"]
    blob = json.dumps(data)
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    title = {"project": f" - {ctx.get('project')}", "group": f" - request {ctx.get('group')}",
             "day": f" - {ctx.get('day')}", "search": " - search"}.get(ctx["kind"], "")
    from html import escape
    head, tail = PAGE.split("__DATA__", 1)
    return head.replace("__TITLE__", escape(title)) + blob + tail
