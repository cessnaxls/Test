const $ = id => document.getElementById(id);
let job = null, timer = null, liveTimer = null;
let device = localStorage.getItem('device_id');
if (!device) { device = crypto.randomUUID(); localStorage.setItem('device_id', device); }
$('device').textContent = `Pairing Device ID: ${device}`;
$('shortcutDevice').textContent = device;
$('ingestUrl').textContent = `${location.origin}/api/live/ingest`;

$('copyDevice').onclick = async () => {
  try { await navigator.clipboard.writeText(device); $('shortcutCopyStatus').textContent = 'Device ID copied.'; }
  catch (_) { $('shortcutCopyStatus').textContent = `Device ID: ${device}`; }
};
$('copyShortcutJs').onclick = async () => {
  try {
    const js = await fetch('/static/shortcut_collector.js').then(r => r.text());
    await navigator.clipboard.writeText(js);
    $('shortcutCopyStatus').textContent = 'Shortcut JavaScript copied. Paste it into “Run JavaScript on Web Page” in Shortcuts.';
  } catch (e) { $('shortcutCopyStatus').textContent = `Could not copy automatically: ${e.message}`; }
};

async function api(path, opt = {}) {
  const r = await fetch(path, { headers: { 'content-type': 'application/json' }, ...opt });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function safeUrl(s) { try { const u = new URL(s); return /^https?:$/.test(u.protocol) ? u.href : ''; } catch { return ''; } }
function show(rows) {
  $('results').innerHTML = rows.map(x => {
    const img = safeUrl(x.image_url || ''); const profile = safeUrl(x.profile_url || '');
    return `<div class="profile">${img ? `<img loading="lazy" src="${esc(img)}">` : '<div class="avatar-placeholder"></div>'}<div>${profile ? `<a target="_blank" rel="noopener" href="${esc(profile)}">@${esc(x.username)}</a>` : `@${esc(x.username)}`}<div>${esc(x.full_name || '')}</div><div class="meta">${esc(x.source || '')}${x.seen_count ? ` · seen ${esc(x.seen_count)}×` : ''}</div></div></div>`;
  }).join('') || '<p class="sub">No profiles found.</p>';
}

async function refreshLive(loadRecent = false) {
  try {
    const s = await api(`/api/live/stats?device_id=${encodeURIComponent(device)}`);
    $('liveCount').textContent = Number(s.profiles_collected || 0).toLocaleString();
    $('liveBadge').textContent = s.scanner_enabled ? 'ON' : 'OFF';
    $('liveBadge').className = `badge ${s.scanner_enabled ? 'on' : 'off'}`;
    $('liveStatus').textContent = s.message || 'No scanner activity yet.';
    $('liveSeen').textContent = s.last_seen ? new Date(s.last_seen * 1000).toLocaleTimeString() : '—';
    if (loadRecent) {
      const d = await api(`/api/live/profiles?device_id=${encodeURIComponent(device)}&limit=100`);
      show(d.profiles);
    }
  } catch (e) { $('liveStatus').textContent = e.message; }
  clearTimeout(liveTimer); liveTimer = setTimeout(() => refreshLive(false), 3000);
}

$('search').onclick = async () => {
  try { const d = await api(`/api/live/search?device_id=${encodeURIComponent(device)}&q=${encodeURIComponent($('q').value)}&limit=200`); show(d.profiles); }
  catch (e) { $('liveStatus').textContent = e.message; }
};
$('recent').onclick = () => refreshLive(true);
$('refresh').onclick = () => refreshLive(true);
$('q').addEventListener('keydown', e => { if (e.key === 'Enter') $('search').click(); });
$('clear').onclick = async () => {
  if (!confirm('Clear every profile in this device library? This cannot be undone.')) return;
  try { await api(`/api/live/profiles?device_id=${encodeURIComponent(device)}`, { method: 'DELETE' }); show([]); refreshLive(false); }
  catch (e) { $('liveStatus').textContent = e.message; }
};

// Existing automated modes remain unchanged.
const modeKey = () => `windows:${$('mode').value}`;
function loadW() { $('windows').value = localStorage.getItem(modeKey()) || 1; }
$('mode').onchange = loadW; loadW();
$('windows').onchange = () => localStorage.setItem(modeKey(), $('windows').value);
$('start').onclick = async () => {
  try {
    job = await api('/api/jobs', { method: 'POST', body: JSON.stringify({
      device_id: device, url: $('url').value, source_mode: $('mode').value,
      browser_windows: +$('windows').value, profile_limit: +$('limit').value,
      comment_limit: +$('comments').value, exact_like_count: $('exact').value ? +$('exact').value : null,
      time_limit_seconds: $('timer').value ? +$('timer').value : null
    }) });
    $('stop').disabled = false; pollJob();
  } catch (e) { $('status').textContent = e.message; }
};
$('stop').onclick = async () => { if (job) await api(`/api/jobs/${job.id}/stop`, { method: 'POST', body: JSON.stringify({ device_id: device }) }); pollJob(); };
async function pollJob() {
  if (!job) return;
  try {
    job = await api(`/api/jobs/${job.id}`);
    $('status').textContent = `${job.status}: ${job.message} — ${job.profiles_collected} profiles — ${job.workers_done}/${job.browser_windows} workers finished`;
    if (!['complete','failed','superseded'].includes(job.status)) { clearTimeout(timer); timer = setTimeout(pollJob, 1200); }
    else $('stop').disabled = true;
  } catch (e) { $('status').textContent = e.message; }
}

refreshLive(true);
