import os
from huggingface_hub import hf_hub_download
from transformers import CLIPProcessor
repo=os.getenv('CLIP_ONNX_REPO','sayantan47/clip-vit-b32-onnx')
file=os.getenv('CLIP_ONNX_FILE','onnx/model_quantized.onnx')
cache=os.getenv('HF_HOME','.hf')
print('Prefetching CLIP model...')
hf_hub_download(repo_id=repo,filename=file,cache_dir=cache)
CLIPProcessor.from_pretrained(repo,cache_dir=cache)
print('CLIP cache ready')
