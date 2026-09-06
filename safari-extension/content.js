const ext = globalThis.browser || globalThis.chrome;
const sleep = ms => new Promise(r => setTimeout(r, ms));
let STOP = false;
let scannerEnabled = false;
let scanTimer = null;
let mutationTimer = null;
let observer = null;
const sentSignatures = new Map();

const BLOCKED = new Set([
  'explore','reels','accounts','direct','stories','p','reel','tv','about','developer','legal','privacy','terms',
  'web','emails','challenge','api','directory','push','settings'
]);

function usernameFromHref(h) {
  try {
    const u = new URL(h, location.href);
    if (u.hostname !== 'www.instagram.com' && u.hostname !== 'instagram.com') return '';
    const p = u.pathname.split('/').filter(Boolean);
    if (p.length !== 1) return '';
    const username = p[0].replace(/^@/, '').toLowerCase();
    if (!username || BLOCKED.has(username) || !/^[a-z0-9._]{1,64}$/i.test(username)) return '';
    return username;
  } catch { return ''; }
}

function isVisible(el) {
  if (!el?.isConnected) return false;
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  return r.bottom >= -100 && r.top <= innerHeight + 100;
}

function nearestImage(anchor) {
  const containers = [anchor, anchor.closest('header'), anchor.closest('article'), anchor.closest('[role="dialog"]'), anchor.parentElement, anchor.closest('div')].filter(Boolean);
  for (const box of containers) {
    const imgs = [...box.querySelectorAll('img')];
    const avatar = imgs.find(img => {
      const r = img.getBoundingClientRect();
      const alt = (img.alt || '').toLowerCase();
      return r.width > 20 && r.height > 20 && r.width < 200 && r.height < 200 && (alt.includes('profile') || alt.includes('photo') || img.closest('a[href]'));
    });
    if (avatar?.currentSrc || avatar?.src) return avatar.currentSrc || avatar.src;
  }
  return '';
}

function fullNameNear(anchor, username) {
  const candidates = [anchor.parentElement, anchor.closest('header'), anchor.closest('li'), anchor.closest('[role="dialog"]'), anchor.closest('div')].filter(Boolean);
  for (const box of candidates) {
    const lines = (box.innerText || '').split('\n').map(x => x.trim()).filter(Boolean).slice(0, 8);
    const name = lines.find(x => {
      const l = x.toLowerCase().replace(/^@/, '');
      return l !== username && x.length <= 80 && !/^follow(ing)?$/i.test(x) && !/^verified$/i.test(x) && !/^\d+[km,.]*\s*(likes?|comments?|followers?|following)?$/i.test(x);
    });
    if (name) return name;
  }
  return '';
}

function contextPostUrl(anchor) {
  const article = anchor.closest('article');
  const postLink = article?.querySelector('a[href*="/p/"],a[href*="/reel/"]');
  if (postLink?.href) return postLink.href.split('?')[0];
  if (/\/(p|reel)\//.test(location.pathname)) return location.href.split('?')[0];
  return location.href.split('?')[0];
}

function collectVisibleCandidates() {
  const map = new Map();
  const scopes = [...document.querySelectorAll('article,[role="dialog"],main')].filter(isVisible);
  const roots = scopes.length ? scopes : [document];
  for (const root of roots) {
    for (const a of root.querySelectorAll('a[href]')) {
      if (!isVisible(a) && !isVisible(a.closest('header,li,div'))) continue;
      const username = usernameFromHref(a.href);
      if (!username) continue;
      const rec = {
        username,
        full_name: fullNameNear(a, username),
        profile_url: `https://www.instagram.com/${username}/`,
        image_url: nearestImage(a),
        source: 'live_scroll',
        post_url: contextPostUrl(a),
        observed_at: Date.now(),
      };
      const old = map.get(username);
      if (!old || (!old.image_url && rec.image_url) || rec.full_name.length > old.full_name.length) map.set(username, rec);
    }
  }
  return [...map.values()];
}

function signature(r) { return `${r.username}|${r.image_url || ''}|${r.full_name || ''}`; }
function pruneSignatures() {
  const cutoff = Date.now() - 6 * 60 * 60 * 1000;
  for (const [k, t] of sentSignatures) if (t < cutoff) sentSignatures.delete(k);
}

async function scanNow(flush = false) {
  if (!scannerEnabled) return;
  pruneSignatures();
  const fresh = [];
  for (const r of collectVisibleCandidates()) {
    const sig = signature(r);
    if (sentSignatures.has(sig)) continue;
    sentSignatures.set(sig, Date.now());
    fresh.push(r);
  }
  if (fresh.length) {
    try { await ext.runtime.sendMessage({ type: 'live-candidates', records: fresh, flush }); } catch (_) {}
  }
}

function scheduleScan(delay = 180) {
  if (!scannerEnabled) return;
  clearTimeout(mutationTimer);
  mutationTimer = setTimeout(() => scanNow(false), delay);
}

function startScanner() {
  scannerEnabled = true;
  if (!observer) {
    observer = new MutationObserver(() => scheduleScan());
    observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['href', 'src'] });
  }
  if (!scanTimer) scanTimer = setInterval(() => scanNow(false), 1800);
  window.addEventListener('scroll', onScroll, { passive: true });
  scanNow(true);
}

function stopScanner() {
  scannerEnabled = false;
  clearTimeout(mutationTimer);
  if (scanTimer) clearInterval(scanTimer);
  scanTimer = null;
  observer?.disconnect(); observer = null;
  window.removeEventListener('scroll', onScroll);
  ext.runtime.sendMessage({ type: 'flush-live' }).catch(() => {});
}

function onScroll() { scheduleScan(120); }

