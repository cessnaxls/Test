document.addEventListener("DOMContentLoaded", () => {
  const byId = id => document.getElementById(id);

  const REQUIRED = [
    "total","photos","search","photosOnly","refresh",
    "clear","status","gallery","more"
  ];

  for (const id of REQUIRED) {
    if (!byId(id)) {
      console.error(`Gallery UI missing #${id}`);
      return;
    }
  }

  const PAGE_SIZE = 250;
  let offset = 0;
  let loading = false;
  let loadedProfiles = [];

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

  function imageSource(profile) {
    if (profile.avatar_base64) {
      return `data:image/jpeg;base64,${profile.avatar_base64}`;
    }
    return profile.image_url || "";
  }

  function profileCard(profile) {
    const username = esc(profile.username);
    const profileUrl = esc(profile.profile_url);
    const fullName = esc(profile.full_name || "");
    const image = imageSource(profile);

    const visual = image
      ? `<a class="photo-link" href="${profileUrl}" target="_blank" rel="noopener">
           <img src="${esc(image)}" loading="lazy" decoding="async" alt="@${username}">
         </a>`
      : `<a class="photo-link placeholder" href="${profileUrl}" target="_blank" rel="noopener">
           <span>@${username}</span>
         </a>`;

    return `<article class="profile-card">
      ${visual}
      <div class="profile-meta">
        <a class="username" href="${profileUrl}" target="_blank" rel="noopener">@${username}</a>
        ${fullName ? `<div class="full-name">${fullName}</div>` : ""}
      </div>
    </article>`;
  }

  function renderVisible() {
    const q = byId("search").value.trim().toLowerCase();

    const rows = q
      ? loadedProfiles.filter(p =>
          String(p.username || "").toLowerCase().includes(q) ||
          String(p.full_name || "").toLowerCase().includes(q)
        )
      : loadedProfiles;

    byId("gallery").innerHTML = rows.length
      ? rows.map(profileCard).join("")
      : `<div class="empty">No profiles loaded yet.</div>`;
  }

  async function refreshStats() {
    try {
      const data = await api("/api/stats");
      byId("total").textContent = Number(data.total || 0).toLocaleString();
      byId("photos").textContent = Number(data.with_photos || 0).toLocaleString();
    } catch (error) {
      byId("status").textContent = error.message;
    }
  }

  async function loadProfiles(reset=false) {
    if (loading) return;
    loading = true;

    try {
      if (reset) {
        offset = 0;
        loadedProfiles = [];
        byId("gallery").innerHTML = "";
      }

      const photosOnly = byId("photosOnly").checked ? "true" : "false";
      const data = await api(
        `/api/profiles?limit=${PAGE_SIZE}&offset=${offset}&photos_only=${photosOnly}`
      );

      const rows = data.profiles || [];
      loadedProfiles.push(...rows);
      offset += rows.length;

      renderVisible();

      byId("more").disabled = rows.length < PAGE_SIZE;
      byId("status").textContent =
        `${loadedProfiles.length.toLocaleString()} profiles loaded · Photo build v2`;

      await refreshStats();
    } catch (error) {
      byId("status").textContent = error.message;
    } finally {
      loading = false;
    }
  }

  byId("refresh").addEventListener("click", () => loadProfiles(true));
  byId("more").addEventListener("click", () => loadProfiles(false));
  byId("photosOnly").addEventListener("change", () => loadProfiles(true));
  byId("search").addEventListener("input", renderVisible);

  byId("clear").addEventListener("click", async () => {
    if (!confirm("Delete every scraped profile from the gallery? This cannot be undone.")) return;

    byId("status").textContent = "Clearing profiles…";

    try {
      await api("/api/profiles", {method:"DELETE"});
      loadedProfiles = [];
      offset = 0;
      renderVisible();
      byId("total").textContent = "0";
      byId("photos").textContent = "0";
      byId("status").textContent = "Gallery cleared.";
    } catch (error) {
      byId("status").textContent = error.message;
    }
  });

  loadProfiles(true);
  setInterval(refreshStats, 2500);
});
