from __future__ import annotations
import io, os, threading
import numpy as np
from PIL import Image

_LOCK = threading.RLock()
_SESSION = None
_PROCESSOR = None
_OUTPUTS = None

REPO = os.getenv('CLIP_ONNX_REPO', 'onnx-community/clip-vit-base-patch32-ONNX')
PROCESSOR_REPO = os.getenv('CLIP_PROCESSOR_REPO', 'openai/clip-vit-base-patch32')
FILE = os.getenv('CLIP_ONNX_FILE', 'onnx/model_quantized.onnx')


def _load():
    global _SESSION, _PROCESSOR, _OUTPUTS
    if _SESSION is not None:
        return
    with _LOCK:
        if _SESSION is not None:
            return
        from huggingface_hub import hf_hub_download
        import onnxruntime as ort
        from transformers import CLIPProcessor
        path = hf_hub_download(repo_id=REPO, filename=FILE, cache_dir=os.getenv('HF_HOME', '/tmp/hf'))
        _PROCESSOR = CLIPProcessor.from_pretrained(PROCESSOR_REPO, cache_dir=os.getenv('HF_HOME', '/tmp/hf'))
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, int(os.getenv('CLIP_INTRA_THREADS', '4')))
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.enable_cpu_mem_arena = True
        so.enable_mem_pattern = True
        _SESSION = ort.InferenceSession(path, sess_options=so, providers=['CPUExecutionProvider'])
        _OUTPUTS = [o.name for o in _SESSION.get_outputs()]


def _feed(images, texts):
    _load()
    inputs = _PROCESSOR(text=texts, images=[im.convert('RGB') for im in images], return_tensors='np', padding=True)
    feed = {}
    valid = {i.name: i for i in _SESSION.get_inputs()}
    for k, v in inputs.items():
        if k not in valid:
            continue
        expected = valid[k].type
        if 'int64' in expected:
            v = v.astype(np.int64)
        elif 'float' in expected:
            v = v.astype(np.float32)
        feed[k] = v
    vals = _SESSION.run(None, feed)
    return dict(zip(_OUTPUTS, vals))


def _matrix(outputs, kind):
    candidates = [k for k in outputs if kind in k.lower() and 'embed' in k.lower()]
    if not candidates:
        candidates = [k for k, v in outputs.items() if hasattr(v, 'shape') and len(v.shape) == 2 and v.shape[-1] == 512]
        if kind == 'image' and len(candidates) > 1:
            candidates = candidates[-1:]
        elif kind == 'text' and candidates:
            candidates = candidates[:1]
    if not candidates:
        raise RuntimeError(f'CLIP output for {kind} embedding not found: {list(outputs)}')
    mat = np.asarray(outputs[candidates[0]], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return mat / np.maximum(norms, 1e-12)


def image_embeddings(items: list[bytes]):
    if not items:
        return []
    images = [Image.open(io.BytesIO(data)).convert('RGB') for data in items]
    # CLIP requires text tensors too; one dummy phrase per image keeps batch dimensions aligned.
    outputs = _feed(images, ['a photo'] * len(images))
    return _matrix(outputs, 'image').tolist()


def image_embedding(data: bytes):
    return image_embeddings([data])[0]


def text_embedding(text: str):
    blank = Image.new('RGB', (224, 224), 'white')
    outputs = _feed([blank], [text])
    return _matrix(outputs, 'text')[0].tolist()
