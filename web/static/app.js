const $=id=>document.getElementById(id);const device='iphone';localStorage.setItem('device_id',device);$('ingestUrl').textContent=`${location.origin}/api/shortcut/ingest`;
async function api(p,o={}){const r=await fetch(p,o);if(!r.ok)throw Error(await r.text());return r.json()}function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function show(rows){$('results').innerHTML=rows.map(x=>`<div class="profile">${x.image_data_url?`<img loading="lazy" src="${x.image_data_url}">`:'<div class="avatar-placeholder"></div>'}<div><a target="_blank" href="${esc(x.profile_url)}">@${esc(x.username)}</a><div>${esc(x.full_name||'')}</div><div class="meta">${x.score!=null?`CLIP ${Number(x.score).toFixed(3)} · `:''}${x.seen_count?`seen ${x.seen_count}×`:''}</div></div></div>`).join('')||'<p class="sub">No profiles found.</p>'}
async function refresh(){try{
 let s=await api(`/api/live/stats?device_id=${encodeURIComponent(device)}`);
 $('captured').textContent=(s.captured||0).toLocaleString();
 $('queued').textContent=(s.queued||0).toLocaleString();
 $('processing').textContent=(s.processing||0).toLocaleString();
 $('count').textContent=(s.indexed||0).toLocaleString();
 $('failed').textContent=(s.failed||0).toLocaleString();
 $('rate').textContent=`${Number(s.rate_per_sec||0).toFixed(1)}/s`;
 const eta=s.eta_seconds; $('eta').textContent=eta==null?'':`Estimated time remaining: ${eta>=3600?Math.floor(eta/3600)+'h '+Math.ceil((eta%3600)/60)+'m':eta>=60?Math.floor(eta/60)+'m '+Math.ceil(eta%60)+'s':Math.ceil(eta)+'s'}`;
 const total=Math.max(s.captured||0,0), done=Math.min((s.indexed||0)+(s.failed||0),total);
 const pct=total?Math.round(done/total*100):0;
 $('progressBar').style.width=`${pct}%`;
 $('progressText').textContent=total?`${done.toLocaleString()} of ${total.toLocaleString()} processed · ${pct}%`:'Waiting for captures…';
 $('storage').textContent=s.persistent?'Persistent Supabase storage: connected':'Temporary storage only — Supabase still needs to be connected.';
 $('status').textContent=s.last_error?`${s.message||''} · Last error: ${s.last_error}`:(s.message||'');
}catch(e){$('status').textContent=e.message}}
$('copyShortcutJs').onclick=async()=>{let js=await fetch('/static/shortcut_collector.js').then(r=>r.text());await navigator.clipboard.writeText(js);$('status').textContent='Collector JavaScript copied.'}
$('recent').onclick=async()=>{let d=await api(`/api/live/profiles?device_id=${encodeURIComponent(device)}&limit=100`);show(d.profiles)};$('search').onclick=async()=>{try{$('searchStatus').textContent='Running CLIP… first search after a Render restart can be slower.';let d=await api(`/api/clip/search/text?device_id=${encodeURIComponent(device)}&q=${encodeURIComponent($('q').value)}&limit=100`);show(d.profiles);$('searchStatus').textContent='CLIP text-to-image search complete.'}catch(e){$('searchStatus').textContent=e.message}};
$('imageSearch').onclick=async()=>{let f=$('imageFile').files[0];if(!f)return;$('searchStatus').textContent='Embedding reference image…';let fd=new FormData();fd.append('file',f);try{let d=await api(`/api/clip/search/image?device_id=${encodeURIComponent(device)}&limit=100`,{method:'POST',body:fd});show(d.profiles);$('searchStatus').textContent='CLIP image-to-image search complete.'}catch(e){$('searchStatus').textContent=e.message}};refresh();setInterval(refresh,2500);$('recent').click();
