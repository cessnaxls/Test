# iPhone-only setup: Instagram → Shortcuts → Render

This workflow does not require a Mac, Xcode, or a Safari extension.

## What it does

1. Browse **instagram.com in Safari** while logged in.
2. Scroll through the feed, a profile list, comments, likes, followers/following, or another Instagram page.
3. Run the **Collect Instagram Profiles** Shortcut from Safari's Share Sheet.
4. The Shortcut's JavaScript reads Instagram profile links that are currently loaded in the page DOM.
5. Shortcuts POSTs those profile records to your Render service.
6. Render deduplicates them by username and keeps the persistent searchable library.

Important: iOS Shortcuts cannot continuously watch Safari while you scroll. Run the Shortcut periodically as you browse. Instagram may recycle old feed DOM nodes, so running it every few screens captures more reliably than scrolling for a very long time and running it only once at the end.

## Phone-only creation

Open your deployed Render dashboard on your iPhone. The **iPhone Shortcut Collector** card gives you:

- your Device ID;
- a **Copy Shortcut JavaScript** button;
- the exact Render ingest URL;
- the complete action sequence.

Create a shortcut named **Collect Instagram Profiles** and enable **Show in Share Sheet** for **Safari Web Pages**.

Add these actions in order:

1. **Run JavaScript on Web Page** — paste the collector JS copied from your Render dashboard. Input: Shortcut Input.
2. **Get Dictionary from Input**.
3. **Get Dictionary Value** `records`.
4. **Get Dictionary Value** `page_url`.
5. **Dictionary** with:
   - `device_id`: your dashboard Device ID
   - `records`: Magic Variable from step 3
   - `page_url`: Magic Variable from step 4
   - `message`: `Collected by iOS Shortcut`
6. **Get Contents of URL**:
   - URL: `https://YOUR-RENDER-SERVICE.onrender.com/api/live/ingest`
   - Method: POST
   - Request Body: JSON
   - JSON body: the Dictionary from step 5
7. Optional **Show Result**.

## Search

Open your Render dashboard. Recent profiles appear in the library and the Search field searches usernames and names. Re-running the Shortcut over the same profiles is safe; Render deduplicates by username and updates last-seen metadata.
