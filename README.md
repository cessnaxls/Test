# Instagram Profile Gallery

Fresh rebuild focused only on profile photos and direct Instagram profile links.

Flow:
Instagram userscript -> /api/profiles/batch -> Supabase -> Render gallery.

Required Render environment variables:
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY

The userscript points to:
https://instagram-profile-search.onrender.com

The gallery defaults to Photos Only.
Tap any photo or username to open the Instagram profile.
