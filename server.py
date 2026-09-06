from __future__ import annotations
import base64, io, json, os, sqlite3, time
from pathlib import Path
from typing import Any
import numpy as np, requests
from PIL import Image
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from clip_engine import image_embedding, text_embedding

BASE=Path(__file__).resolve().parent
DB=Path('/tmp/clip_profiles.sqlite3')
SUPA_URL=os.getenv('SUPABASE_URL','').rstrip('/')
SUPA_KEY=os.getenv('SUPABASE_SERVICE_ROLE_KEY','')
app=FastAPI(title='Instagram CLIP Library',version='4.0')
app.mount('/static',StaticFiles(directory=BASE/'web'/'static'),name='static')

def db():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
 c.execute('''create table if not exists clip_profiles(device_id text,username text,full_name text,profile_url text,source_url text,original_image_url text,thumbnail_b64 text,embedding text,first_seen real,last_seen real,seen_count integer default 1,primary key(device_id,username))'''); return c

def supa(path, method='GET', payload=None, params=None):
 if not (SUPA_URL and SUPA_KEY): raise RuntimeError('Supabase is not configured')
 h={'apikey':SUPA_KEY,'Authorization':f'Bearer {SUPA_KEY}','Content-Type':'application/json','Prefer':'return=representation,resolution=merge-duplicates'}
 r=requests.request(method,f'{SUPA_URL}/rest/v1/{path}',headers=h,json=payload,params=params,timeout=45)
 if not r.ok: raise RuntimeError(f'Supabase {r.status_code}: {r.text[:500]}')
 return r.json() if r.text else []

def persistent(): return bool(SUPA_URL and SUPA_KEY)

def _from_supabase_row(r):
 # Map the existing Supabase instagram_profiles schema to the API's internal names.
 x=dict(r)
 x['source_url']=x.get('post_url') or ''
 x['original_image_url']=x.get('image_url') or ''
 x['thumbnail_b64']=x.get('thumbnail_base64') or ''
 return x

def fetch_all(device_id):
 if persistent():
  rows=[]; offset=0
  while True:
   chunk=supa('instagram_profiles',params={'device_id':f'eq.{device_id}','select':'*','order':'last_seen.desc','limit':'1000','offset':str(offset)})
   rows += [_from_supabase_row(r) for r in chunk]
   if len(chunk)<1000: break
   offset+=1000
  return rows
 with db() as c: return [dict(r) for r in c.execute('select * from clip_profiles where device_id=? order by last_seen desc',(device_id,))]

def save(row):
 if persistent():
  old=supa('instagram_profiles',params={'device_id':f"eq.{row['device_id']}",'username':f"eq.{row['username']}",'select':'first_seen,seen_count'})
  if old:
   first_seen=old[0]['first_seen']; seen_count=int(old[0].get('seen_count') or 1)+1
  else:
   first_seen=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()); seen_count=1
  last_seen=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
  payload={
   'device_id':row['device_id'],
   'username':row['username'],
   'full_name':row.get('full_name') or '',
   'profile_url':row.get('profile_url') or '',
   'image_url':row.get('original_image_url') or '',
   'thumbnail_base64':row.get('thumbnail_b64') or '',
   'embedding':'['+','.join(str(float(v)) for v in row['embedding'])+']',
   'source':'ios_shortcut',
   'post_url':row.get('source_url') or '',
   'first_seen':first_seen,
   'last_seen':last_seen,
   'seen_count':seen_count
  }
  supa('instagram_profiles?on_conflict=device_id,username','POST',payload); return
 with db() as c:
  old=c.execute('select first_seen,seen_count from clip_profiles where device_id=? and username=?',(row['device_id'],row['username'])).fetchone(); now=time.time()
  row['first_seen']=old['first_seen'] if old else now; row['last_seen']=now; row['seen_count']=(old['seen_count']+1) if old else 1
  c.execute('''insert or replace into clip_profiles(device_id,username,full_name,profile_url,source_url,original_image_url,thumbnail_b64,embedding,first_seen,last_seen,seen_count) values(?,?,?,?,?,?,?,?,?,?,?)''',(row['device_id'],row['username'],row['full_name'],row['profile_url'],row['source_url'],row['original_image_url'],row['thumbnail_b64'],json.dumps(row['embedding']),row['first_seen'],row['last_seen'],row['seen_count'])); c.commit()

def download_avatar(url):
 r=requests.get(url,headers={'User-Agent':'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1','Referer':'https://www.instagram.com/'},timeout=20)
 r.raise_for_status();
 if len(r.content)>8_000_000: raise ValueError('image too large')
 return r.content

def thumb(data):
 im=Image.open(io.BytesIO(data)).convert('RGB'); im.thumbnail((224,224)); out=io.BytesIO(); im.save(out,'JPEG',quality=78,optimize=True); return base64.b64encode(out.getvalue()).decode()

def vec(x):
 if isinstance(x,list): return np.asarray(x,dtype=np.float32)
 return np.asarray(json.loads(x),dtype=np.float32)