ext.runtime.onMessage.addListener(m => {
  if (m.type === 'scanner-state') {
    if (m.enabled) startScanner(); else stopScanner();
    return;
  }
  if (m.type !== 'start-worker') return;
  STOP = false;
  const { job, worker } = m;
  (async () => {
    try {
      if (job.source_mode === 'post_stream_likers') await scrapePostStream(job, worker);
      else if (job.source_mode === 'commenters') await scrapeComments(job, worker);
      else await scrapeList(job, worker);
    } catch (e) { console.warn(e); }
    finally {
      await post(job, worker, '/finish', {}).catch(() => {});
      ext.runtime.sendMessage({ type: 'worker-finished' }).catch(() => {});
    }
  })();
});

ext.storage.local.get(['scannerEnabled']).then(v => { if (v.scannerEnabled) startScanner(); });

// ---------------- Existing automated collection helpers ----------------
function uniq(rows) { const m = new Map(); for (const r of rows) { const u = (r.username || '').toLowerCase(); if (u && !m.has(u)) m.set(u, r); } return [...m.values()]; }
function scanAnchors(source, postUrl = '') { const out = []; document.querySelectorAll('a[href^="/"]').forEach(a => { const u = usernameFromHref(a.href); if (!u) return; const box = a.closest('div'); const img = box?.querySelector('img') || a.querySelector('img'); const txt = (box?.innerText || a.innerText || '').split('\n').filter(Boolean); out.push({ username: u, full_name: txt.find(x => x !== u && x !== ('@' + u)) || '', profile_url: `https://www.instagram.com/${u}/`, image_url: img?.src || '', source, post_url: postUrl }); }); return uniq(out); }
function scrollable() { const els = [...document.querySelectorAll('div')].filter(e => e.scrollHeight > e.clientHeight + 200); return els.sort((a, b) => (b.clientHeight * b.scrollHeight) - (a.clientHeight * a.scrollHeight))[0] || document.scrollingElement; }
async function post(job, worker, path, payload = {}) { const c = await ext.storage.local.get(['serverUrl', 'deviceId']); const r = await fetch(c.serverUrl + `/api/jobs/${job.id}${path}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ device_id: c.deviceId, worker, ...payload }) }); const d = await r.json(); if (d.stop) STOP = true; return d; }
async function flush(job, worker, rows, msg) { if (!rows.length) return; await post(job, worker, '/records', { records: uniq(rows), message: msg }); }
async function openRelation(job) { if (job.source_mode === 'profile_followers' || job.source_mode === 'profile_following') { const rel = job.source_mode === 'profile_followers' ? 'followers' : 'following'; const a = [...document.querySelectorAll('a')].find(x => (x.getAttribute('href') || '').includes('/' + rel + '/') || x.innerText.toLowerCase().includes(rel)); a?.click(); await sleep(1400); } }
async function openLikesIfNeeded(job) { if (job.source_mode === 'likers') { const a = [...document.querySelectorAll('a,button')].find(x => (x.getAttribute('href') || '').includes('/liked_by/') || /\blikes?\b/i.test(x.innerText || '')); a?.click(); await sleep(1200); } }
async function scrapeList(job, worker) { await openRelation(job); await openLikesIfNeeded(job); const box = scrollable(), seen = new Set(); let stall = 0; const start = Date.now(); while (!STOP) { const rows = scanAnchors(job.source_mode, location.href).filter(r => hash(r.username) % job.browser_windows === worker); const fresh = rows.filter(r => !seen.has(r.username)); fresh.forEach(r => seen.add(r.username)); if (fresh.length) await flush(job, worker, fresh, `Worker ${worker + 1}: ${seen.size} unique profiles`); stall = fresh.length ? 0 : stall + 1; if (seen.size >= Math.ceil(job.profile_limit / job.browser_windows) || stall > 18) break; if (job.time_limit_seconds && Date.now() - start > job.time_limit_seconds * 1000) break; box.scrollTop = box.scrollHeight; window.scrollTo(0, document.body.scrollHeight); await sleep(700); } }
async function scrapeComments(job, worker) { const seen = new Set(), start = Date.now(); let stall = 0; while (!STOP) { const rows = scanAnchors('commenters', location.href).filter(r => hash(r.username) % job.browser_windows === worker); const fresh = rows.filter(r => !seen.has(r.username)); fresh.forEach(r => seen.add(r.username)); if (fresh.length) await flush(job, worker, fresh, `Worker ${worker + 1}: ${seen.size} comment authors`); const more = [...document.querySelectorAll('button,div[role=button]')].find(x => /view.*comments|load more comments/i.test(x.innerText || '')); more?.click(); window.scrollBy(0, 600); await sleep(650); stall = fresh.length ? 0 : stall + 1; if (stall > 20 || seen.size >= Math.ceil(job.profile_limit / job.browser_windows)) break; if (job.time_limit_seconds && Date.now() - start > job.time_limit_seconds * 1000) break; } }
function postUrls() { return [...new Set([...document.querySelectorAll('a[href*="/p/"],a[href*="/reel/"]')].map(a => a.href.split('?')[0]))]; }
async function scrapePostStream(job, worker) { let posts = postUrls(); if (/\/(p|reel)\//.test(location.pathname)) posts = [location.href.split('?')[0]]; const mine = posts.filter(p => hash(p) % job.browser_windows === worker); for (let i = 0; i < mine.length && !STOP; i++) { location.href = mine[i].replace(/\/$/, '') + '/liked_by/'; await sleep(1800); const rows = scanAnchors('post_stream_likers', mine[i]).filter(r => hash(r.username) % job.browser_windows === worker); await flush(job, worker, rows, `Worker ${worker + 1}: post ${i + 1}/${mine.length}`); } }
function hash(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) h = (h ^ s.charCodeAt(i)) * 16777619 >>> 0; return h >>> 0; }
