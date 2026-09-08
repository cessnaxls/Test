// ==UserScript==
// @name         Instagram Profile Scraper → Render Gallery
// @namespace    local.instagram.profile.gallery
// @version      1.0.0
// @description  Sends profiles loaded while scrolling Instagram to the Render profile gallery.
// @match        https://www.instagram.com/*
// @match        https://instagram.com/*
// @run-at       document-idle
// ==/UserScript==

(() => {
  'use strict';

  const API_BASE = 'https://instagram-profile-search.onrender.com';
  const BATCH_SIZE = 50;
  const FLUSH_MS = 800;

  const BLOCKED = new Set([
    'explore','reels','accounts','direct','stories','p','reel','tv','about',
    'developer','legal','privacy','terms','web','emails','challenge','api',
    'directory','push','settings','notifications'
  ]);

  const K_ENABLED = 'profile_gallery_enabled_v1';
  const K_QUEUE = 'profile_gallery_queue_v1';
  const K_SEEN = 'profile_gallery_seen_v1';
  const K_SENT = 'profile_gallery_sent_v1';

  let enabled = localStorage.getItem(K_ENABLED) === '1';
  let queue = read(K_QUEUE);
  let seenList = read(K_SEEN);
  let seen = new Set(seenList);
  let sent = Number(localStorage.getItem(K_SENT) || 0);
  let flushing = false;
  let observer = null;
  let timer = null;
  const processed = new WeakSet();

  function read(key) {
    try {
      const value = JSON.parse(localStorage.getItem(key) || '[]');
      return Array.isArray(value) ? value : [];
    } catch (_) {
      return [];
    }
  }

  function save() {
    try {
      localStorage.setItem(K_QUEUE, JSON.stringify(queue.slice(-10000)));
      localStorage.setItem(K_SEEN, JSON.stringify(seenList.slice(-30000)));
      localStorage.setItem(K_SENT, String(sent));
    } catch (_) {}
    updateUI();
  }

  function usernameFromHref(href) {
    try {
      const u = new URL(href, location.href);
      if (!/(^|\.)instagram\.com$/i.test(u.hostname)) return '';
      const parts = u.pathname.split('/').filter(Boolean);
      if (parts.length !== 1) return '';
      const username = parts[0].replace(/^@/, '').toLowerCase();
      if (!username || BLOCKED.has(username)) return '';
      if (!/^[a-z0-9._]{1,64}$/i.test(username)) return '';
      return username;
    } catch (_) {
      return '';
    }
  }

  function imageNear(anchor) {
    const direct = anchor.querySelector?.('img');
    if (direct?.currentSrc || direct?.src) return direct.currentSrc || direct.src;

    let node = anchor.parentElement;
    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
      const images = node.querySelectorAll?.('img') || [];
      for (const img of images) {
        const src = img.currentSrc || img.src || '';
        if (!src) continue;

        const alt = (img.alt || '').toLowerCase();
        const w = Number(img.width || 0);
        const h = Number(img.height || 0);

        if (
          alt.includes('profile') ||
          alt.includes('photo') ||
          (w >= 20 && h >= 20 && w <= 220 && h <= 220)
        ) {
          return src;
        }
      }
    }

    return '';
  }

  function fullNameNear(anchor, username) {
    let node = anchor.parentElement;

    for (let depth = 0; node && depth < 2; depth++, node = node.parentElement) {
      const text = (node.innerText || '').trim();
      if (!text || text.length > 250) continue;

      for (const line of text.split('\n').map(s => s.trim()).filter(Boolean)) {
        const lowered = line.replace(/^@/, '').toLowerCase();
        if (lowered === username) continue;
        if (/^(follow|following|requested|verified)$/i.test(line)) continue;
        if (line.length <= 100) return line;
      }
    }

    return '';
  }

  function collect(anchor) {
    if (!enabled || !anchor || processed.has(anchor)) return;
    processed.add(anchor);

    const username = usernameFromHref(anchor.href);
    if (!username || seen.has(username)) return;

    queue.push({
      username,
      full_name: fullNameNear(anchor, username),
      profile_url: `https://www.instagram.com/${username}/`,
      image_url: imageNear(anchor),
      source_url: location.href.split('?')[0]
    });

    seen.add(username);
    seenList.push(username);
    save();

    if (queue.length >= BATCH_SIZE) flush();
  }

  function scanNode(node) {
    if (!enabled || !node || node.nodeType !== 1) return;

    if (node.matches?.('a[href]')) collect(node);

    const links = node.querySelectorAll?.('a[href]');
    if (links) {
      for (const anchor of links) collect(anchor);
    }
  }

  function initialScan() {
    for (const anchor of document.querySelectorAll('a[href]')) collect(anchor);
  }

  async function flush() {
    if (!enabled || flushing || !queue.length) return;

    flushing = true;
    updateUI('sending');

    const batch = queue.slice(0, BATCH_SIZE);

    try {
      const response = await fetch(`${API_BASE}/api/profiles/batch`, {
        method: 'POST',
        mode: 'cors',
        credentials: 'omit',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          source_url: location.href.split('?')[0],
          records: batch
        })
      });

      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      queue.splice(0, batch.length);
      sent += batch.length;
      save();

      if (queue.length) setTimeout(flush, 40);
    } catch (error) {
      updateUI(`retry ${String(error.message || error).slice(0, 30)}`);
    } finally {
      flushing = false;
    }
  }

  function start() {
    if (!observer) {
      observer = new MutationObserver(records => {
        if (!enabled) return;

        for (const mutation of records) {
          for (const node of mutation.addedNodes) {
            scanNode(node);
          }
        }
      });

      observer.observe(document.documentElement, {
        childList: true,
        subtree: true
      });
    }

    initialScan();

    clearInterval(timer);
    timer = setInterval(flush, FLUSH_MS);
  }

  function stop() {
    observer?.disconnect();
    observer = null;
    clearInterval(timer);
    timer = null;
  }

  function setEnabled(value) {
    enabled = !!value;
    localStorage.setItem(K_ENABLED, enabled ? '1' : '0');

    if (enabled) start();
    else stop();

    updateUI();
  }

  function reset() {
    queue = [];
    seenList = [];
    seen = new Set();
    sent = 0;

    localStorage.removeItem(K_QUEUE);
    localStorage.removeItem(K_SEEN);
    localStorage.setItem(K_SENT, '0');

    updateUI('reset');
  }

  function updateUI(extra = '') {
    const box = document.getElementById('__profile_gallery_scraper');
    if (!box) return;

    box.querySelector('.dot').style.background = enabled ? '#2ecc71' : '#888';
    box.querySelector('.state').textContent = enabled ? 'SCRAPER ON' : 'SCRAPER OFF';
    box.querySelector('.meta').textContent =
      `queued ${queue.length} · sent ${sent}${extra ? ' · ' + extra : ''}`;
    box.querySelector('[data-toggle]').textContent = enabled ? 'Turn Off' : 'Turn On';
  }

  function createUI() {
    if (document.getElementById('__profile_gallery_scraper')) return;

    const box = document.createElement('div');
    box.id = '__profile_gallery_scraper';
    box.innerHTML = `
      <div class="line"><span class="dot"></span><strong class="state">SCRAPER OFF</strong></div>
      <div class="meta">queued ${queue.length} · sent ${sent}</div>
      <div class="buttons">
        <button data-toggle>Turn On</button>
        <button data-flush>Send Now</button>
        <button data-reset>Reset</button>
      </div>
    `;

    Object.assign(box.style, {
      position: 'fixed',
      right: '10px',
      bottom: '18px',
      zIndex: '2147483647',
      background: 'rgba(20,20,20,.95)',
      color: '#fff',
      padding: '10px 12px',
      borderRadius: '14px',
      font: '12px -apple-system,BlinkMacSystemFont,sans-serif',
      boxShadow: '0 4px 20px rgba(0,0,0,.35)',
      minWidth: '200px'
    });

    const style = document.createElement('style');
    style.textContent = `
      #__profile_gallery_scraper .line{display:flex;align-items:center;gap:7px;margin-bottom:4px}
      #__profile_gallery_scraper .dot{width:9px;height:9px;border-radius:50%;display:inline-block}
      #__profile_gallery_scraper .meta{opacity:.8;margin-bottom:7px}
      #__profile_gallery_scraper .buttons{display:flex;gap:5px}
      #__profile_gallery_scraper button{font:inherit;border:0;border-radius:8px;padding:6px 8px;background:#fff;color:#111}
    `;

    document.documentElement.appendChild(style);
    document.documentElement.appendChild(box);

    box.querySelector('[data-toggle]').onclick = () => setEnabled(!enabled);
    box.querySelector('[data-flush]').onclick = () => flush();
    box.querySelector('[data-reset]').onclick = () => {
      if (confirm('Reset local scraper queue and seen history?')) reset();
    };

    updateUI();
  }

  createUI();
  if (enabled) start();
})();
