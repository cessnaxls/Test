import os
from huggingface_hub import hf_hub_download
from transformers import CLIPProcessor
repo=os.getenv('CLIP_ONNX_REPO','onnx-community/clip-vit-base-patch32-ONNX')
file=os.getenv('CLIP_ONNX_FILE','onnx/model_quantized.onnx')
cache=os.getenv('HF_HOME','.hf')
print('Prefetching CLIP model...')
hf_hub_download(repo_id=repo,filename=file,cache_dir=cache)
CLIPProcessor.from_pretrained(repo,cache_dir=cache)
print('CLIP cache ready')
