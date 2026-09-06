from __future__ import annotations
import io, os, threading
import numpy as np
from PIL import Image

_LOCK = threading.RLock()
_SESSION = None
_PROCESSOR = None
_OUTPUTS = None

REPO = os.getenv('CLIP_ONNX_REPO', 'sayantan47/clip-vit-b32-onnx')
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
        _PROCESSOR = CLIPProcessor.from_pretrained(REPO, cache_dir=os.getenv('HF_HOME', '/tmp/hf'))
        so = ort.SessionOptions(); so.intra_op_num_threads = 1; so.inter_op_num_threads = 1
        _SESSION = ort.InferenceSession(path, sess_options=so, providers=['CPUExecutionProvider'])
        _OUTPUTS = [o.name for o in _SESSION.get_outputs()]


def _run(image: Image.Image, text: str):
    _load()
    inputs = _PROCESSOR(text=[text], images=[image.convert('RGB')], return_tensors='np', padding=True)
    feed = {}
    valid = {i.name: i for i in _SESSION.get_inputs()}
    for k, v in inputs.items():
        if k not in valid: continue
        expected = valid[k].type
        if 'int64' in expected: v = v.astype(np.int64)
        elif 'float' in expected: v = v.astype(np.float32)
        feed[k] = v
    vals = _SESSION.run(None, feed)
    return dict(zip(_OUTPUTS, vals))


def _pick(outputs, kind):
    candidates = [k for k in outputs if kind in k.lower() and 'embed' in k.lower()]
    if not candidates:
        # HF CLIP usually exports text_embeds/image_embeds. Fall back by shape.
        candidates = [k for k,v in outputs.items() if hasattr(v, 'shape') and len(v.shape)==2 and v.shape[-1]==512]
        if kind == 'image' and len(candidates) > 1: candidates = candidates[-1:]
        if kind == 'text' and candidates: candidates = candidates[:1]
    if not candidates: raise RuntimeError(f'CLIP output for {kind} embedding not found: {list(outputs)}')
    vec = np.asarray(outputs[candidates[0]][0], dtype=np.float32)
    n = np.linalg.norm(vec)
    return (vec / max(n, 1e-12)).tolist()


def image_embedding(data: bytes):
    im = Image.open(io.BytesIO(data)).convert('RGB')
    out = _run(im, 'a photo')
    return _pick(out, 'image')


def text_embedding(text: str):
    blank = Image.new('RGB', (224,224), 'white')
    out = _run(blank, text)
    return _pick(out, 'text')
