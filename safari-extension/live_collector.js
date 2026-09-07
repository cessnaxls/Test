// ==UserScript==
// @name         Instagram CLIP Live Scroll Collector
// @namespace    local.clip.collector
// @version      1.0.0
// @description  Collect Instagram profiles you manually scroll past and send them to your CLIP library.
// @match        https://www.instagram.com/*
// @match        https://instagram.com/*
// @run-at       document-idle
// ==/UserScript==

(() => {
  'use strict';

  const API_BASE = 'https://instagram-profile-search.onrender.com';
  const DEVICE_ID = 'iphone';
  const BATCH_SIZE = 25;
  const FLUSH_MS = 3500;
  const MAX_LOCAL_QUEUE = 5000;

  const BLOCKED = new Set([
    'explore','reels','accounts','direct','stories','p','reel','tv','about',
    'developer','legal','privacy','terms','web','emails','challenge','api',
    'directory','push','settings','notifications'
  ]);

  const K_ENABLED = 'clip_live_enabled_v1';
  const K_QUEUE = 'clip_live_queue_v1';
  const K_SENT = 'clip_live_sent_v1';

  let enabled = localStorage.getItem(K_ENABLED) === '1';
  let queue = readQueue();
  let sent = Number(localStorage.getItem(K_SENT) || 0);
  let seen = new Set(queue.map(x => x.username));
  let flushing = false;
  let scanTimer = 0;
  let flushTimer = 0;
  let observer = null;

  function readQueue() {
    try {
      const x = JSON.parse(localStorage.getItem(K_QUEUE) || '[]');
      return Array.isArray(x) ? x : [];
    } catch (_) {
      return [];
    }
  }

  function saveQueue() {
    if (queue.length > MAX_LOCAL_QUEUE) queue = queue.slice(-MAX_LOCAL_QUEUE);
    try { localStorage.setItem(K_QUEUE, JSON.stringify(queue)); } catch (_) {}
    updateUI();
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
    // Prefer an image inside the link itself.
    const direct = anchor.querySelector?.('img');
    if (direct?.currentSrc || direct?.src) return direct.currentSrc || direct.src;

    // Climb only a few levels so we don't accidentally grab a post image.
    let node = anchor.parentElement;
    for (let i = 0; node && i < 5; i++, node = node.parentElement) {
      const imgs = node.querySelectorAll?.('img') || [];
      for (const img of imgs) {
        const src = img.currentSrc || img.src || '';
        if (!src) continue;
        const alt = (img.alt || '').toLowerCase();
        const w = Number(img.width || 0), h = Number(img.height || 0);
        const likelyAvatar =
          alt.includes('profile') ||
          alt.includes('photo') ||
          (w > 0 && h > 0 && w <= 200 && h <= 200);
        if (likelyAvatar) return src;
      }
    }
    return '';
  }

  function nearbyName(anchor, username) {
    let node = anchor.parentElement;
    for (let i = 0; node && i < 3; i++, node = node.parentElement) {
      const text = (node.innerText || '').trim();
      if (!text || text.length > 300) continue;
      const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
      for (const line of lines) {
        const v = line.replace(/^@/, '').toLowerCase();
        if (v === username) continue;
        if (/^(follow|following|requested|verified)$/i.test(line)) continue;
        if (line.length <= 100) return line;
      }
    }
    return '';
  }

  function scan() {
    scanTimer = 0;
    if (!enabled) return;

    const links = document.querySelectorAll('a[href]');
    let added = 0;

    for (const a of links) {
      const username = usernameFromHref(a.href);
      if (!username || seen.has(username)) continue;

      const rec = {
        username,
        full_name: nearbyName(a, username),
        profile_url: `https://www.instagram.com/${username}/`,
        image_url: nearestAvatar(a),
        source: 'manual_safari_scroll',
        post_url: location.href.split('?')[0],
        observed_at: Date.now()
      };

      seen.add(username);
      queue.push(rec);
      added++;
    }

    if (added) {
      saveQueue();
      if (queue.length >= BATCH_SIZE) flush();
    }
  }

  function scheduleScan(delay = 180) {
    if (!enabled) return;
    clearTimeout(scanTimer);
    scanTimer = setTimeout(scan, delay);
  }

  async function flush() {
    if (!enabled || flushing || queue.length === 0) return;
    flushing = true;
    updateUI('sending');

    const batch = queue.slice(0, BATCH_SIZE);
    try {
      const r = await fetch(`${API_BASE}/api/browser/ingest`, {
        method: 'POST',
        mode: 'cors',
        credentials: 'omit',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          device_id: DEVICE_ID,
          page_url: location.href.split('?')[0],
          records: batch
        })
      });

      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();

      queue.splice(0, batch.length);
      sent += batch.length;
      localStorage.setItem(K_SENT, String(sent));
      saveQueue();
      updateUI(`sent ${data.queued ?? data.captured ?? batch.length}`);

      if (queue.length) setTimeout(flush, 150);
    } catch (e) {
      updateUI(`retry: ${String(e.message || e).slice(0, 50)}`);
    } finally {
      flushing = false;
    }
  }

  function setEnabled(on) {
    enabled = !!on;
    localStorage.setItem(K_ENABLED, enabled ? '1' : '0');
    if (enabled) {
      startObserver();
      scheduleScan(20);
      flushTimer = setInterval(flush, FLUSH_MS);
    } else {
      stopObserver();
      clearInterval(flushTimer);
      flushTimer = 0;
    }
    updateUI();
  }

  function startObserver() {
    if (observer) return;
    observer = new MutationObserver(() => scheduleScan(120));
    observer.observe(document.documentElement, {childList:true, subtree:true});
    addEventListener('scroll', onScroll, {passive:true});
  }

  function stopObserver() {
    if (observer) observer.disconnect();
    observer = null;
    removeEventListener('scroll', onScroll);
  }

  function onScroll() {
    scheduleScan(150);
  }

  function updateUI(extra = '') {
    const box = document.getElementById('__clip_live_collector');
    if (!box) return;
    const dot = box.querySelector('.clip-dot');
    const state = box.querySelector('.clip-state');
    const meta = box.querySelector('.clip-meta');
    const btn = box.querySelector('button');

    dot.style.background = enabled ? '#2ecc71' : '#999';
    state.textContent = enabled ? 'Scanner ON' : 'Scanner OFF';
    meta.textContent = `queued ${queue.length} · sent ${sent}${extra ? ' · ' + extra : ''}`;
    btn.textContent = enabled ? 'Turn Off' : 'Turn On';
  }

  function makeUI() {
    if (document.getElementById('__clip_live_collector')) return;
    const box = document.createElement('div');
    box.id = '__clip_live_collector';
    box.innerHTML = `
      <div class="clip-line"><span class="clip-dot"></span><strong class="clip-state">Scanner OFF</strong></div>
      <div class="clip-meta">queued 0 · sent 0</div>
      <div class="clip-buttons"><button type="button">Turn On</button><button type="button" data-flush>Send Now</button></div>
    `;
    Object.assign(box.style, {
      position:'fixed', right:'10px', bottom:'18px', zIndex:'2147483647',
      background:'rgba(20,20,20,.92)', color:'#fff', padding:'10px 12px',
      borderRadius:'14px', font:'12px -apple-system,BlinkMacSystemFont,sans-serif',
      boxShadow:'0 4px 20px rgba(0,0,0,.35)', minWidth:'155px'
    });
    const style = document.createElement('style');
    style.textContent = `
      #__clip_live_collector .clip-line{display:flex;align-items:center;gap:7px;margin-bottom:4px}
      #__clip_live_collector .clip-dot{width:9px;height:9px;border-radius:50%;display:inline-block}
      #__clip_live_collector .clip-meta{opacity:.8;margin-bottom:7px}
      #__clip_live_collector .clip-buttons{display:flex;gap:6px}
      #__clip_live_collector button{font:inherit;border:0;border-radius:9px;padding:6px 9px;background:#fff;color:#111}
    `;
    document.documentElement.appendChild(style);
    document.documentElement.appendChild(box);
    box.querySelector('button').onclick = () => setEnabled(!enabled);
    box.querySelector('[data-flush]').onclick = () => flush();
    updateUI();
  }

  makeUI();
  if (enabled) setEnabled(true);
})();
