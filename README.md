# iPhone Shortcuts + Render Instagram Library

**Recommended phone-only workflow:** use iOS Shortcuts in Safari. No Mac or Xcode is required. See `SHORTCUTS_IPHONE.md`, or deploy Render and open the dashboard: it contains a phone-friendly setup card and a Copy Shortcut JavaScript button.

The Safari Web Extension source is still included as an optional alternative.

# Instagram Live Library — Render + Safari Web Extension

This build adds a **live scrolling scanner** while preserving the previous five automated Safari-worker modes.

## Primary workflow

1. Render hosts the persistent profile library and search page.
2. Safari stays logged into `instagram.com`.
3. Open the extension popup and switch **Scanner ON**.
4. Browse Instagram in Safari normally.
5. As profile links become visible in feed posts, dialogs, comments, reels, and profile/list UI, the content script detects them.
6. New observations are deduplicated locally, placed in a persistent extension queue, and uploaded to Render in batches.
7. Render deduplicates again by `device_id + username` and preserves the best name/avatar metadata it has seen.
8. Search the library from the Render dashboard at any time.
9. Switch **Scanner OFF** to stop observing new Instagram content. The extension tries to flush its remaining queue first.

The scanner only sees Instagram pages loaded in **Safari**. It cannot inspect scrolling inside the native Instagram iOS app.

## Reliability behavior

- Scanner state is stored in extension local storage, so navigation/reloads do not silently turn it off.
- A MutationObserver plus scroll/periodic scans handle Instagram's dynamically loaded DOM.
- Only visible/near-visible Instagram profile anchors are considered for the live scanner; navigation paths such as `/explore/`, `/reels/`, `/direct/`, etc. are excluded.
- The background extension keeps a bounded persistent upload queue and retries later after network errors.
- Uploads are batched (up to 50 per request) rather than one request per profile.
- Both the extension and Render deduplicate profiles.
- Render records `first_seen`, `last_seen`, and `seen_count` for the live library.

## Deploy to Render

1. Put this directory in a Git repository.
2. In Render choose **Blueprint** and use `render.yaml`, or create a Python Web Service manually.
3. Start command:

   `gunicorn -k uvicorn.workers.UvicornWorker -w 1 -b 0.0.0.0:$PORT server:app`

4. Keep the persistent disk configured in `render.yaml`. SQLite data must live on persistent storage.
5. Open the Render URL. The page displays a random **Pairing Device ID** in that browser's local storage.

## Package the Safari Web Extension for iPhone

Apple requires Safari Web Extensions on iPhone/iPad to be packaged inside an iOS app extension with Xcode.

1. On a Mac, create an iOS app with a **Safari Web Extension** target (or use Apple's Safari Web Extension converter).
2. Replace the generated WebExtension resources with the files inside `safari-extension/`.
3. Build/install it on your iPhone.
4. Enable it under **Settings > Apps > Safari > Extensions**.
5. Grant access to `instagram.com` and your Render domain.
6. Open your Render dashboard and copy the **Pairing Device ID**.
7. Open the extension popup, enter the Render URL and Device ID, then tap **Save connection**.
8. Open `instagram.com` in Safari and turn **Scanner ON** in the extension popup.

The extension badge displays `ON` when scanning with an empty queue, or a number when profiles are waiting to upload.

## Existing automated modes

The prior job-driven workflow is still present under **Automated collection modes** on the dashboard:

- Comment likers
- Comment authors
- Profile followers
- Profile following
- Post/reel/profile likes stream

Those modes can still open 1–10 Safari worker tabs. They are independent of Live Scanner.

## Search

The included Render app supports persistent username/full-name search and recent browsing of the live library. This build intentionally does not label that as CLIP semantic search. A GPU/image-embedding service can be added later while keeping the live collection path unchanged.

## Privacy/security note

The Render service does not receive your Instagram password or Safari cookies. The extension reads the Instagram DOM that Safari has permission to expose and sends the extracted profile metadata to the Render URL you configure. Treat the Render deployment and Device ID as private, and add authentication before exposing a production deployment to other users.
