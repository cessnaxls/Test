document.addEventListener("DOMContentLoaded", () => {
  const $ = id => document.getElementById(id);
  const PAGE_SIZE = 250;

  let offset = 0;
  let loading = false;
  let currentRows = [];
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
    const response = await fetch(path, options);
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

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
                onerror="this.closest('.photo-link').classList.add('broken');this.remove();">
           ${score}
         </a>`
      : `<a class="photo-link placeholder" href="${profileUrl}" target="_blank" rel="noopener">
           <span>@${username}</span>
           ${score}
         </a>`;

    return `<article class="profile-card">
      ${visual}
      <div class="profile-meta">
        <a class="username" href="${profileUrl}" target="_blank" rel="noopener">@${username}</a>
        ${fullName ? `<div class="full-name">${fullName}</div>` : ""}
      </div>
    </article>`;
  }

  function sortSimilarityRows() {
    if (!similarityMode) return;
    const direction = $("scoreSort").value;
    currentRows.sort((a,b) => direction === "asc"
      ? Number(a.score ?? 0) - Number(b.score ?? 0)
      : Number(b.score ?? 0) - Number(a.score ?? 0)
    );
  }

  function render() {
    const filter = $("filter").value.trim().toLowerCase();

    const rows = filter
      ? currentRows.filter(profile =>
          String(profile.username || "").toLowerCase().includes(filter) ||
          String(profile.full_name || "").toLowerCase().includes(filter)
        )
      : currentRows;

    $("gallery").innerHTML = rows.length
      ? rows.map(card).join("")
      : `<div class="empty">No profiles to display.</div>`;
  }

  async function refreshBaseStats() {
    try {
      const data = await api("/api/stats");
      $("total").textContent = Number(data.total || 0).toLocaleString();
      $("photos").textContent = Number(data.with_photos || 0).toLocaleString();
    } catch (error) {
      $("status").textContent = error.message;
    }
  }

  async function refreshClipStats() {
    try {
      const data = await api("/api/clip/stats");
      $("clipIndexed").textContent = Number(data.indexed || 0).toLocaleString();

      const total = Number(data.total || 0);
      const indexed = Number(data.indexed || 0);
      const failed = Number(data.failed || 0);
      const processing = Number(data.processing || 0);
      const pct = total ? Math.round((indexed / total) * 100) : 0;

      $("clipProgressBar").style.width = `${pct}%`;
      const gpuRate = Number(data.gpu_images_per_second || 0);
      $("clipProgressText").textContent =
        `${indexed.toLocaleString()} indexed · ${Number(data.queued || 0).toLocaleString()} queued · ${processing.toLocaleString()} processing · ${failed.toLocaleString()} failed · ${pct}%${gpuRate ? ` · GPU ${gpuRate.toFixed(1)}/s` : ""}`;

      if (data.last_error) {
        $("clipStatus").textContent = `Last error: ${data.last_error}`;
      }
    } catch (error) {
      $("clipStatus").textContent = error.message;
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
      const data = await api(
        `/api/profiles?limit=${PAGE_SIZE}&offset=${offset}&photos_only=${photosOnly}`
      );

      const rows = data.profiles || [];
      currentRows.push(...rows);
      offset += rows.length;

      render();

      $("more").style.display = "";
      $("more").disabled = rows.length < PAGE_SIZE;
      $("status").textContent =
        `${currentRows.length.toLocaleString()} recent profiles loaded`;

      await refreshBaseStats();
    } catch (error) {
      $("status").textContent = error.message;
    } finally {
      loading = false;
    }
  }

  async function runTextSearch() {
    const query = $("clipQuery").value.trim();

    if (!query) {
      $("clipStatus").textContent = "Type a text description first.";
      return;
    }

    $("clipSearch").disabled = true;
    $("clipStatus").textContent =
      "Embedding text and scoring every indexed profile photo…";

    try {
      const data = await api(
        `/api/clip/search/text?q=${encodeURIComponent(query)}&limit=10000`
      );

      similarityMode = true;
      currentRows = data.profiles || [];
      offset = 0;
      sortSimilarityRows();

      render();

      $("more").style.display = "none";
      $("status").textContent =
        `${currentRows.length.toLocaleString()} photos sorted by CLIP similarity`;
      $("clipStatus").textContent =
        `Searched ${Number(data.indexed_searched || 0).toLocaleString()} indexed photos for “${query}”.`;
    } catch (error) {
      $("clipStatus").textContent = error.message;
    } finally {
      $("clipSearch").disabled = false;
    }
  }

  $("clipSearch").onclick = runTextSearch;

  $("clipQuery").addEventListener("keydown", event => {
    if (event.key === "Enter") runTextSearch();
  });

  $("showRecent").onclick = () => {
    offset = 0;
    currentRows = [];
    $("more").style.display = "";
    loadRecent(true);
  };

  $("reindex").onclick = async () => {
    if (!confirm("Rebuild CLIP embeddings for every scraped profile photo?")) return;

    $("clipStatus").textContent = "Resetting CLIP index…";

    try {
      await api("/api/clip/reindex", {method:"POST"});
      $("clipStatus").textContent = "All photos queued for CLIP indexing.";
      await refreshClipStats();
    } catch (error) {
      $("clipStatus").textContent = error.message;
    }
  };

  $("refresh").onclick = () => loadRecent(true);
  $("more").onclick = () => loadRecent(false);
  $("photosOnly").onchange = () => loadRecent(true);
  $("filter").oninput = render;
  $("scoreSort").onchange = () => { sortSimilarityRows(); render(); };

  $("clear").onclick = async () => {
    if (!confirm("Delete every scraped profile and CLIP embedding?")) return;

    try {
      await api("/api/profiles", {method:"DELETE"});
      currentRows = [];
      offset = 0;
      render();
      $("status").textContent = "Gallery cleared.";
      await refreshBaseStats();
      await refreshClipStats();
    } catch (error) {
      $("status").textContent = error.message;
    }
  };

  loadRecent(true);
  refreshClipStats();

  setInterval(refreshBaseStats, 3000);
  setInterval(refreshClipStats, 3000);
});
