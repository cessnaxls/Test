/* Instagram Profile Collector for iOS Shortcuts — Run JavaScript on Web Page */
(() => {
  const BLOCKED = new Set([
    'explore','reels','accounts','direct','stories','p','reel','tv','about','developer','legal','privacy','terms',
    'web','emails','challenge','api','directory','push','settings'
  ]);

  function usernameFromHref(href) {
    try {
      const u = new URL(href, location.href);
      if (!/(^|\.)instagram\.com$/i.test(u.hostname)) return '';
      const parts = u.pathname.split('/').filter(Boolean);
      if (parts.length !== 1) return '';
      const username = parts[0].replace(/^@/, '').toLowerCase();
      if (!username || BLOCKED.has(username) || !/^[a-z0-9._]{1,64}$/i.test(username)) return '';
      return username;
    } catch (_) { return ''; }
  }

  function imageNear(anchor) {
    const boxes = [
      anchor,
      anchor.closest('header'),
      anchor.closest('article'),
      anchor.closest('[role="dialog"]'),
      anchor.closest('li'),
      anchor.parentElement,
      anchor.closest('div')
    ].filter(Boolean);
    for (const box of boxes) {
      const imgs = [...box.querySelectorAll('img')];
      const img = imgs.find(i => {
        const r = i.getBoundingClientRect();
        const alt = (i.alt || '').toLowerCase();
        return r.width >= 20 && r.height >= 20 && r.width <= 240 && r.height <= 240 &&
          (alt.includes('profile') || alt.includes('photo') || i.closest('a[href]'));
      });
      if (img?.currentSrc || img?.src) return img.currentSrc || img.src;
    }
    return '';
  }

  function fullNameNear(anchor, username) {
    const boxes = [anchor.parentElement, anchor.closest('header'), anchor.closest('li'), anchor.closest('[role="dialog"]'), anchor.closest('div')].filter(Boolean);
    for (const box of boxes) {
      const lines = (box.innerText || '').split('\n').map(s => s.trim()).filter(Boolean).slice(0, 10);
      const name = lines.find(s => {
        const l = s.toLowerCase().replace(/^@/, '');
        return l !== username && s.length <= 100 && !/^follow(ing)?$/i.test(s) && !/^verified$/i.test(s) &&
          !/^\d+[km,.]*\s*(likes?|comments?|followers?|following)?$/i.test(s);
      });
      if (name) return name;
    }
    return '';
  }

  function postUrlNear(anchor) {
    const article = anchor.closest('article');
    const post = article?.querySelector('a[href*="/p/"],a[href*="/reel/"]');
    if (post?.href) return post.href.split('?')[0];
    return location.href.split('?')[0];
  }

  const found = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    const username = usernameFromHref(a.href);
    if (!username) continue;
    const rec = {
      username,
      full_name: fullNameNear(a, username),
      profile_url: `https://www.instagram.com/${username}/`,
      image_url: imageNear(a),
      source: 'ios_shortcut',
      post_url: postUrlNear(a),
      observed_at: Date.now()
    };
    const old = found.get(username);
    if (!old || (!old.image_url && rec.image_url) || rec.full_name.length > old.full_name.length) found.set(username, rec);
  }

  completion(JSON.stringify({
    page_url: location.href.split('?')[0],
    records: [...found.values()],
    count: found.size
  }));
})();
