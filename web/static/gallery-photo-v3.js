document.addEventListener("DOMContentLoaded", () => {
  const savedTheme = localStorage.getItem("gallery_theme") || "light";
  document.documentElement.dataset.theme = savedTheme;

  const themeButton = document.getElementById("themeToggle");
  if (themeButton) {
    themeButton.textContent = savedTheme === "light" ? "Dark theme" : "Light theme";
    themeButton.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("gallery_theme", next);
      themeButton.textContent = next === "light" ? "Dark theme" : "Light theme";
    });
  }

  const $ = id => document.getElementById(id);
  const PAGE_SIZE = 250;
  let offset = 0;
  let loading = false;
  let loadedProfiles = [];

  function esc(v) {
    return String(v ?? "").replace(/[&<>"']/g, c => ({
      "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    }[c]));
  }

  async function api(path, options={}) {
    const r = await fetch(path, options);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }

  function avatarSrc(p) {
    if (p.avatar_base64) {
      return `data:image/jpeg;base64,${p.avatar_base64}`;
    }
    if (p.image_url) {
      return `/api/avatar?url=${encodeURIComponent(p.image_url)}`;
    }
    return "";
  }

  function card(p) {
    const username = esc(p.username);
    const url = esc(p.profile_url);
    const name = esc(p.full_name || "");
    const img = avatarSrc(p);

    const visual = img
      ? `<a class="photo-link" href="${url}" target="_blank" rel="noopener">
           <img src="${esc(img)}" loading="lazy" decoding="async"
                onerror="this.closest('.photo-link').classList.add('broken');this.remove();">
         </a>`
      : `<a class="photo-link placeholder" href="${url}" target="_blank" rel="noopener">
           <span>@${username}</span>
         </a>`;

    return `<article class="profile-card">
      ${visual}
      <div class="profile-meta">
        <a class="username" href="${url}" target="_blank" rel="noopener">@${username}</a>
        ${name ? `<div class="full-name">${name}</div>` : ""}
      </div>
    </article>`;
  }

  function render() {
    const q = $("search").value.trim().toLowerCase();
    const rows = q
      ? loadedProfiles.filter(p =>
          String(p.username || "").toLowerCase().includes(q) ||
          String(p.full_name || "").toLowerCase().includes(q)
        )
      : loadedProfiles;

    $("gallery").innerHTML = rows.length
      ? rows.map(card).join("")
      : `<div class="empty">No profiles loaded yet.</div>`;
  }

  async function stats() {
    try {
      const d = await api("/api/stats");
      $("total").textContent = Number(d.total || 0).toLocaleString();
      $("photos").textContent = Number(d.with_photos || 0).toLocaleString();
    } catch (e) {
      $("status").textContent = e.message;
    }
  }

  async function load(reset=false) {
    if (loading) return;
    loading = true;
    try {
      if (reset) {
        offset = 0;
        loadedProfiles = [];
      }

      const photosOnly = $("photosOnly").checked ? "true" : "false";
      const d = await api(`/api/profiles?limit=${PAGE_SIZE}&offset=${offset}&photos_only=${photosOnly}`);
      const rows = d.profiles || [];

      loadedProfiles.push(...rows);
      offset += rows.length;

      render();
      $("more").disabled = rows.length < PAGE_SIZE;
      $("status").textContent = `${loadedProfiles.length.toLocaleString()} profiles loaded · photo proxy v3`;
      await stats();
    } catch (e) {
      $("status").textContent = e.message;
    } finally {
      loading = false;
    }
  }

  $("refresh").onclick = () => load(true);
  $("more").onclick = () => load(false);
  $("photosOnly").onchange = () => load(true);
  $("search").oninput = render;

  $("clear").onclick = async () => {
    if (!confirm("Delete every scraped profile from the gallery?")) return;
    try {
      await api("/api/profiles", {method:"DELETE"});
      offset = 0;
      loadedProfiles = [];
      render();
      await stats();
      $("status").textContent = "Gallery cleared.";
    } catch (e) {
      $("status").textContent = e.message;
    }
  };

  load(true);
  setInterval(stats, 3000);
});