def result_row(r,score=None):
 x={k:r.get(k) for k in ['username','full_name','profile_url','source_url','original_image_url','seen_count','first_seen','last_seen']}; x['image_data_url']='data:image/jpeg;base64,'+(r.get('thumbnail_b64') or '') if r.get('thumbnail_b64') else ''
 if score is not None: x['score']=round(float(score),5)
 return x

class Ingest(BaseModel):
 device_id:str=Field(min_length=4,max_length=100); records:list[dict[str,Any]]=Field(default_factory=list,max_length=200); page_url:str|None=None; message:str|None=None

class ShortcutIngest(BaseModel):
 payload:str=Field(min_length=2,max_length=1000000); device_id:str=Field(default='iphone',min_length=1,max_length=100)

@app.get('/',response_class=HTMLResponse)
def home(): return (BASE/'web'/'index.html').read_text()
@app.get('/health')
def health(): return {'ok':True,'version':'4.0','persistent':persistent(),'clip':'openai/clip-vit-base-patch32 ONNX quantized'}
@app.get('/api/config')
def config(): return {'persistent':persistent(),'clip_enabled':True,'supabase_configured':persistent()}

def ingest_records(device_id:str, records:list[dict[str,Any]], page_url:str|None=None):
 accepted=0; indexed=0; errors=[]
 for rec in records[:200]:
  u=str(rec.get('username','')).strip().lstrip('@').lower()
  if not u or not rec.get('image_url'): continue
  accepted+=1
  try:
   data=download_avatar(str(rec['image_url'])); emb=image_embedding(data)
   save({'device_id':device_id,'username':u,'full_name':str(rec.get('full_name') or '')[:300],'profile_url':str(rec.get('profile_url') or f'https://www.instagram.com/{u}/'),'source_url':str(rec.get('post_url') or page_url or ''),'original_image_url':str(rec.get('image_url') or ''),'thumbnail_b64':thumb(data),'embedding':emb})
   indexed+=1
  except Exception as e: errors.append(f'{u}: {str(e)[:140]}')
 return {'accepted':accepted,'indexed':indexed,'failed':len(errors),'errors':errors[:10],'persistent':persistent()}

@app.post('/api/live/ingest')
def ingest(req:Ingest):
 return ingest_records(req.device_id, req.records, req.page_url)

@app.post('/api/shortcut/ingest')
def shortcut_ingest(req:ShortcutIngest):
 try:
  obj=json.loads(req.payload)
 except Exception as e:
  raise HTTPException(400,f'JavaScript result is not valid JSON: {e}')
 if not isinstance(obj,dict):
  raise HTTPException(400,'JavaScript result must be a JSON object')
 records=obj.get('records',[])
 if not isinstance(records,list):
  raise HTTPException(400,'JavaScript result records must be an array')
 result=ingest_records(req.device_id,records,str(obj.get('page_url') or ''))
 result['scanner_count']=obj.get('count',len(records))
 return result

@app.get('/api/live/stats')
def stats(device_id:str):
 rows=fetch_all(device_id); return {'profiles_collected':len(rows),'indexed':sum(1 for r in rows if r.get('embedding')),'persistent':persistent(),'message':('CLIP library ready' if persistent() else 'WARNING: Supabase not configured; using temporary Render storage')}
@app.get('/api/live/profiles')
def profiles(device_id:str,limit:int=100): return {'profiles':[result_row(r) for r in fetch_all(device_id)[:min(limit,500)]]}
@app.delete('/api/live/profiles')
def clear(device_id:str):
 if persistent(): supa('instagram_profiles','DELETE',params={'device_id':f'eq.{device_id}'})
 else:
  with db() as c: c.execute('delete from clip_profiles where device_id=?',(device_id,)); c.commit()
 return {'ok':True}

@app.get('/api/clip/search/text')
def search_text(device_id:str,q:str,limit:int=50):
 rows=[r for r in fetch_all(device_id) if r.get('embedding')];
 if not q.strip(): return {'profiles':[result_row(r) for r in rows[:limit]]}
 qv=np.asarray(text_embedding(q.strip()),dtype=np.float32); scored=[(float(np.dot(qv,vec(r['embedding']))),r) for r in rows]; scored.sort(key=lambda x:x[0],reverse=True)
 return {'profiles':[result_row(r,s) for s,r in scored[:min(limit,200)]],'query':q,'semantic':True}

@app.post('/api/clip/search/image')
async def search_image(device_id:str,file:UploadFile=File(...),limit:int=50):
 data=await file.read(); qv=np.asarray(image_embedding(data),dtype=np.float32); rows=[r for r in fetch_all(device_id) if r.get('embedding')]; scored=[(float(np.dot(qv,vec(r['embedding']))),r) for r in rows]; scored.sort(key=lambda x:x[0],reverse=True)
 return {'profiles':[result_row(r,s) for s,r in scored[:min(limit,200)]],'semantic':True}
