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
h1{font-size:18px;margin:0 0 8px}
nav,.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
nav a,.chip{padding:6px 12px;border:1px solid var(--line);border-radius:16px;color:var(--fg);
text-decoration:none;background:var(--card);font-size:14px;cursor:pointer;user-select:none}
nav a.on,.chip.on{background:var(--acc);border-color:var(--acc);color:#fff}
.lbl{color:var(--mut);font-size:12px;align-self:center;margin-right:2px}
main{padding:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.card{background:var(--card);border-radius:8px;overflow:hidden;cursor:pointer;border:1px solid var(--line)}
.card img{display:block;width:100%;aspect-ratio:4/3;object-fit:contain;background:#8884}
.card .m{padding:8px;font-size:12px;color:var(--mut);overflow-wrap:anywhere}.card .m b{color:var(--fg)}
.empty{color:var(--mut);padding:24px 0}
#lb{position:fixed;inset:0;background:#000d;display:none;flex-direction:column;align-items:center;
justify-content:center;padding:12px;z-index:9;color:#eee}
#lb.open{display:flex}#lb img{max-width:100%;max-height:75vh;object-fit:contain;background:#fff1}
#lb .cap{margin-top:10px;font-size:13px;max-width:90vw;text-align:center;overflow-wrap:anywhere}
#lb button{position:absolute;background:#0008;color:#fff;border:1px solid #fff4;border-radius:8px;
font-size:22px;padding:8px 14px;cursor:pointer}
#lb .x{top:10px;right:10px}#lb .p{left:8px;top:50%}#lb .n{right:8px;top:50%}
</style></head><body>
<header><h1>tulda</h1><nav id="nav"></nav><div class="chips" id="chips"></div></header>
<main><div class="grid" id="grid"></div><div class="empty" id="empty" hidden>No renders yet.</div></main>
<div id="lb"><button class="x" aria-label="Close">&times;</button><button class="p" aria-label="Previous">&lsaquo;</button>
<img id="lbimg" alt=""><div class="cap" id="cap"></div><button class="n" aria-label="Next">&rsaquo;</button></div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const items=D.items,project=D.project;
const sel={adapter:new Set(),stage:new Set()};
const $=id=>document.getElementById(id);
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function nav(){const n=$('nav');const counts={};items.forEach(i=>counts[i.project]=(counts[i.project]||0)+1);
 const a=el('a',project?'':'on','All ('+items.length+')');a.href='/';n.append(a);
 Object.keys(counts).sort().forEach(p=>{const l=el('a',p===project?'on':'',p+' ('+counts[p]+')');
  l.href='/'+encodeURIComponent(p)+'/';n.append(l)})}
const scope=()=>items.filter(i=>!project||i.project===project);
function chips(){const c=$('chips');c.textContent='';
 ['adapter','stage'].forEach(k=>{const vals=[...new Set(scope().map(i=>i[k]))].sort();
  if(vals.length<2&&!sel[k].size)return;c.append(el('span','lbl',k));
  vals.forEach(v=>{const x=el('span','chip'+(sel[k].has(v)?' on':''),v);
   x.onclick=()=>{sel[k].has(v)?sel[k].delete(v):sel[k].add(v);chips();grid()};c.append(x)})})}
let view=[];
function grid(){const g=$('grid');g.textContent='';
 view=scope().filter(i=>(!sel.adapter.size||sel.adapter.has(i.adapter))&&(!sel.stage.size||sel.stage.has(i.stage)));
 $('empty').hidden=view.length>0;
 view.forEach((i,ix)=>{const c=el('div','card');const im=el('img');im.src=i.url;im.loading='lazy';im.alt=i.file;
  const m=el('div','m');m.append(el('b','',i.file),el('div','',i.adapter+' / '+i.stage+(project?'':' / '+i.project)));
  c.append(im,m);c.onclick=()=>open(ix);g.append(c)})}
let cur=-1;
function open(ix){cur=(ix+view.length)%view.length;const i=view[cur];$('lbimg').src=i.url;
 const bits=[i.file,i.adapter+' / '+i.stage];
 if(i.source)bits.push('source '+i.source+(i.git_sha?' @'+i.git_sha+(i.git_dirty?'*':''):''));
 if(i.tool_version)bits.push(i.tool_version);if(i.created_utc)bits.push(i.created_utc);
 $('cap').textContent=bits.join('  |  ');$('lb').classList.add('open')}
function close(){$('lb').classList.remove('open');cur=-1}
$('lb').querySelector('.x').onclick=close;$('lb').querySelector('.p').onclick=()=>open(cur-1);
$('lb').querySelector('.n').onclick=()=>open(cur+1);
$('lb').onclick=e=>{if(e.target.id==='lb')close()};
document.addEventListener('keydown',e=>{if(cur<0)return;
 if(e.key==='Escape')close();else if(e.key==='ArrowLeft')open(cur-1);else if(e.key==='ArrowRight')open(cur+1)});
nav();chips();grid();
</script></body></html>
"""


def render_index(items: list, project: str | None) -> str:
    data = json.dumps({"items": items, "project": project})
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    title = f" - {project}" if project else ""
    from html import escape
    head, tail = PAGE.split("__DATA__", 1)
    return head.replace("__TITLE__", escape(title)) + data + tail
