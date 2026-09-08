from __future__ import annotations

import io
import os
import threading

import numpy as np
from PIL import Image

_LOCK = threading.RLock()
_SESSION = None
_PROCESSOR = None
_OUTPUT_NAMES = None

MODEL_REPO = os.getenv(
    "CLIP_ONNX_REPO",
    "onnx-community/clip-vit-base-patch32-ONNX",
)
PROCESSOR_REPO = os.getenv(
    "CLIP_PROCESSOR_REPO",
    "openai/clip-vit-base-patch32",
)
MODEL_FILE = os.getenv(
    "CLIP_ONNX_FILE",
    "onnx/model_quantized.onnx",
)


def _load():
    global _SESSION, _PROCESSOR, _OUTPUT_NAMES

    if _SESSION is not None:
        return

    with _LOCK:
        if _SESSION is not None:
            return

        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from transformers import CLIPProcessor

        cache = os.getenv("HF_HOME", "/tmp/hf")

        model_path = hf_hub_download(
            repo_id=MODEL_REPO,
            filename=MODEL_FILE,
            cache_dir=cache,
        )

        _PROCESSOR = CLIPProcessor.from_pretrained(
            PROCESSOR_REPO,
            cache_dir=cache,
        )

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(
            1,
            int(os.getenv("CLIP_INTRA_THREADS", "2")),
        )
        options.inter_op_num_threads = 1
        options.graph_optimization_level = (
            ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        )

        _SESSION = ort.InferenceSession(
            model_path,
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

        _OUTPUT_NAMES = [output.name for output in _SESSION.get_outputs()]


def _run(images, texts):
    _load()

    processed = _PROCESSOR(
        text=texts,
        images=[image.convert("RGB") for image in images],
        return_tensors="np",
        padding=True,
    )

    session_inputs = {item.name: item for item in _SESSION.get_inputs()}
    feed = {}

    for name, value in processed.items():
        if name not in session_inputs:
            continue

        expected = session_inputs[name].type

        if "int64" in expected:
            value = value.astype(np.int64)
        elif "float" in expected:
            value = value.astype(np.float32)

        feed[name] = value

    outputs = _SESSION.run(None, feed)

    return dict(zip(_OUTPUT_NAMES, outputs))


def _embedding_matrix(outputs, kind):
    names = [
        name
        for name in outputs
        if kind in name.lower() and "embed" in name.lower()
    ]

    if not names:
        candidates = [
            name
            for name, value in outputs.items()
            if hasattr(value, "shape")
            and len(value.shape) == 2
            and value.shape[-1] == 512
        ]

        if kind == "text":
            names = candidates[:1]
        else:
            names = candidates[-1:]

    if not names:
        raise RuntimeError(
            f"Could not identify {kind} CLIP embedding output. "
            f"Outputs: {list(outputs)}"
        )

    matrix = np.asarray(outputs[names[0]], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)

    return matrix / np.maximum(norms, 1e-12)


def image_embeddings(items: list[bytes]):
    if not items:
        return []

    images = [
        Image.open(io.BytesIO(data)).convert("RGB")
        for data in items
    ]

    outputs = _run(
        images,
        ["a photo"] * len(images),
    )

    return _embedding_matrix(outputs, "image").tolist()


def image_embedding(data: bytes):
    return image_embeddings([data])[0]


def text_embedding(text: str):
    blank = Image.new("RGB", (224, 224), "white")

    outputs = _run(
        [blank],
        [text],
    )

    return _embedding_matrix(outputs, "text")[0].tolist()
