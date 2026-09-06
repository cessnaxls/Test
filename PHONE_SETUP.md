# iPhone-only setup for persistent CLIP search

## A. Update Render
Upload this project over the existing `instagram_clip_render_safari` folder in your GitHub repo. Render will redeploy automatically.

## B. Create free Supabase project
1. In Safari open https://supabase.com and create/sign in.
2. Create a new free project.
3. Open SQL Editor, New query, paste all of `SUPABASE_SETUP.sql`, Run.
4. Project Settings → API: copy Project URL and the **service_role** key. Never put the service-role key in the Shortcut or webpage.
5. Render → your service → Environment. Add:
   - `SUPABASE_URL` = your Project URL
   - `SUPABASE_SERVICE_ROLE_KEY` = your service-role key
6. Save; Render redeploys.
7. Open `/health` on your Render URL and confirm `persistent` is `true`.

## C. Shortcut
Use the dashboard's Copy Shortcut JavaScript button. The Shortcut only extracts visible/loaded Instagram profiles. Shortcuts sends the result to `/api/live/ingest`; Render downloads each avatar, creates a CLIP image embedding, makes a compact thumbnail, and saves both to Supabase.

## D. Search
Use the dashboard for text-to-image CLIP search or upload a reference image for image-to-image CLIP search.

Note: iOS Shortcuts cannot continuously watch Safari. Run the Shortcut periodically as you scroll.
