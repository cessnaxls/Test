# Instagram Profile Viewer v2

Clean Phase 1 rebuild.

Flow:
1. Userscript runs on instagram.com.
2. It detects profile links as Instagram loads them while you scroll.
3. It POSTs batches to `/api/profiles/batch`.
4. Render writes them to Supabase table `scraped_profiles_v2`.
5. The web app lists the profiles and images.

No CLIP, no embeddings, no model downloads, no backend Instagram scraper.

## Deploy
1. Run `SUPABASE_SETUP.txt` in Supabase SQL Editor.
2. Deploy this project to Render.
3. Add Render env vars `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`.
4. If the Render URL is not `https://instagram-profile-search.onrender.com`, edit `API` near the top of the userscript.
5. Put `instagram_profile_scraper.user.js` into the iOS Userscripts folder.
6. Restart Safari, open Instagram, turn the floating scraper ON, and scroll.
