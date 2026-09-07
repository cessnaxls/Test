// ==UserScript==
// @name         Instagram CLIP Live Scroll Collector AVATAR TURBO
// @namespace    local.clip.collector
// @version      3.0.0
// @description  High-throughput collector that uploads authenticated avatar bytes for CLIP indexing.
// @match        https://www.instagram.com/*
// @match        https://instagram.com/*
// @run-at       document-idle
// ==/UserScript==

(() => {
  'use strict';

  const API_BASE = 'https://instagram-profile-search.onrender.com';
  const DEVICE_ID = 'iphone';
  const BATCH_SIZE = 25;
  const AVATAR_FETCH_WORKERS = 8;
  const FLUSH_MS = 400;
  const MAX_LOCAL_QUEUE = 20000;
  const MAX_SEEN = 30000;

  const BLOCKED = new Set([
    'explore','reels','accounts','direct','stories','p','reel','tv','about',
    'developer','legal','privacy','terms','web','emails','challenge','api',
    'directory','push','settings','notifications'
  ]);

  const K_ENABLED = 'clip_live_enabled_v3';
  const K_QUEUE = 'clip_live_queue_v3';
  const K_SEEN = 'clip_live_seen_v3';
  const K_SENT = 'clip_live_sent_v3';

  let enabled = localStorage.getItem(K_ENABLED) === '1';
  let queue = readArray(K_QUEUE);
  let seenOrder = readArray(K_SEEN);
  let seen = new Set(seenOrder);
  let sent = Number(localStorage.getItem(K_SENT) || 0);
  let flushing = false;
  let observer = null;
  let flushTimer = 0;
  let persistTimer = 0;
  let pageAdded = 0;
  const processedAnchors = new WeakSet();

  function readArray(key) {
    try {
      const x = JSON.parse(localStorage.getItem(key) || '[]');
      return Array.isArray(x) ? x : [];
    } catch (_) { return []; }
  }

  function persistSoon() {
    clearTimeout(persistTimer);
    persistTimer = setTimeout(() => {
      try {
        if (queue.length > MAX_LOCAL_QUEUE) queue = queue.slice(-MAX_LOCAL_QUEUE);
        if (seenOrder.length > MAX_SEEN) {
          seenOrder = seenOrder.slice(-MAX_SEEN);
          seen = new Set(seenOrder);
        }
        localStorage.setItem(K_QUEUE, JSON.stringify(queue));
        localStorage.setItem(K_SEEN, JSON.stringify(seenOrder));
        localStorage.setItem(K_SENT, String(sent));
      } catch (_) {}
      updateUI();
    }, 80);
  }

  function usernameFromHref(href) {
    try {
      const u = new URL(href, location.href);
      if (!/(^|\.)instagram\.com$/i.test(u.hostname)) return '';
      const parts = u.pathname.split('/').filter(Boolean);
      if (parts.length !== 1) return '';
      const username = parts[0].replace(/^@/, '').toLowerCase();
      if (!username || BLOCKED.has(username) || !/^[a-z0-9._]{1,64}$/i.test(username)) return '';
      return username;
    } catch (_) { return ''; }
  }

  function nearestAvatar(anchor) {
    const direct = anchor.querySelector?.('img');
    if (direct?.currentSrc || direct?.src) return direct.currentSrc || direct.src;
    let node = anchor.parentElement;
    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
      const imgs = node.querySelectorAll?.('img') || [];
      for (const img of imgs) {
        const src = img.currentSrc || img.src || '';
        if (!src) continue;
        const alt = (img.alt || '').toLowerCase();
        const w = Number(img.width || 0), h = Number(img.height || 0);
        if (alt.includes('profile') || alt.includes('photo') || (w >= 20 && h >= 20 && w <= 200 && h <= 200)) return src;
      }
    }
    return '';
  }

  function nearbyName(anchor, username) {
    let node = anchor.parentElement;
    for (let depth = 0; node && depth < 2; depth++, node = node.parentElement) {
      const text = (node.innerText || '').trim();
      if (!text || text.length > 220) continue;
      for (const line of text.split('\n').map(s => s.trim()).filter(Boolean)) {
        const v = line.replace(/^@/, '').toLowerCase();
        if (v === username || /^(follow|following|requested|verified)$/i.test(line)) continue;
        if (line.length <= 100) return line;
      }
    }
    return '';
  }

  function collectAnchor(a) {
    if (!enabled || !a || processedAnchors.has(a)) return 0;
    processedAnchors.add(a);
    const username = usernameFromHref(a.href);
    if (!username || seen.has(username)) return 0;

    const rec = {
      username,
      full_name: nearbyName(a, username),
      profile_url: `https://www.instagram.com/${username}/`,
      image_url: nearestAvatar(a),
      source: 'manual_safari_scroll_avatar_turbo',
      post_url: location.href.split('?')[0],
      observed_at: Date.now()
    };

    seen.add(username);
    seenOrder.push(username);
    queue.push(rec);
    pageAdded++;
    return 1;
  }

  function scanNode(node) {
    if (!enabled || !node || node.nodeType !== 1) return 0;
    let added = 0;
    if (node.matches?.('a[href]')) added += collectAnchor(node);
    const links = node.querySelectorAll?.('a[href]');
    if (links) for (const a of links) added += collectAnchor(a);
    if (added) {
      persistSoon();
      if (queue.length >= BATCH_SIZE) flush();
    }
    return added;
  }

  function initialScan() {
    if (!enabled) return;
    for (const a of document.querySelectorAll('a[href]')) collectAnchor(a);
    persistSoon();
    if (queue.length >= BATCH_SIZE) flush();
  }

  async function blobToAvatarBase64(blob) {
    try {
      const bitmap = await createImageBitmap(blob);
      const size = 160;
      const canvas = document.createElement('canvas');
      canvas.width = size; canvas.height = size;
      const ctx = canvas.getContext('2d', {alpha:false});
      const scale = Math.max(size / bitmap.width, size / bitmap.height);
      const w = bitmap.width * scale, h = bitmap.height * scale;
      ctx.drawImage(bitmap, (size-w)/2, (size-h)/2, w, h);
      if (bitmap.close) bitmap.close();
      return canvas.toDataURL('image/jpeg', 0.72).split(',',2)[1] || '';
    } catch (_) {
      return await new Promise((resolve) => {
        const fr = new FileReader();
        fr.onload = () => resolve(String(fr.result || '').split(',',2)[1] || '');
        fr.onerror = () => resolve('');
        fr.readAsDataURL(blob);
      });
    }
  }

  async function fetchAvatarBytes(rec) {
    if (!rec.image_url) return rec;
    try {
      if (rec.image_url.startsWith('data:image/')) {
        return {...rec, avatar_base64: rec.image_url.split(',',2)[1] || ''};
      }
      const r = await fetch(rec.image_url, {
        method:'GET',
        mode:'cors',
        credentials:'include',
        cache:'force-cache'
      });
      if (!r.ok) throw new Error(`avatar ${r.status}`);
      const blob = await r.blob();
      if (!blob.type.startsWith('image/')) throw new Error('not image');
      const avatar_base64 = await blobToAvatarBase64(blob);
      return avatar_base64 ? {...rec, avatar_base64} : rec;
    } catch (_) {
      return rec;
    }
  }

  async function hydrateAvatars(batch) {
    const out = new Array(batch.length);
    let next = 0;
    async function worker() {
      while (true) {
        const i = next++;
        if (i >= batch.length) return;
        out[i] = await fetchAvatarBytes(batch[i]);
      }
    }
    await Promise.all(Array.from({length:Math.min(AVATAR_FETCH_WORKERS,batch.length)}, worker));
    return out;
  }

  async function flush() {
    if (!enabled || flushing || queue.length === 0) return;
    flushing = true;
    updateUI('sending');
    const rawBatch = queue.slice(0, BATCH_SIZE);
    try {
      const batch = await hydrateAvatars(rawBatch);
      const r = await fetch(`${API_BASE}/api/browser/ingest`, {
        method: 'POST', mode: 'cors', credentials: 'omit',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({device_id: DEVICE_ID, page_url: location.href.split('?')[0], records: batch})
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      queue.splice(0, batch.length);
      sent += batch.length;
      persistSoon();
      updateUI(`+${data.queued ?? data.captured ?? batch.length}`);
    } catch (e) {
      updateUI(`retry ${String(e.message || e).slice(0, 36)}`);
    } finally {
      flushing = false;
      if (enabled && queue.length) setTimeout(flush, 25);
    }
  }

  function startObserver() {
    if (observer) return;
    observer = new MutationObserver(records => {
      if (!enabled) return;
      for (const m of records) for (const n of m.addedNodes) scanNode(n);
    });
    observer.observe(document.documentElement, {childList:true, subtree:true});
  }

  function stopObserver() {
    observer?.disconnect();
    observer = null;
  }

  function setEnabled(on) {
    enabled = !!on;
    localStorage.setItem(K_ENABLED, enabled ? '1' : '0');
    if (enabled) {
      startObserver();
      initialScan();
      clearInterval(flushTimer);
      flushTimer = setInterval(flush, FLUSH_MS);
    } else {
      stopObserver();
      clearInterval(flushTimer);
      flushTimer = 0;
      persistSoon();
    }
    updateUI();
  }

  function updateUI(extra='') {
    const box = document.getElementById('__clip_live_collector');
    if (!box) return;
    box.querySelector('.clip-dot').style.background = enabled ? '#2ecc71' : '#999';
    box.querySelector('.clip-state').textContent = enabled ? 'TURBO ON' : 'AVATAR TURBO OFF';
    box.querySelector('.clip-meta').textContent = `page +${pageAdded} · queued ${queue.length} · sent ${sent}${extra ? ' · '+extra : ''}`;
    box.querySelector('[data-toggle]').textContent = enabled ? 'Turn Off' : 'Turn On';
  }

  function makeUI() {
    if (document.getElementById('__clip_live_collector')) return;
    const box = document.createElement('div');
    box.id='__clip_live_collector';
    box.innerHTML=`<div class="clip-line"><span class="clip-dot"></span><strong class="clip-state">AVATAR TURBO OFF</strong></div><div class="clip-meta">page +0 · queued ${queue.length} · sent ${sent}</div><div class="clip-buttons"><button data-toggle type="button">Turn On</button><button data-flush type="button">Send Now</button></div>`;
    Object.assign(box.style,{position:'fixed',right:'10px',bottom:'18px',zIndex:'2147483647',background:'rgba(20,20,20,.94)',color:'#fff',padding:'10px 12px',borderRadius:'14px',font:'12px -apple-system,BlinkMacSystemFont,sans-serif',boxShadow:'0 4px 20px rgba(0,0,0,.35)',minWidth:'190px'});
    const style=document.createElement('style');
    style.textContent=`#__clip_live_collector .clip-line{display:flex;align-items:center;gap:7px;margin-bottom:4px}#__clip_live_collector .clip-dot{width:9px;height:9px;border-radius:50%;display:inline-block}#__clip_live_collector .clip-meta{opacity:.82;margin-bottom:7px}#__clip_live_collector .clip-buttons{display:flex;gap:6px}#__clip_live_collector button{font:inherit;border:0;border-radius:9px;padding:6px 9px;background:#fff;color:#111}`;
    document.documentElement.appendChild(style); document.documentElement.appendChild(box);
    box.querySelector('[data-toggle]').onclick=()=>setEnabled(!enabled);
    box.querySelector('[data-flush]').onclick=()=>flush();
    updateUI();
  }

  makeUI();
  if (enabled) setEnabled(true);
})();
