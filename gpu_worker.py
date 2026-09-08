from __future__ import annotations

import asyncio
import io
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import torch
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

MODEL_ID = os.getenv("CLIP_MODEL_ID", "openai/clip-vit-base-patch32")
API_TOKEN = os.getenv("GPU_WORKER_TOKEN", "")
DOWNLOAD_CONCURRENCY = max(1, int(os.getenv("PHOTO_DOWNLOAD_CONCURRENCY", "32")))
GPU_BATCH_SIZE = max(1, int(os.getenv("GPU_CLIP_BATCH_SIZE", "128")))
MAX_INFLIGHT_INFERENCE = max(1, int(os.getenv("GPU_INFERENCE_CONCURRENCY", "2")))
DOWNLOAD_RETRIES = max(0, int(os.getenv("PHOTO_DOWNLOAD_RETRIES", "3")))
MAX_IMAGE_BYTES = max(100_000, int(os.getenv("MAX_IMAGE_BYTES", "8000000")))

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DTYPE = torch.float16 if DEVICE == "cuda" else torch.float32

MODEL: CLIPModel | None = None
PROCESSOR: CLIPProcessor | None = None
HTTP: httpx.AsyncClient | None = None
INFERENCE_SEM: asyncio.Semaphore | None = None
DOWNLOAD_SEM: asyncio.Semaphore | None = None


class ImageItem(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    image_url: str = Field(min_length=8, max_length=5000)


class ImageBatch(BaseModel):
    items: list[ImageItem] = Field(default_factory=list, max_length=512)


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


def require_token(authorization: str | None):
    if not API_TOKEN:
        return
    expected = f"Bearer {API_TOKEN}"
    if authorization != expected:
        raise HTTPException(401, "Unauthorized")


def normalize(features: torch.Tensor) -> torch.Tensor:
    return features / features.norm(dim=-1, keepdim=True).clamp_min(1e-12)


async def decode_image(data: bytes) -> Image.Image:
    def _decode():
        image = Image.open(io.BytesIO(data))
        image.load()
        return image.convert("RGB")
    return await asyncio.to_thread(_decode)


async def download_one(item: ImageItem):
    assert HTTP is not None
    assert DOWNLOAD_SEM is not None

    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
        "Referer": "https://www.instagram.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    last_error = ""

    async with DOWNLOAD_SEM:
        for attempt in range(DOWNLOAD_RETRIES + 1):
            try:
                async with HTTP.stream("GET", item.image_url, headers=headers) as response:
                    if response.status_code in (408, 425, 429, 500, 502, 503, 504):
                        last_error = f"HTTP {response.status_code}"
                        if attempt < DOWNLOAD_RETRIES:
                            await asyncio.sleep(0.15 * (2 ** attempt))
                            continue

                    response.raise_for_status()

                    content_type = response.headers.get("content-type", "")
                    if not content_type.startswith("image/"):
                        raise RuntimeError("URL did not return an image")

                    chunks = []
                    total = 0

                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise RuntimeError("image too large")
                        chunks.append(chunk)

                    data = b"".join(chunks)

                image = await decode_image(data)
                return item.username, image, None

            except Exception as exc:
                last_error = str(exc)[:240]
                if attempt < DOWNLOAD_RETRIES:
                    await asyncio.sleep(0.15 * (2 ** attempt))

    return item.username, None, last_error or "download failed"


async def embed_pil_images(usernames: list[str], images: list[Image.Image]):
    assert MODEL is not None
    assert PROCESSOR is not None
    assert INFERENCE_SEM is not None

    results = []

    async with INFERENCE_SEM:
        for start in range(0, len(images), GPU_BATCH_SIZE):
            batch_images = images[start:start + GPU_BATCH_SIZE]
            batch_names = usernames[start:start + GPU_BATCH_SIZE]

            def _prepare():
                return PROCESSOR(images=batch_images, return_tensors="pt")

            inputs = await asyncio.to_thread(_prepare)
            pixel_values = inputs["pixel_values"].to(DEVICE, non_blocking=True)

            with torch.inference_mode():
                if DEVICE == "cuda":
                    with torch.autocast(device_type="cuda", dtype=torch.float16):
                        features = MODEL.get_image_features(pixel_values=pixel_values)
                else:
                    features = MODEL.get_image_features(pixel_values=pixel_values)

                features = normalize(features.float()).cpu().numpy()

            for username, vector in zip(batch_names, features):
                results.append({
                    "username": username,
                    "embedding": vector.tolist(),
                })

    return results


@asynccontextmanager
async def lifespan(app: FastAPI):
    global MODEL, PROCESSOR, HTTP, INFERENCE_SEM, DOWNLOAD_SEM

    torch.set_grad_enabled(False)

    PROCESSOR = CLIPProcessor.from_pretrained(MODEL_ID)
    MODEL = CLIPModel.from_pretrained(
        MODEL_ID,
        torch_dtype=DTYPE if DEVICE == "cuda" else torch.float32,
    )
    MODEL.eval()
    MODEL.to(DEVICE)

    if DEVICE == "cuda":
        torch.backends.cudnn.benchmark = True

    limits = httpx.Limits(
        max_connections=max(DOWNLOAD_CONCURRENCY * 2, 64),
        max_keepalive_connections=max(DOWNLOAD_CONCURRENCY, 32),
        keepalive_expiry=30.0,
    )

    timeout = httpx.Timeout(
        connect=8.0,
        read=15.0,
        write=10.0,
        pool=10.0,
    )

    HTTP = httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        limits=limits,
        timeout=timeout,
    )

    INFERENCE_SEM = asyncio.Semaphore(MAX_INFLIGHT_INFERENCE)
    DOWNLOAD_SEM = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    # Warm the model once so the first real request is fast.
    dummy = Image.new("RGB", (224, 224), "white")
    await embed_pil_images(["warmup"], [dummy])

    yield

    await HTTP.aclose()


