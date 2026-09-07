const $=id=>document.getElementById(id);const device='iphone';localStorage.setItem('device_id',device);
async function api(p,o={}){const r=await fetch(p,o);if(!r.ok)throw Error(await r.text());return r.json()}function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function show(rows){$('results').innerHTML=rows.map(x=>`<div class="profile">${x.image_data_url?`<img loading="lazy" src="${x.image_data_url}">`:'<div class="avatar-placeholder"></div>'}<div><a target="_blank" href="${esc(x.profile_url)}">@${esc(x.username)}</a><div>${esc(x.full_name||'')}</div><div class="meta">${x.score!=null?`CLIP ${Number(x.score).toFixed(3)} · `:''}${x.seen_count?`seen ${x.seen_count}×`:''}</div></div></div>`).join('')||'<p class="sub">No profiles found.</p>'}
async function importPayload(){
 const payload=$('profilePayload').value.trim();
 if(!payload){$('importStatus').textContent='Paste profile data first.';return}
 $('importStatus').textContent='Importing profiles…';
 try{
  const d=await api('/api/shortcut/ingest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:device,payload})});
  const captured=d.captured??d.scanner_count??0, queued=d.queued??0, skipped=d.skipped??0;
  $('importStatus').textContent=`Imported ${Number(captured).toLocaleString()} profiles · ${Number(queued).toLocaleString()} queued${skipped?` · ${Number(skipped).toLocaleString()} skipped`:''}.`;
  $('profilePayload').value='';
  await refresh();
 }catch(e){$('importStatus').textContent=`Import failed: ${e.message}`}
}
$('importProfiles').onclick=importPayload;
$('clearPaste').onclick=()=>{$('profilePayload').value='';$('importStatus').textContent=''};
$('pasteClipboard').onclick=async()=>{
 try{
  const t=await navigator.clipboard.readText();
  $('profilePayload').value=t;
  $('importStatus').textContent=t?'Clipboard pasted. Tap Import & Index.':'Clipboard is empty.';
 }catch(e){
  $('profilePayload').focus();
  $('importStatus').textContent='Safari blocked automatic clipboard access. Tap inside the box and choose Paste.';
 }
};
$('bulkClear').onclick=()=>{$('bulkText').value='';$('bulkStatus').textContent=''};
$('bulkImport').onclick=async()=>{
 const text=$('bulkText').value.trim();
 if(!text){$('bulkStatus').textContent='Paste usernames or Instagram profile URLs first.';return}
 $('bulkStatus').textContent='Parsing list and resolving profile images…';
 try{
  const d=await api('/api/bulk/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:device,text})});
  $('bulkStatus').textContent=`Parsed ${Number(d.parsed||0).toLocaleString()} · avatars ready ${Number((d.with_avatar_already||0)+(d.avatars_resolved||0)).toLocaleString()} · queued ${Number(d.queued||0).toLocaleString()} · unresolved ${Number(d.unresolved||0).toLocaleString()}.`;
  await refresh();
 }catch(e){$('bulkStatus').textContent=`Bulk import failed: ${e.message}`}
};
$('clearHtml').onclick=()=>{$('htmlPayload').value='';$('htmlStatus').textContent=''};
$('pasteHtml').onclick=async()=>{
 try{
  const t=await navigator.clipboard.readText();
  $('htmlPayload').value=t;
  $('htmlStatus').textContent=t?`Pasted ${(t.length/1000000).toFixed(2)} MB of HTML. Tap Extract Profiles & Index.`:'Clipboard is empty.';
 }catch(e){
  $('htmlPayload').focus();
  $('htmlStatus').textContent='Safari blocked automatic clipboard access. Tap inside the box and choose Paste.';
 }
};
$('importHtml').onclick=async()=>{
 const raw=$('htmlPayload').value;
 if(!raw.trim()){$('htmlStatus').textContent='Paste Instagram HTML first.';return}
 $('htmlStatus').textContent='Extracting profiles from HTML and resolving avatars…';
 try{
  const d=await api('/api/html/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:device,html:raw})});
  $('htmlStatus').textContent=`Extracted ${Number(d.extracted||0).toLocaleString()} profiles · avatars in HTML ${Number(d.avatars_in_html||0).toLocaleString()} · resolved ${Number(d.avatars_resolved||0).toLocaleString()} · queued ${Number(d.queued||0).toLocaleString()} · unresolved ${Number(d.unresolved||0).toLocaleString()}.`;
  await refresh();
 }catch(e){$('htmlStatus').textContent=`HTML import failed: ${e.message}`}
};
$('clearLibrary').onclick=async()=>{
 const ok=confirm('Clear ALL scraped profiles, queued jobs, CLIP vectors, and failed rows for this iPhone library? This cannot be undone.');
 if(!ok)return;
 $('clearLibraryStatus').textContent='Clearing scraped profiles…';
 try{
  const d=await api(`/api/live/profiles?device_id=${encodeURIComponent(device)}`,{method:'DELETE'});
  $('results').innerHTML='';
  $('q').value='';
  $('clearLibraryStatus').textContent=`Cleared ${Number(d.deleted||0).toLocaleString()} scraped profiles. Reset the Instagram scanner history too if you want to scrape the same people again.`;
  await refresh();
 }catch(e){$('clearLibraryStatus').textContent=`Clear failed: ${e.message}`}
};
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
