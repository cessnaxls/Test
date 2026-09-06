from __future__ import annotations
import base64, io, json, os, sqlite3, time, queue, threading
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

INDEX_QUEUE=queue.Queue()
INDEX_STATE={}
INDEX_LOCK=threading.RLock()
QUEUED_KEYS=set()

def _state(device_id):
 with INDEX_LOCK:
  return INDEX_STATE.setdefault(device_id, {'processing':0,'failed':0,'last_error':''})

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


def save_pending(device_id, rec, page_url=''):
 u=str(rec.get('username','')).strip().lstrip('@').lower()
 if not u: return False
 full=str(rec.get('full_name') or '')[:300]
 profile=str(rec.get('profile_url') or f'https://www.instagram.com/{u}/')
 image=str(rec.get('image_url') or '')
 source_url=str(rec.get('post_url') or page_url or '')
 if persistent():
  old=supa('instagram_profiles',params={'device_id':f'eq.{device_id}','username':f'eq.{u}','select':'first_seen,seen_count,embedding,thumbnail_base64'})
  if old:
   first_seen=old[0]['first_seen']; seen_count=int(old[0].get('seen_count') or 1)+1
   embedding=old[0].get('embedding'); thumb_b64=old[0].get('thumbnail_base64') or ''
  else:
   first_seen=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()); seen_count=1; embedding=None; thumb_b64=''
  now=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
  payload={'device_id':device_id,'username':u,'full_name':full,'profile_url':profile,'image_url':image,
           'thumbnail_base64':thumb_b64,'embedding':embedding,'source':'ios_shortcut','post_url':source_url,
           'first_seen':first_seen,'last_seen':now,'seen_count':seen_count}
  supa('instagram_profiles?on_conflict=device_id,username','POST',payload)
 else:
  with db() as c:
   old=c.execute('select first_seen,seen_count,embedding,thumbnail_b64 from clip_profiles where device_id=? and username=?',(device_id,u)).fetchone()
   now=time.time()
   first_seen=old['first_seen'] if old else now; seen_count=(old['seen_count']+1) if old else 1
   emb=old['embedding'] if old else None; tb=old['thumbnail_b64'] if old else ''
   c.execute("insert or replace into clip_profiles(device_id,username,full_name,profile_url,source_url,original_image_url,thumbnail_b64,embedding,first_seen,last_seen,seen_count) values(?,?,?,?,?,?,?,?,?,?,?)",
             (device_id,u,full,profile,source_url,image,tb,emb,first_seen,now,seen_count)); c.commit()
 return True

def update_indexed(device_id, username, image_url, data, emb):
 tb=thumb(data)
 if persistent():
  vector='['+','.join(str(float(v)) for v in emb)+']'
  supa('instagram_profiles','PATCH',
       {'thumbnail_base64':tb,'embedding':vector,'image_url':image_url,'last_seen':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())},
       {'device_id':f'eq.{device_id}','username':f'eq.{username}'})
 else:
  with db() as c:
   c.execute('update clip_profiles set thumbnail_b64=?,embedding=?,original_image_url=?,last_seen=? where device_id=? and username=?',
             (tb,json.dumps(emb),image_url,time.time(),device_id,username)); c.commit()

def enqueue_profile(device_id, rec, page_url=''):
 u=str(rec.get('username','')).strip().lstrip('@').lower()
 image=str(rec.get('image_url') or '')
 if not u or not image: return False
 save_pending(device_id,rec,page_url)
 key=(device_id,u)
 with INDEX_LOCK:
  if key in QUEUED_KEYS: return True
  QUEUED_KEYS.add(key)
 INDEX_QUEUE.put((device_id,u,image))
 return True

def index_worker():
 while True:
  device_id,u,image=INDEX_QUEUE.get()
  st=_state(device_id)
  with INDEX_LOCK: st['processing']+=1
  try:
   data=download_avatar(image)
   emb=image_embedding(data)
   update_indexed(device_id,u,image,data,emb)
  except Exception as e:
   with INDEX_LOCK:
    st['failed']+=1
    st['last_error']=f'{u}: {str(e)[:180]}'
  finally:
   with INDEX_LOCK:
    st['processing']=max(0,st['processing']-1)
    QUEUED_KEYS.discard((device_id,u))
   INDEX_QUEUE.task_done()

@app.on_event('startup')
def start_index_worker():
 if not any(t.name=='clip-index-worker' and t.is_alive() for t in threading.enumerate()):
  threading.Thread(target=index_worker,name='clip-index-worker',daemon=True).start()

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
def health(): return {'ok':True,'version':'4.1','persistent':persistent(),'clip':'openai/clip-vit-base-patch32 ONNX quantized'}
@app.get('/api/config')
def config(): return {'persistent':persistent(),'clip_enabled':True,'supabase_configured':persistent()}

def ingest_records(device_id:str, records:list[dict[str,Any]], page_url:str|None=None):
 captured=0; queued=0; skipped=0; errors=[]
 for rec in records[:200]:
  u=str(rec.get('username','')).strip().lstrip('@').lower()
  if not u or not rec.get('image_url'):
   skipped+=1
   continue
  captured+=1
  try:
   if enqueue_profile(device_id,rec,page_url or ''): queued+=1
  except Exception as e:
   errors.append(f'{u}: {str(e)[:140]}')
 return {'captured':captured,'queued':queued,'skipped':skipped,'failed_to_queue':len(errors),
         'errors':errors[:10],'persistent':persistent()}

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
 rows=fetch_all(device_id)
 indexed=sum(1 for r in rows if r.get('embedding'))
 captured=len(rows)
 st=_state(device_id)
 with INDEX_LOCK:
  processing=st['processing']; failed=st['failed']; last_error=st['last_error']
 pending=max(captured-indexed,0)
 queued=max(pending-processing-failed,0)
 return {'profiles_collected':captured,'captured':captured,'queued':queued,'processing':processing,
         'indexed':indexed,'failed':failed,'last_error':last_error,'persistent':persistent(),
         'message':('CLIP indexing in progress' if pending else 'CLIP library ready')}
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
