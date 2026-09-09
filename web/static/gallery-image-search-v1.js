document.addEventListener("DOMContentLoaded", () => {
  const $ = id => document.getElementById(id);
  const PAGE_SIZE = 250;

  let offset = 0;
  let currentRows = [];
  let loading = false;
  let similarityMode = false;

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

  $("themeToggle").onclick = () => {
    applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  };

  function avatarSrc(profile) {
    return profile.image_url || "";
  }

  function proxySrc(profile) {
    if (!profile.image_url) return "";
    return `/api/avatar?url=${encodeURIComponent(profile.image_url)}`;
  }

  function card(profile) {
    const username = esc(profile.username);
    const url = esc(profile.profile_url);
    const name = esc(profile.full_name || "");
    const img = avatarSrc(profile);
    const proxy = proxySrc(profile);
    const score = profile.score == null
      ? ""
      : `<div class="score">CLIP ${Number(profile.score).toFixed(4)}</div>`;

    const visual = img
      ? `<a class="photo-link" href="${url}" target="_blank" rel="noopener">
           <img src="${esc(img)}" data-proxy="${esc(proxy)}" loading="lazy" decoding="async" referrerpolicy="no-referrer"
             onerror="if(!this.dataset.triedProxy&&this.dataset.proxy){this.dataset.triedProxy='1';this.src=this.dataset.proxy}else{this.style.display='none';this.closest('.photo-link').classList.add('silent-placeholder')}">
           ${score}
         </a>`
      : `<a class="photo-link silent-placeholder" href="${url}" target="_blank" rel="noopener">${score}</a>`;

    return `<article class="profile-card">
      ${visual}
      <div class="profile-meta">
        <a class="username" href="${url}" target="_blank" rel="noopener">@${username}</a>
        ${name ? `<div class="full-name">${name}</div>` : ""}
      </div>
    </article>`;
  }

  function sortSimilarity() {
    if (!similarityMode) return;
    const asc = $("scoreSort").value === "asc";
    currentRows.sort((a,b) => asc
      ? Number(a.score ?? 0) - Number(b.score ?? 0)
      : Number(b.score ?? 0) - Number(a.score ?? 0));
  }

  function render() {
    const q = $("filter").value.trim().toLowerCase();

    const rows = q
      ? currentRows.filter(p =>
          String(p.username || "").toLowerCase().includes(q) ||
          String(p.full_name || "").toLowerCase().includes(q))
      : currentRows;

    $("gallery").innerHTML = rows.length
      ? rows.map(card).join("")
      : `<div class="empty">No profiles to display.</div>`;
  }

  async function refreshStats() {
    try {
      const d = await api("/api/stats");
      $("total").textContent = Number(d.total || 0).toLocaleString();
      $("photos").textContent = Number(d.with_photos || 0).toLocaleString();

      const s = await api("/api/search/status");
      $("indexed").textContent = Number(s.indexed || 0).toLocaleString();
    } catch (e) {
      $("status").textContent = e.message;
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

      await refreshStats();
    } catch (e) {
      $("status").textContent = e.message;
    } finally {
      loading = false;
    }
  }

  async function searchByImage() {
    const file = $("queryImage").files?.[0];

    if (!file) {
      $("imageSearchStatus").textContent = "Choose an image first.";
      return;
    }

    $("imageSearch").disabled = true;
    $("imageSearchStatus").textContent = "Embedding query image and sorting searchable profiles…";

    try {
      const form = new FormData();
      form.append("image", file);

      const d = await api("/api/search/image?limit=10000", {
        method: "POST",
        body: form
      });

      currentRows = d.profiles || [];
      similarityMode = true;
      offset = 0;

      sortSimilarity();
      render();

      $("more").style.display = "none";
      $("status").textContent = `${currentRows.length.toLocaleString()} photos sorted by image similarity`;
      $("imageSearchStatus").textContent =
        `Compared against ${Number(d.indexed_searched || 0).toLocaleString()} indexed profile photos.`;
    } catch (e) {
      $("imageSearchStatus").textContent = e.message;
    } finally {
      $("imageSearch").disabled = false;
    }
  }

  $("imageSearch").onclick = searchByImage;
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
    if (!confirm("Delete every scraped profile?")) return;

    try {
      await api("/api/profiles", {method:"DELETE"});
      currentRows = [];
      offset = 0;
      render();
      await refreshStats();
      $("status").textContent = "Gallery cleared.";
    } catch (e) {
      $("status").textContent = e.message;
    }
  };

  loadRecent(true);
  refreshStats();
  setInterval(refreshStats, 3000);
});
