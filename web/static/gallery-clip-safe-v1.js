document.addEventListener("DOMContentLoaded", () => {
  const $ = id => document.getElementById(id);
  const PAGE_SIZE = 250;

  let offset = 0;
  let currentRows = [];
  let loading = false;
  let similarityMode = false;

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({
      "&":"&amp;",
      "<":"&lt;",
      ">":"&gt;",
      '"':"&quot;",
      "'":"&#39;"
    }[c]));
  }

  async function api(path, options={}) {
    const r = await fetch(path, options);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("gallery_theme", theme);
    $("themeToggle").textContent = theme === "light" ? "Dark theme" : "Light theme";
  }

  applyTheme(localStorage.getItem("gallery_theme") || "light");

  $("themeToggle").onclick = () => {
    applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  };

  function avatarSrc(profile) {
    if (!profile.image_url) return "";
    return `/api/avatar?url=${encodeURIComponent(profile.image_url)}`;
  }

  function card(profile) {
    const username = esc(profile.username);
    const profileUrl = esc(profile.profile_url);
    const fullName = esc(profile.full_name || "");
    const image = avatarSrc(profile);
    const score = profile.score == null
      ? ""
      : `<div class="score">CLIP ${Number(profile.score).toFixed(4)}</div>`;

    const visual = image
      ? `<a class="photo-link" href="${profileUrl}" target="_blank" rel="noopener">
           <img src="${esc(image)}" loading="lazy" decoding="async"
             onerror="this.style.display='none';this.closest('.photo-link').classList.add('silent-placeholder')">
           ${score}
         </a>`
      : `<a class="photo-link silent-placeholder" href="${profileUrl}" target="_blank" rel="noopener">${score}</a>`;

    return `<article class="profile-card">
      ${visual}
      <div class="profile-meta">
        <a class="username" href="${profileUrl}" target="_blank" rel="noopener">@${username}</a>
        ${fullName ? `<div class="full-name">${fullName}</div>` : ""}
      </div>
    </article>`;
  }

  function sortSimilarity() {
    if (!similarityMode) return;
    const ascending = $("scoreSort").value === "asc";
    currentRows.sort((a,b) => ascending
      ? Number(a.score ?? 0) - Number(b.score ?? 0)
      : Number(b.score ?? 0) - Number(a.score ?? 0));
  }

  function render() {
    const filter = $("filter").value.trim().toLowerCase();

    const rows = filter
      ? currentRows.filter(p =>
          String(p.username || "").toLowerCase().includes(filter) ||
          String(p.full_name || "").toLowerCase().includes(filter))
      : currentRows;

    $("gallery").innerHTML = rows.length
      ? rows.map(card).join("")
      : `<div class="empty">No profiles to display.</div>`;
  }

  async function refreshBaseStats() {
    try {
      const d = await api("/api/stats");
      $("total").textContent = Number(d.total || 0).toLocaleString();
      $("photos").textContent = Number(d.with_photos || 0).toLocaleString();
    } catch (e) {
      $("status").textContent = e.message;
    }
  }

  async function refreshClipStats() {
    try {
      const d = await api("/api/clip/stats");

      const total = Number(d.total || 0);
      const indexed = Number(d.indexed || 0);
      const queued = Number(d.queued || 0);
      const failed = Number(d.failed || 0);
      const processing = Number(d.processing || 0);
      const rate = Number(d.images_per_second || 0);
      const pct = total ? Math.round(indexed / total * 100) : 0;

      $("clipIndexed").textContent = indexed.toLocaleString();
      $("clipProgressBar").style.width = `${pct}%`;
      $("clipProgressText").textContent =
        `${d.running ? "INDEXING" : "IDLE"} · ${indexed.toLocaleString()} indexed · ${queued.toLocaleString()} queued · ${processing.toLocaleString()} processing · ${failed.toLocaleString()} failed · ${pct}%${rate ? ` · ${rate.toFixed(1)} images/s` : ""}`;

      $("startClip").disabled = !!d.running;
      $("stopClip").disabled = !d.running;

      if (d.last_error) {
        $("clipStatus").textContent = `Last error: ${d.last_error}`;
      } else {
        $("clipStatus").textContent = "";
      }
    } catch (e) {
      $("clipStatus").textContent = e.message;
    }
  }

  async function loadRecent(reset=false) {
    if (loading) return;
    loading = true;

    try {
      similarityMode = false;

      if (reset) {
        offset = 0;
        currentRows = [];
      }

      const photosOnly = $("photosOnly").checked ? "true" : "false";
      const d = await api(`/api/profiles?limit=${PAGE_SIZE}&offset=${offset}&photos_only=${photosOnly}`);
      const rows = d.profiles || [];

      currentRows.push(...rows);
      offset += rows.length;

      render();

      $("more").style.display = "";
      $("more").disabled = rows.length < PAGE_SIZE;
      $("status").textContent = `${currentRows.length.toLocaleString()} recent profiles loaded`;

      await refreshBaseStats();
    } catch (e) {
      $("status").textContent = e.message;
    } finally {
      loading = false;
    }
  }

  async function runTextSearch() {
    const q = $("clipQuery").value.trim();

    if (!q) {
      $("clipStatus").textContent = "Type an image description first.";
      return;
    }

    $("clipSearch").disabled = true;
    $("clipStatus").textContent = "Embedding text and sorting indexed profile photos…";

    try {
      const d = await api(`/api/clip/search/text?q=${encodeURIComponent(q)}&limit=10000`);
      currentRows = d.profiles || [];
      similarityMode = true;
      offset = 0;

      sortSimilarity();
      render();

      $("more").style.display = "none";
      $("status").textContent = `${currentRows.length.toLocaleString()} photos sorted by CLIP similarity`;
      $("clipStatus").textContent =
        `Searched ${Number(d.indexed_searched || 0).toLocaleString()} indexed photos for “${q}”.`;
    } catch (e) {
      $("clipStatus").textContent = e.message;
    } finally {
      $("clipSearch").disabled = false;
    }
  }

  $("startClip").onclick = async () => {
    $("clipStatus").textContent = "Starting CLIP engine… first start may download/load the model.";
    try {
      await api("/api/clip/start", {method:"POST"});
      await refreshClipStats();
    } catch (e) {
      $("clipStatus").textContent = e.message;
    }
  };

  $("stopClip").onclick = async () => {
    try {
      await api("/api/clip/stop", {method:"POST"});
      $("clipStatus").textContent = "Stopping after current work finishes…";
    } catch (e) {
      $("clipStatus").textContent = e.message;
    }
  };

  $("reindexClip").onclick = async () => {
    if (!confirm("Reset all CLIP embeddings and queue every profile photo again?")) return;

    try {
      await api("/api/clip/reindex", {method:"POST"});
      $("clipStatus").textContent = "All photos queued for a fresh CLIP index.";
      await refreshClipStats();
    } catch (e) {
      $("clipStatus").textContent = e.message;
    }
  };

  $("clipSearch").onclick = runTextSearch;
  $("clipQuery").addEventListener("keydown", e => {
    if (e.key === "Enter") runTextSearch();
  });

  $("scoreSort").onchange = () => {
    sortSimilarity();
    render();
  };

  $("showRecent").onclick = () => loadRecent(true);
  $("refresh").onclick = () => loadRecent(true);
  $("more").onclick = () => loadRecent(false);
  $("photosOnly").onchange = () => loadRecent(true);
  $("filter").oninput = render;

  $("clear").onclick = async () => {
    if (!confirm("Delete every scraped profile and CLIP embedding?")) return;

    try {
      await api("/api/profiles", {method:"DELETE"});
      currentRows = [];
      offset = 0;
      render();
      await refreshBaseStats();
      await refreshClipStats();
      $("status").textContent = "Gallery cleared.";
    } catch (e) {
      $("status").textContent = e.message;
    }
  };

  loadRecent(true);
  refreshClipStats();
  setInterval(refreshBaseStats, 3000);
  setInterval(refreshClipStats, 2500);
});
