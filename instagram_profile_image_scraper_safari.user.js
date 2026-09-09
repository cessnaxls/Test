// ==UserScript==
// @name         Instagram Profile Image Scraper → Profile Gallery (Safari)
// @namespace    aeroslate.profile-gallery
// @version      2.1.0
// @description  Captures Instagram profile usernames, names, and profile-image URLs as they appear and sends them to your Render/Supabase profile gallery.
// @match        https://www.instagram.com/*
// @match        https://instagram.com/*
// @run-at       document-idle
// @noframes
// ==/UserScript==

(() => {
  'use strict';

  // Your existing Render app.
  const API_BASE = 'https://instagram-profile-search.onrender.com';
  const INGEST_URL = `${API_BASE}/api/profiles/batch`;

  const BATCH_SIZE = 100;
  const FLUSH_EVERY_MS = 300;
  const RESCAN_EVERY_MS = 2500;
  const MAX_QUEUE = 5000;
  const MAX_COMPLETE = 30000;

  const BLOCKED = new Set([
    'explore','reels','accounts','direct','stories','p','reel','tv','about',
    'developer','legal','privacy','terms','web','emails','challenge','api',
    'directory','push','settings','notifications'
  ]);

  const KEY = {
    enabled: 'pg_safari_enabled_v2',
    queue: 'pg_safari_queue_v2',
    complete: 'pg_safari_complete_v2',
    sent: 'pg_safari_sent_v2'
  };

  let enabled = localStorage.getItem(KEY.enabled) !== '0';
  let queue = loadArray(KEY.queue);
  let completeList = loadArray(KEY.complete);
  let complete = new Set(completeList);
  let sent = Number(localStorage.getItem(KEY.sent) || 0);
  let flushing = false;
  let mutationObserver = null;
  let flushTimer = null;
  let rescanTimer = null;
  let lastHref = location.href;
  let lastStatus = '';
  let indexKicking = false;
  let expanded = localStorage.getItem('pg_safari_expanded_v21') === '1';

  // Keep the newest/best record for a username while waiting to send.
  const pendingByUsername = new Map();
  for (const item of queue) {
    if (item?.username) pendingByUsername.set(item.username, item);
  }

  function loadArray(key) {
    try {
      const value = JSON.parse(localStorage.getItem(key) || '[]');
      return Array.isArray(value) ? value : [];
    } catch (_) {
      return [];
    }
  }

  function persist() {
    queue = Array.from(pendingByUsername.values()).slice(-MAX_QUEUE);
    try {
      localStorage.setItem(KEY.queue, JSON.stringify(queue));
      localStorage.setItem(KEY.complete, JSON.stringify(completeList.slice(-MAX_COMPLETE)));
      localStorage.setItem(KEY.sent, String(sent));
    } catch (_) {}
    paint();
  }

  function cleanSourceUrl() {
    return `${location.origin}${location.pathname}`;
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

  function normalizeImageUrl(value) {
    const src = String(value || '').trim();
    if (!src || src.startsWith('data:') || src.startsWith('blob:')) return '';
    try {
      const u = new URL(src, location.href);
      if (u.protocol !== 'https:') return '';
      return u.href;
    } catch (_) {
      return '';
    }
  }

  function largestFromSrcset(srcset) {
    if (!srcset) return '';
    let bestUrl = '';
    let bestScore = -1;

    for (const raw of srcset.split(',')) {
      const part = raw.trim();
      if (!part) continue;
      const bits = part.split(/\s+/);
      const url = normalizeImageUrl(bits[0]);
      if (!url) continue;

      const descriptor = bits[1] || '';
      let score = 1;
      if (/\d+w$/i.test(descriptor)) score = parseInt(descriptor, 10) || 1;
      else if (/\d+(?:\.\d+)?x$/i.test(descriptor)) score = (parseFloat(descriptor) || 1) * 1000;

      if (score > bestScore) {
        bestScore = score;
        bestUrl = url;
      }
    }
    return bestUrl;
  }

  function urlFromImage(img) {
    if (!img) return '';
    return (
      largestFromSrcset(img.getAttribute('srcset')) ||
      normalizeImageUrl(img.currentSrc) ||
      normalizeImageUrl(img.getAttribute('src')) ||
      normalizeImageUrl(img.src)
    );
  }

  function imageScore(img) {
    if (!img) return -Infinity;

    const src = urlFromImage(img);
    if (!src) return -Infinity;

    const alt = (img.getAttribute('alt') || '').toLowerCase();
    const rect = img.getBoundingClientRect?.() || { width: 0, height: 0 };
    const width = Number(img.naturalWidth || img.width || rect.width || 0);
    const height = Number(img.naturalHeight || img.height || rect.height || 0);

    let score = 0;
    if (alt.includes('profile')) score += 100;
    if (alt.includes('photo') || alt.includes('picture')) score += 50;
    if (Math.abs(width - height) <= Math.max(width, height) * 0.18) score += 35;
    if (width >= 24 && height >= 24) score += 20;
    if (width <= 400 && height <= 400) score += 15;
    if (/cdninstagram|fbcdn|scontent/i.test(src)) score += 20;

    // Huge post/media images are unlikely to be avatars.
    if (width > 500 || height > 500) score -= 100;

    return score;
  }

  function findProfileImage(anchor) {
    const candidates = [];

    // Strongest match: an image actually inside the profile link.
    for (const img of anchor.querySelectorAll?.('img') || []) candidates.push(img);

    // Instagram frequently puts avatar + username in the same row/card but not
    // inside the same anchor, so walk a few ancestors and score nearby images.
    let node = anchor.parentElement;
    for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
      for (const img of node.querySelectorAll?.('img') || []) candidates.push(img);

      // Stop before accidentally searching an enormous page container.
      if ((node.querySelectorAll?.('a[href]')?.length || 0) > 30) break;
    }

    // Nudge Safari to request avatar assets immediately instead of waiting for
    // its native lazy-loading threshold. This only touches nearby candidates.
    for (const img of candidates) {
      try {
        img.loading = 'eager';
        img.fetchPriority = 'high';
      } catch (_) {}
    }

    let best = null;
    let bestScore = -Infinity;
    for (const img of candidates) {
      const score = imageScore(img);
      if (score > bestScore) {
        bestScore = score;
        best = img;
      }
    }

    return bestScore >= 25 ? urlFromImage(best) : '';
  }

  function findFullName(anchor, username) {
    let node = anchor;

    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
      const text = (node.innerText || '').trim();
      if (!text || text.length > 500) continue;

      const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
      for (const line of lines) {
        const lowered = line.replace(/^@/, '').toLowerCase();
        if (lowered === username) continue;
        if (/^(follow|following|requested|verified|message)$/i.test(line)) continue;
        if (/^\d+[,.]?\d*[kmb]?\s+(followers?|following|posts?)$/i.test(line)) continue;
        if (line.length >= 2 && line.length <= 100) return line;
      }
    }
    return '';
  }

  function queueRecord(anchor) {
    if (!enabled || !anchor?.href) return;

    const username = usernameFromHref(anchor.href);
    if (!username) return;

    const imageUrl = findProfileImage(anchor);
    const fullName = findFullName(anchor, username);
    const existing = pendingByUsername.get(username);

    // If a complete record was already successfully sent, only revisit it if
    // this run has a newer pending record to improve. This keeps scrolling fast.
    if (complete.has(username) && !existing) return;

    // Critical Safari/lazy-load behavior: DO NOT permanently mark a profile as
    // complete until we have a real image URL and the server accepts the batch.
    if (!imageUrl && !existing) return;

    const record = {
      username,
      full_name: fullName || existing?.full_name || '',
      profile_url: `https://www.instagram.com/${username}/`,
      image_url: imageUrl || existing?.image_url || '',
      source_url: cleanSourceUrl()
    };

    // This app is specifically for profile images, so skip image-less records.
    if (!record.image_url) return;

    const changed = !existing ||
      existing.image_url !== record.image_url ||
      (!existing.full_name && record.full_name);

    if (changed) {
      pendingByUsername.set(username, record);
      persist();
      if (pendingByUsername.size >= BATCH_SIZE) flush();
    }
  }

  function scan(root = document) {
    if (!enabled || !root) return;

    if (root.nodeType === 1 && root.matches?.('a[href]')) queueRecord(root);
    for (const anchor of root.querySelectorAll?.('a[href]') || []) queueRecord(anchor);
  }

  async function kickIndexer() {
    if (indexKicking) return;
    indexKicking = true;
    try {
      const response = await fetch(`${API_BASE}/api/index/pending?limit=100`, {
        method: 'POST', mode: 'cors', credentials: 'omit', cache: 'no-store'
      });
      if (response.ok) {
        const d = await response.json().catch(() => ({}));
        if (Number(d.indexed || 0) > 0) {
          lastStatus = `indexed +${Number(d.indexed)}`;
          paint();
          // Drain older pending rows in short chunks without slowing scrolling.
          setTimeout(kickIndexer, 450);
        }
      }
    } catch (_) {
      // Ingest should keep working even while the indexer is warming/retrying.
    } finally {
      indexKicking = false;
    }
  }

  async function flush() {
    if (!enabled || flushing || pendingByUsername.size === 0) return;
    flushing = true;
    lastStatus = 'sending';
    paint();

    const batch = Array.from(pendingByUsername.values()).slice(0, BATCH_SIZE);

    try {
      const response = await fetch(INGEST_URL, {
        method: 'POST',
        mode: 'cors',
        credentials: 'omit',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_url: cleanSourceUrl(),
          records: batch
        })
      });

      if (!response.ok) {
        const body = await response.text().catch(() => '');
        throw new Error(`HTTP ${response.status}${body ? `: ${body.slice(0, 80)}` : ''}`);
      }

      const result = await response.json().catch(() => ({}));

      for (const record of batch) {
        pendingByUsername.delete(record.username);
        if (!complete.has(record.username)) {
          complete.add(record.username);
          completeList.push(record.username);
        }
      }

      sent += Number(result.accepted ?? batch.length);
      lastStatus = `sent ${Number(result.with_photos ?? batch.length)} photos`;
      persist();
      kickIndexer();

      if (pendingByUsername.size) setTimeout(flush, 20);
    } catch (error) {
      lastStatus = `retry: ${String(error?.message || error).slice(0, 55)}`;
      paint();
    } finally {
      flushing = false;
    }
  }

  function start() {
    if (!mutationObserver) {
      mutationObserver = new MutationObserver(mutations => {
        if (!enabled) return;
        for (const mutation of mutations) {
          for (const node of mutation.addedNodes) {
            if (node.nodeType === 1) scan(node);
          }

          // src/srcset often changes after the <img> was inserted.
          if (mutation.type === 'attributes') {
            const img = mutation.target;
            const container = img.closest?.('a[href]') || img.parentElement;
            if (container) scan(container);
          }
        }
      });

      mutationObserver.observe(document.documentElement, {
        subtree: true,
        childList: true,
        attributes: true,
        attributeFilter: ['src', 'srcset']
      });
    }

    clearInterval(flushTimer);
    clearInterval(rescanTimer);

    flushTimer = setInterval(flush, FLUSH_EVERY_MS);
    rescanTimer = setInterval(() => {
      if (location.href !== lastHref) {
        lastHref = location.href;
        lastStatus = 'page changed';
      }
      scan(document);
    }, RESCAN_EVERY_MS);

    scan(document);
    setTimeout(() => scan(document), 900);
    setTimeout(() => scan(document), 2500);
  }

  function stop() {
    mutationObserver?.disconnect();
    mutationObserver = null;
    clearInterval(flushTimer);
    clearInterval(rescanTimer);
    flushTimer = null;
    rescanTimer = null;
  }

  function setEnabled(value) {
    enabled = !!value;
    localStorage.setItem(KEY.enabled, enabled ? '1' : '0');
    lastStatus = enabled ? 'running' : 'paused';
    if (enabled) start();
    else stop();
    paint();
  }

  function resetLocalHistory() {
    pendingByUsername.clear();
    complete.clear();
    completeList = [];
    queue = [];
    sent = 0;
    localStorage.removeItem(KEY.queue);
    localStorage.removeItem(KEY.complete);
    localStorage.setItem(KEY.sent, '0');
    lastStatus = 'local history reset';
    paint();
    if (enabled) scan(document);
  }

  async function testServer() {
    lastStatus = 'testing server';
    paint();
    try {
      const response = await fetch(`${API_BASE}/health`, {
        mode: 'cors',
        credentials: 'omit',
        cache: 'no-store'
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      lastStatus = data?.ok ? 'server connected' : 'server replied';
    } catch (error) {
      lastStatus = `server error: ${String(error?.message || error).slice(0, 45)}`;
    }
    paint();
  }

  function paint() {
    const box = document.getElementById('__pg_safari_scraper');
    if (!box) return;

    box.dataset.expanded = expanded ? '1' : '0';
    box.querySelector('[data-dot]').style.background = enabled ? '#34c759' : '#8e8e93';
    box.querySelector('[data-count]').textContent = String(pendingByUsername.size);
    box.querySelector('[data-meta]').textContent =
      `${sent} sent${lastStatus ? ` · ${lastStatus}` : ''}`;
    box.querySelector('[data-toggle]').textContent = enabled ? 'Pause' : 'Start';
  }

  function createUI() {
    if (document.getElementById('__pg_safari_scraper')) return;

    const style = document.createElement('style');
    style.textContent = `
      #__pg_safari_scraper {
        position:fixed; right:8px;
        bottom:calc(max(8px, env(safe-area-inset-bottom)) + 58px);
        z-index:2147483647; box-sizing:border-box; color:#fff;
        font:11px/1.2 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
        -webkit-user-select:none; user-select:none;
      }
      #__pg_safari_scraper .pg-chip {
        margin-left:auto; width:max-content; min-width:54px; height:30px;
        display:flex; align-items:center; justify-content:center; gap:6px;
        padding:0 9px; border-radius:999px; background:rgba(24,24,27,.88);
        box-shadow:0 3px 12px rgba(0,0,0,.25); -webkit-backdrop-filter:blur(12px);
        backdrop-filter:blur(12px); touch-action:manipulation;
      }
      #__pg_safari_scraper .pg-dot { width:7px; height:7px; border-radius:50%; flex:none; }
      #__pg_safari_scraper .pg-count { font-weight:800; min-width:10px; text-align:center; }
      #__pg_safari_scraper .pg-panel {
        display:none; width:190px; margin:0 0 6px auto; padding:8px;
        border-radius:12px; background:rgba(24,24,27,.94); box-shadow:0 8px 24px rgba(0,0,0,.32);
        -webkit-backdrop-filter:blur(16px); backdrop-filter:blur(16px);
      }
      #__pg_safari_scraper[data-expanded="1"] .pg-panel { display:block; }
      #__pg_safari_scraper .pg-meta { opacity:.72; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-bottom:6px; }
      #__pg_safari_scraper .pg-buttons { display:grid; grid-template-columns:1fr 1fr; gap:5px; }
      #__pg_safari_scraper button {
        appearance:none; -webkit-appearance:none; border:0; border-radius:8px;
        min-height:27px; padding:4px 6px; background:#fff; color:#111;
        font:700 10px -apple-system,BlinkMacSystemFont,sans-serif; touch-action:manipulation;
      }
      #__pg_safari_scraper button:active { opacity:.7; }
    `;
    document.documentElement.appendChild(style);

    const box = document.createElement('div');
    box.id = '__pg_safari_scraper';
    box.innerHTML = `
      <div class="pg-panel">
        <div class="pg-meta" data-meta></div>
        <div class="pg-buttons">
          <button type="button" data-toggle></button>
          <button type="button" data-send>Send</button>
          <button type="button" data-index>Index</button>
          <button type="button" data-test>Test</button>
          <button type="button" data-reset>Reset</button>
          <button type="button" data-close>Close</button>
        </div>
      </div>
      <div class="pg-chip" data-chip aria-label="Profile scraper controls">
        <span class="pg-dot" data-dot></span><span class="pg-count" data-count>0</span>
      </div>
    `;
    document.documentElement.appendChild(box);

    const setExpanded = value => {
      expanded = !!value;
      localStorage.setItem('pg_safari_expanded_v21', expanded ? '1' : '0');
      paint();
    };

    box.querySelector('[data-chip]').addEventListener('click', () => setExpanded(!expanded));
    box.querySelector('[data-close]').addEventListener('click', () => setExpanded(false));
    box.querySelector('[data-toggle]').addEventListener('click', () => setEnabled(!enabled));
    box.querySelector('[data-send]').addEventListener('click', flush);
    box.querySelector('[data-index]').addEventListener('click', kickIndexer);
    box.querySelector('[data-test]').addEventListener('click', testServer);
    box.querySelector('[data-reset]').addEventListener('click', () => {
      if (confirm('Reset only this Safari userscript’s local queue and sent-history?')) resetLocalHistory();
    });

    paint();
  }

  createUI();
  if (enabled) {
    start();
    setTimeout(kickIndexer, 1200);
  }
})();
