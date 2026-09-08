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

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(
            1,
            int(os.getenv("CLIP_INTRA_THREADS", "2")),
        )
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        _SESSION = ort.InferenceSession(
            model_path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

        _OUTPUT_NAMES = [o.name for o in _SESSION.get_outputs()]


def _run(images, texts):
    _load()

    inputs = _PROCESSOR(
        text=texts,
        images=[img.convert("RGB") for img in images],
        return_tensors="np",
        padding=True,
    )

    valid = {i.name: i for i in _SESSION.get_inputs()}
    feed = {}

    for name, value in inputs.items():
        if name not in valid:
            continue

        expected = valid[name].type
        if "int64" in expected:
            value = value.astype(np.int64)
        elif "float" in expected:
            value = value.astype(np.float32)

        feed[name] = value

    outputs = _SESSION.run(None, feed)
    return dict(zip(_OUTPUT_NAMES, outputs))


def _image_matrix(outputs):
    names = [
        name for name in outputs
        if "image" in name.lower() and "embed" in name.lower()
    ]

    if not names:
        names = [
            name for name, value in outputs.items()
            if hasattr(value, "shape")
            and len(value.shape) == 2
            and value.shape[-1] == 512
        ][-1:]

    if not names:
        raise RuntimeError(f"Image embedding output not found: {list(outputs)}")

    matrix = np.asarray(outputs[names[0]], dtype=np.float32)
    matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
    return matrix


def image_embedding(data: bytes):
    image = Image.open(io.BytesIO(data)).convert("RGB")
    outputs = _run([image], ["a photo"])
    return _image_matrix(outputs)[0].tolist()
