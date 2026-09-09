document.addEventListener("DOMContentLoaded", () => {
  const $ = id => document.getElementById(id);

  let currentRows = [];
  let loading = false;
  let similarityMode = false;
  let imageRun = 0;
  let indexing = false;

  const DEFAULT_PHOTO_WORKERS = Number(localStorage.getItem("photo_workers") || 32);
  const DEFAULT_INDEX_WORKERS = Number(localStorage.getItem("index_workers") || 16);

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({
      "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    }[c]));
  }

  async function api(path, options={}) {
    const response = await fetch(path, options);
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("gallery_theme", theme);
    $("themeToggle").textContent = theme === "light" ? "Dark theme" : "Light theme";
  }

  applyTheme(localStorage.getItem("gallery_theme") || "light");
  $("themeToggle").onclick = () => applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");

  function selectSaved(id, value) {
    const el = $(id);
    if ([...el.options].some(o => Number(o.value) === Number(value))) el.value = String(value);
  }
  selectSaved("photoWorkers", DEFAULT_PHOTO_WORKERS);
  selectSaved("indexWorkers", DEFAULT_INDEX_WORKERS);

  function proxySrc(profile) {
    return profile.image_url ? `/api/avatar?url=${encodeURIComponent(profile.image_url)}` : "";
  }

  function card(profile) {
    const username = esc(profile.username);
    const url = esc(profile.profile_url);
    const name = esc(profile.full_name || "");
    const img = profile.image_url || "";
    const proxy = proxySrc(profile);
    const score = profile.score == null ? "" : `<div class="score">CLIP ${Number(profile.score).toFixed(4)}</div>`;
    const visual = img
      ? `<a class="photo-link" href="${url}" target="_blank" rel="noopener">
           <img data-src="${esc(img)}" data-proxy="${esc(proxy)}" alt="@${username}" decoding="async" referrerpolicy="no-referrer">
           ${score}
         </a>`
      : `<a class="photo-link silent-placeholder" href="${url}" target="_blank" rel="noopener">${score}</a>`;
    return `<article class="profile-card">${visual}<div class="profile-meta">
      <a class="username" href="${url}" target="_blank" rel="noopener">@${username}</a>
      ${name ? `<div class="full-name">${name}</div>` : ""}
    </div></article>`;
  }

  function sortSimilarity() {
    if (!similarityMode) return;
    const asc = $("scoreSort").value === "asc";
    currentRows.sort((a,b) => asc ? Number(a.score ?? 0) - Number(b.score ?? 0) : Number(b.score ?? 0) - Number(a.score ?? 0));
  }

  function filteredRows() {
    const q = $("filter").value.trim().toLowerCase();
    return q ? currentRows.filter(p => String(p.username || "").toLowerCase().includes(q) || String(p.full_name || "").toLowerCase().includes(q)) : currentRows;
  }

  function loadOneImage(img, runId) {
    return new Promise(resolve => {
      if (runId !== imageRun || !img.isConnected) return resolve();
      const direct = img.dataset.src;
      const proxy = img.dataset.proxy;
      if (!direct) return resolve();

      let triedProxy = false;
      const done = () => {
        img.onload = null;
        img.onerror = null;
        resolve();
      };
      img.onload = done;
      img.onerror = () => {
        if (!triedProxy && proxy && runId === imageRun) {
          triedProxy = true;
          img.src = proxy;
        } else {
          img.style.display = "none";
          img.closest(".photo-link")?.classList.add("silent-placeholder");
          done();
        }
      };
      img.src = direct;
    });
  }

  async function runImageQueue() {
    const runId = ++imageRun;
    const imgs = [...document.querySelectorAll("#gallery img[data-src]")];
    const total = imgs.length;
    if (!total) return;

    const workerCount = Math.max(1, Math.min(Number($("photoWorkers").value || 32), 64, total));
    let cursor = 0;
    let completed = 0;
    $("photoLoadStatus").textContent = `0/${total.toLocaleString()} photos · ${workerCount} workers`;

    async function worker() {
      while (runId === imageRun) {
        const i = cursor++;
        if (i >= total) break;
        await loadOneImage(imgs[i], runId);
        completed++;
        if (runId === imageRun && (completed % 10 === 0 || completed === total)) {
          $("photoLoadStatus").textContent = `${completed.toLocaleString()}/${total.toLocaleString()} photos · ${workerCount} workers`;
        }
      }
    }
    await Promise.all(Array.from({length: workerCount}, worker));
  }

  function render() {
    const rows = filteredRows();
    $("gallery").innerHTML = rows.length ? rows.map(card).join("") : `<div class="empty">No profiles to display.</div>`;
    runImageQueue();
  }

  async function refreshStats() {
    try {
      const [d, s] = await Promise.all([api("/api/stats"), api("/api/search/status")]);
      $("total").textContent = Number(d.total || 0).toLocaleString();
      $("photos").textContent = Number(d.with_photos || 0).toLocaleString();
      $("indexed").textContent = Number(s.indexed || 0).toLocaleString();
    } catch (e) {
      $("status").textContent = e.message;
    }
  }

  async function loadAllRecent() {
    if (loading) return;
    loading = true;
    $("refresh").disabled = true;
    try {
      similarityMode = false;
      $("status").textContent = "Loading all profiles…";
      const photosOnly = $("photosOnly").checked ? "true" : "false";
      const d = await api(`/api/profiles/all?photos_only=${photosOnly}`);
      currentRows = d.profiles || [];
      render();
      $("status").textContent = `${currentRows.length.toLocaleString()} profiles loaded — no pagination`;
      await refreshStats();
    } catch (e) {
      $("status").textContent = e.message;
    } finally {
      loading = false;
      $("refresh").disabled = false;
    }
  }

  async function indexPending() {
    if (indexing) return;
    indexing = true;
    $("indexNow").disabled = true;
    const workers = Math.max(1, Math.min(Number($("indexWorkers").value || 16), 64));
    let totalIndexed = 0;
    let totalFailed = 0;
    let rounds = 0;
    try {
      while (rounds < 100) {
        rounds++;
        $("indexStatus").textContent = `Indexing… ${workers} workers · ${totalIndexed.toLocaleString()} done`;
        const d = await api(`/api/index/pending?limit=250&workers=${workers}`, {method:"POST"});
        totalIndexed += Number(d.indexed || 0);
        totalFailed += Number(d.failed || 0);
        await refreshStats();
        if (Number(d.pending || 0) === 0) break;
        if (Number(d.indexed || 0) === 0) break; // avoids looping forever on CDN failures
        if (Number(d.pending || 0) < 250) break;
      }
      $("indexStatus").textContent = `Indexed ${totalIndexed.toLocaleString()}${totalFailed ? ` · ${totalFailed.toLocaleString()} failed` : ""}`;
    } catch (e) {
      $("indexStatus").textContent = e.message;
    } finally {
      indexing = false;
      $("indexNow").disabled = false;
    }
  }

  async function searchByImage() {
    const file = $("queryImage").files?.[0];
    if (!file) return void ($("imageSearchStatus").textContent = "Choose an image first.");
    $("imageSearch").disabled = true;
    $("imageSearchStatus").textContent = "Embedding query image and sorting searchable profiles…";
    try {
      const form = new FormData();
      form.append("image", file);
      const d = await api("/api/search/image?limit=10000", {method:"POST", body:form});
      currentRows = d.profiles || [];
      similarityMode = true;
      sortSimilarity();
      render();
      $("status").textContent = `${currentRows.length.toLocaleString()} photos sorted by image similarity`;
      $("imageSearchStatus").textContent = `Compared against ${Number(d.indexed_searched || 0).toLocaleString()} indexed profile photos.`;
    } catch (e) {
      $("imageSearchStatus").textContent = e.message;
    } finally {
      $("imageSearch").disabled = false;
    }
  }

  $("imageSearch").onclick = searchByImage;
  $("scoreSort").onchange = () => { sortSimilarity(); render(); };
  $("showRecent").onclick = loadAllRecent;
  $("refresh").onclick = loadAllRecent;
  $("photosOnly").onchange = loadAllRecent;
  $("filter").oninput = render;
  $("photoWorkers").onchange = () => {
    localStorage.setItem("photo_workers", $("photoWorkers").value);
    runImageQueue();
  };
  $("indexWorkers").onchange = () => localStorage.setItem("index_workers", $("indexWorkers").value);
  $("indexNow").onclick = indexPending;

  $("clear").onclick = async () => {
    if (!confirm("Delete every scraped profile?")) return;
    try {
      await api("/api/profiles", {method:"DELETE"});
      currentRows = [];
      render();
      await refreshStats();
      $("status").textContent = "Gallery cleared.";
    } catch (e) { $("status").textContent = e.message; }
  };

  loadAllRecent();
  refreshStats();
  setInterval(refreshStats, 5000);
});
