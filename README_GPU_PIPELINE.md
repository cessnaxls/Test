# Instagram Gallery + High-Throughput GPU CLIP

This build keeps the working JavaScript scraper and gallery, but moves CLIP image analysis off Render.

## Pipeline

Instagram Userscript
→ Render/Supabase profile gallery
→ 16 concurrent Render index coordinators
→ GPU worker
→ 32 concurrent pooled photo downloads
→ CLIP GPU batches up to 128 images
→ one bulk Supabase embedding write per returned batch

Text search uses the exact same CLIP model on the GPU worker, then Render scores every indexed image vector and sorts the full result set by similarity.

## 1. Supabase

Run `SUPABASE_GPU_CLIP_UPGRADE.txt` in the SQL editor.

## 2. GPU worker

Deploy the files:

- `gpu_worker.py`
- `gpu_requirements.txt`
- `Dockerfile.gpu`

to a CUDA GPU service/container.

Set the same strong random value as `GPU_WORKER_TOKEN` on the GPU worker and Render.

Useful GPU settings:

- `PHOTO_DOWNLOAD_CONCURRENCY=32`
- `GPU_CLIP_BATCH_SIZE=128`
- `GPU_INFERENCE_CONCURRENCY=2`
- `PHOTO_DOWNLOAD_RETRIES=3`
- `CLIP_MODEL_ID=openai/clip-vit-base-patch32`

The GPU `/health` endpoint should report `"device":"cuda"` before you start indexing.

## 3. Render

Deploy the normal gallery files to the existing Render service.

Add:

- `GPU_WORKER_URL=https://YOUR-GPU-WORKER`
- `GPU_WORKER_TOKEN=the-same-private-token`

Keep the existing:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

Default coordinator settings are 16 workers × 64 profiles.

## Throughput

The architecture is designed for the requested 1,000-profile burst. Actual 15–30 second completion depends on GPU type, CDN response time, and how many source avatar URLs are still valid. The GPU worker response reports its measured `images_per_second`, so tune from real numbers rather than guessing.

The scraper does not need to change.
