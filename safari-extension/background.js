const ext = globalThis.browser || globalThis.chrome;
const QUEUE_KEY = 'liveScanQueue';
const FLUSH_ALARM = 'live-scan-flush';
let flushing = false;

async function cfg() {
  return ext.storage.local.get(['serverUrl', 'deviceId', 'scannerEnabled']);
}

async function req(path, opt = {}) {
  const { serverUrl } = await cfg();
  if (!serverUrl) throw new Error('Render URL not configured');
  const r = await fetch(serverUrl.replace(/\/$/, '') + path, {
    headers: { 'content-type': 'application/json' },
    ...opt,
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function normalizeRecord(r) {
  const username = String(r?.username || '').trim().replace(/^@/, '').toLowerCase();
  if (!username) return null;
  return {
    username,
    full_name: String(r.full_name || '').slice(0, 500),
    profile_url: String(r.profile_url || `https://www.instagram.com/${username}/`).slice(0, 1000),
    image_url: String(r.image_url || '').slice(0, 4000),
    source: String(r.source || 'live_scroll').slice(0, 100),
    post_url: String(r.post_url || '').slice(0, 1000),
    observed_at: r.observed_at || Date.now(),
  };
}

function mergeBetter(a, b) {
  return {
    ...a,
    ...b,
    full_name: (b.full_name || '').length > (a.full_name || '').length ? b.full_name : a.full_name,
    image_url: b.image_url || a.image_url,
    post_url: b.post_url || a.post_url,
  };
}

async function enqueue(records) {
  const old = (await ext.storage.local.get(QUEUE_KEY))[QUEUE_KEY] || [];
  const map = new Map(old.map(r => [r.username, r]));
  for (const raw of records || []) {
    const r = normalizeRecord(raw);
    if (!r) continue;
    map.set(r.username, map.has(r.username) ? mergeBetter(map.get(r.username), r) : r);
  }
  // Keep the queue bounded. Oldest entries are retained first until successfully uploaded.
  const queue = [...map.values()].slice(0, 3000);
  await ext.storage.local.set({ [QUEUE_KEY]: queue });
  if (queue.length >= 20) flushQueue();
  await updateBadge(queue.length);
  return queue.length;
}

async function updateBadge(queued = null) {
  try {
    const { scannerEnabled } = await cfg();
    if (queued == null) queued = ((await ext.storage.local.get(QUEUE_KEY))[QUEUE_KEY] || []).length;
    await ext.action.setBadgeText({ text: scannerEnabled ? (queued ? String(Math.min(queued, 999)) : 'ON') : '' });
  } catch (_) {}
}

async function flushQueue() {
  if (flushing) return;
  flushing = true;
  try {
    const c = await cfg();
    if (!c.serverUrl || !c.deviceId) return;
    let queue = (await ext.storage.local.get(QUEUE_KEY))[QUEUE_KEY] || [];
    while (queue.length) {
      const batch = queue.slice(0, 50);
      try {
        await req('/api/live/ingest', {
          method: 'POST',
          body: JSON.stringify({
            device_id: c.deviceId,
            records: batch,
            page_url: batch[batch.length - 1]?.post_url || null,
            message: `Safari uploaded ${batch.length} queued profiles`,
          }),
        });
        queue = queue.slice(batch.length);
        await ext.storage.local.set({ [QUEUE_KEY]: queue, lastUploadAt: Date.now(), lastUploadError: '' });
      } catch (e) {
        await ext.storage.local.set({ lastUploadError: String(e?.message || e) });
        break;
      }
    }
    await updateBadge(queue.length);
  } finally {
    flushing = false;
  }
}

async function broadcastState() {
  const c = await cfg();
  const tabs = await ext.tabs.query({ url: 'https://www.instagram.com/*' });
  for (const tab of tabs) {
    if (tab.id == null) continue;
    ext.tabs.sendMessage(tab.id, { type: 'scanner-state', enabled: !!c.scannerEnabled }).catch(() => {});
  }
}

async function setScanner(enabled, pageUrl = null) {
  const c = await cfg();
  await ext.storage.local.set({ scannerEnabled: !!enabled });
  if (c.serverUrl && c.deviceId) {
    try {
      await req('/api/live/state', {
        method: 'POST',
        body: JSON.stringify({ device_id: c.deviceId, enabled: !!enabled, page_url: pageUrl }),
      });
    } catch (_) {}
  }
  await broadcastState();
  await updateBadge();
  if (!enabled) await flushQueue();
  return { enabled: !!enabled };
}

// Existing automated-job bridge remains available.
let runningJob = null;
let workerTabs = new Map();
async function pollJobs() {
  try {
    const { deviceId } = await cfg();
    if (!deviceId) return;
    const d = await req('/api/extension/active?device_id=' + encodeURIComponent(deviceId));
    if (!d.job || runningJob === d.job.id) return;
    runningJob = d.job.id;
    await req(`/api/jobs/${d.job.id}/claim`, { method: 'POST', body: JSON.stringify({ device_id: deviceId }) });
    workerTabs.clear();
    for (let i = 0; i < d.job.browser_windows; i++) {
      const t = await ext.tabs.create({ url: d.job.url, active: i === 0 });
      workerTabs.set(t.id, i);
      setTimeout(() => ext.tabs.sendMessage(t.id, { type: 'start-worker', job: d.job, worker: i }).catch(() => {}), 1800 + i * 250);
    }
  } catch (e) { console.warn(e); }
}

ext.runtime.onInstalled.addListener(async () => {
  const v = await ext.storage.local.get(['scannerEnabled']);
  if (typeof v.scannerEnabled !== 'boolean') await ext.storage.local.set({ scannerEnabled: false, [QUEUE_KEY]: [] });
  ext.alarms.create(FLUSH_ALARM, { periodInMinutes: 1 });
  ext.alarms.create('job-poll', { periodInMinutes: 1 });
  updateBadge();
});

ext.alarms.onAlarm.addListener(a => {
  if (a.name === FLUSH_ALARM) flushQueue();
  if (a.name === 'job-poll') pollJobs();
});

ext.tabs.onUpdated?.addListener((tabId, info, tab) => {
  if (info.status === 'complete' && /^https:\/\/www\.instagram\.com\//.test(tab.url || '')) {
    cfg().then(c => ext.tabs.sendMessage(tabId, { type: 'scanner-state', enabled: !!c.scannerEnabled }).catch(() => {}));
  }
});

ext.runtime.onMessage.addListener((m, s) => {
  if (m.type === 'live-candidates') {
    return enqueue(m.records || []).then(async queued => {
      if (m.flush) await flushQueue();
      return { ok: true, queued };
    });
  }
  if (m.type === 'set-scanner') return setScanner(!!m.enabled, m.pageUrl || s.tab?.url || null);
  if (m.type === 'get-scanner-status') {
    return Promise.all([cfg(), ext.storage.local.get([QUEUE_KEY, 'lastUploadAt', 'lastUploadError'])]).then(([c, q]) => ({
      enabled: !!c.scannerEnabled,
      queued: (q[QUEUE_KEY] || []).length,
      lastUploadAt: q.lastUploadAt || null,
      lastUploadError: q.lastUploadError || '',
    }));
  }
  if (m.type === 'flush-live') return flushQueue().then(() => ({ ok: true }));
  if (m.type === 'poll') return pollJobs();
  if (m.type === 'worker-finished' && s.tab?.id != null) {
    workerTabs.delete(s.tab.id);
    if (workerTabs.size === 0) runningJob = null;
  }
});

pollJobs();
updateBadge();
