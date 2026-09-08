const $=id=>document.getElementById(id);
const PAGE_SIZE=250;
let offset=0, loading=false, loadedProfiles=[];

function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
async function api(path,options={}){const r=await fetch(path,options);if(!r.ok)throw new Error(await r.text());return r.json();}

function card(p){
  const u=esc(p.username), url=esc(p.profile_url), img=esc(p.image_url||""), name=esc(p.full_name||"");
  const visual=img
    ? `<a class="photo-link" href="${url}" target="_blank" rel="noopener"><img src="${img}" loading="lazy" decoding="async" referrerpolicy="no-referrer" alt="@${u}"></a>`
    : `<a class="photo-link placeholder" href="${url}" target="_blank" rel="noopener"><span>@${u}</span></a>`;
  return `<article class="profile-card">${visual}<div class="profile-meta"><a class="username" href="${url}" target="_blank" rel="noopener">@${u}</a>${name?`<div class="full-name">${name}</div>`:""}</div></article>`;
}

function renderVisible(){
  const q=$("search").value.trim().toLowerCase();
  const rows=q?loadedProfiles.filter(p=>String(p.username||"").toLowerCase().includes(q)||String(p.full_name||"").toLowerCase().includes(q)):loadedProfiles;
  $("gallery").innerHTML=rows.length?rows.map(card).join(""):`<div class="empty">No profiles loaded yet.</div>`;
}

async function refreshStats(){
  try{
    const d=await api("/api/stats");
    $("total").textContent=Number(d.total||0).toLocaleString();
    $("photos").textContent=Number(d.with_photos||0).toLocaleString();
  }catch(e){$("status").textContent=e.message;}
}

async function loadProfiles(reset=false){
  if(loading)return;
  loading=true;
  try{
    if(reset){offset=0;loadedProfiles=[];$("gallery").innerHTML="";}
    const photosOnly=$("photosOnly").checked?"true":"false";
    const d=await api(`/api/profiles?limit=${PAGE_SIZE}&offset=${offset}&photos_only=${photosOnly}`);
    const rows=d.profiles||[];
    loadedProfiles.push(...rows);
    offset+=rows.length;
    renderVisible();
    $("more").disabled=rows.length<PAGE_SIZE;
    $("status").textContent=`${loadedProfiles.length.toLocaleString()} profiles loaded into this page`;
    await refreshStats();
  }catch(e){$("status").textContent=e.message;}
  finally{loading=false;}
}

$("refresh").onclick=()=>loadProfiles(true);
$("more").onclick=()=>loadProfiles(false);
$("photosOnly").onchange=()=>loadProfiles(true);
$("search").oninput=renderVisible;
$("clear").onclick=async()=>{
  if(!confirm("Delete every scraped profile from the gallery? This cannot be undone."))return;
  $("status").textContent="Clearing profiles…";
  try{
    await api("/api/profiles",{method:"DELETE"});
    loadedProfiles=[];offset=0;renderVisible();
    $("total").textContent="0";$("photos").textContent="0";$("status").textContent="Gallery cleared.";
  }catch(e){$("status").textContent=e.message;}
};

loadProfiles(true);
setInterval(refreshStats,2500);
