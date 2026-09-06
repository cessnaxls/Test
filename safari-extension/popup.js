const api = globalThis.browser || globalThis.chrome;
const $ = id => document.getElementById(id);

async function load() {
  const v = await api.storage.local.get(['serverUrl','deviceId','scannerEnabled','liveScanQueue','lastUploadAt','lastUploadError']);
  $('u').value = v.serverUrl || '';
  $('d').value = v.deviceId || '';
  $('scanner').checked = !!v.scannerEnabled;
  renderStatus(v);
}
function renderStatus(v) {
  const on = !!v.scannerEnabled;
  $('stateLabel').textContent = on ? 'Scanner ON' : 'Scanner OFF';
  const queued = (v.liveScanQueue || []).length;
  const uploaded = v.lastUploadAt ? `Last upload ${new Date(v.lastUploadAt).toLocaleTimeString()}.` : 'No upload yet.';
  $('s').textContent = `${queued} queued. ${uploaded}${v.lastUploadError ? ' Error: ' + v.lastUploadError : ''}`;
}
$('save').onclick = async () => {
  const serverUrl = $('u').value.trim().replace(/\/$/, '');
  const deviceId = $('d').value.trim();
  await api.storage.local.set({ serverUrl, deviceId });
  $('s').textContent = 'Connection saved.';
  api.runtime.sendMessage({ type: 'poll' });
};
$('scanner').onchange = async () => {
  const enabled = $('scanner').checked;
  const tabs = await api.tabs.query({ active: true, currentWindow: true });
  await api.runtime.sendMessage({ type: 'set-scanner', enabled, pageUrl: tabs[0]?.url || null });
  const v = await api.storage.local.get(['serverUrl','deviceId','scannerEnabled','liveScanQueue','lastUploadAt','lastUploadError']);
  renderStatus(v);
};
setInterval(load, 1500);
load();