app = FastAPI(
    title="GPU CLIP Worker",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    gpu_name = (
        torch.cuda.get_device_name(0)
        if torch.cuda.is_available()
        else None
    )

    return {
        "ok": True,
        "device": DEVICE,
        "gpu": gpu_name,
        "model": MODEL_ID,
        "download_concurrency": DOWNLOAD_CONCURRENCY,
        "gpu_batch_size": GPU_BATCH_SIZE,
        "inference_concurrency": MAX_INFLIGHT_INFERENCE,
    }


@app.post("/embed/images")
async def embed_images(
    request: ImageBatch,
    authorization: str | None = Header(default=None),
):
    require_token(authorization)

    started = time.perf_counter()

    # Deduplicate URLs/usernames inside a single coordinator request.
    unique = {}
    for item in request.items:
        unique[item.username] = item

    downloaded = await asyncio.gather(
        *(download_one(item) for item in unique.values())
    )

    usernames = []
    images = []
    failures = []

    for username, image, error in downloaded:
        if image is None:
            failures.append({
                "username": username,
                "error": error or "download failed",
            })
        else:
            usernames.append(username)
            images.append(image)

    embeddings = (
        await embed_pil_images(usernames, images)
        if images
        else []
    )

    elapsed = max(time.perf_counter() - started, 1e-6)

    return {
        "received": len(request.items),
        "unique": len(unique),
        "embedded": len(embeddings),
        "failed": len(failures),
        "elapsed_seconds": round(elapsed, 4),
        "images_per_second": round(len(embeddings) / elapsed, 2),
        "embeddings": embeddings,
        "failures": failures,
    }


@app.post("/embed/text")
async def embed_text(
    request: TextIn,
    authorization: str | None = Header(default=None),
):
    require_token(authorization)

    assert MODEL is not None
    assert PROCESSOR is not None
    assert INFERENCE_SEM is not None

    async with INFERENCE_SEM:
        inputs = PROCESSOR(
            text=[request.text],
            return_tensors="pt",
            padding=True,
        )

        input_ids = inputs["input_ids"].to(DEVICE, non_blocking=True)
        attention_mask = inputs.get("attention_mask")
        if attention_mask is not None:
            attention_mask = attention_mask.to(DEVICE, non_blocking=True)

        with torch.inference_mode():
            if DEVICE == "cuda":
                with torch.autocast(device_type="cuda", dtype=torch.float16):
                    features = MODEL.get_text_features(
                        input_ids=input_ids,
                        attention_mask=attention_mask,
                    )
            else:
                features = MODEL.get_text_features(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                )

            vector = normalize(features.float())[0].cpu().numpy().tolist()

    return {
        "text": request.text,
        "embedding": vector,
    }
