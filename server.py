from __future__ import annotations
import base64, io, json, os, sqlite3, time, threading, concurrent.futures, collections, re, html
from pathlib import Path
from typing import Any
from html.parser import HTMLParser
import numpy as np, requests
from PIL import Image
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from clip_engine import image_embedding, image_embeddings, text_embedding

BASE=Path(__file__).resolve().parent
DB=Path('/tmp/clip_profiles.sqlite3')
SUPA_URL=os.getenv('SUPABASE_URL','').rstrip('/')
SUPA_KEY=os.getenv('SUPABASE_SERVICE_ROLE_KEY','')
app=FastAPI(title='Instagram CLIP Library',version='7.0-turbo')
app.add_middleware(
 CORSMiddleware,
 allow_origins=['https://www.instagram.com','https://instagram.com'],
 allow_credentials=False,
 allow_methods=['GET','POST','OPTIONS'],
 allow_headers=['*'],
)
app.mount('/static',StaticFiles(directory=BASE/'web'/'static'),name='static')

BATCH_SIZE=max(1,int(os.getenv('CLIP_BATCH_SIZE','32')))
DOWNLOAD_WORKERS=max(1,int(os.getenv('AVATAR_DOWNLOAD_WORKERS','32')))
POLL_SECONDS=max(0.05,float(os.getenv('INDEX_POLL_SECONDS','0.15')))
INGEST_MAX=max(100,int(os.getenv('INGEST_MAX_BATCH','1000')))
HTTP=requests.Session()
HTTP.headers.update({'Connection':'keep-alive'})
adapter=requests.adapters.HTTPAdapter(pool_connections=64,pool_maxsize=64,max_retries=1)
HTTP.mount('https://',adapter)
INDEX_LOCK=threading.RLock()
RATE_EVENTS=collections.deque(maxlen=2000)
LAST_ERROR=''
PROCESSING=0

def db():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
 c.execute('''create table if not exists clip_profiles(device_id text,username text,full_name text,profile_url text,source_url text,original_image_url text,thumbnail_b64 text,embedding text,first_seen real,last_seen real,seen_count integer default 1,primary key(device_id,username))'''); return c

def supa(path, method='GET', payload=None, params=None):
 if not (SUPA_URL and SUPA_KEY): raise RuntimeError('Supabase is not configured')
 h={'apikey':SUPA_KEY,'Authorization':f'Bearer {SUPA_KEY}','Content-Type':'application/json','Prefer':'return=representation,resolution=merge-duplicates'}
 r=HTTP.request(method,f'{SUPA_URL}/rest/v1/{path}',headers=h,json=payload,params=params,timeout=45)
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


def _iso_now():
 return time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())

def _pending_rows(limit):
 if not persistent(): return []
 return supa('instagram_profiles',params={
  'embedding':'is.null','index_status':'in.(queued,failed)','index_attempts':'lt.4',
  'select':'device_id,username,full_name,profile_url,image_url,source,post_url,first_seen,last_seen,seen_count,index_status,index_attempts',
  'order':'last_seen.asc','limit':str(limit)
 })

def _claim_rows(rows):
 if not rows: return
 by_device={}
 for r in rows: by_device.setdefault(r['device_id'],[]).append(r['username'])
 for device_id,names in by_device.items():
  for i in range(0,len(names),200):
   chunk=names[i:i+200]
   supa('instagram_profiles','PATCH',{'index_status':'indexing','index_error':None},
        {'device_id':f'eq.{device_id}','username':f"in.({','.join(chunk)})",'embedding':'is.null'})

def _bulk_upsert(rows):
 if rows:
  supa('instagram_profiles?on_conflict=device_id,username','POST',rows)

def save_pending(device_id, rec, page_url=''):
 u=str(rec.get('username','')).strip().lstrip('@').lower()
 if not u: return False
 full=str(rec.get('full_name') or '')[:300]
 profile=str(rec.get('profile_url') or f'https://www.instagram.com/{u}/')
 image=str(rec.get('image_url') or '')
 source_url=str(rec.get('post_url') or page_url or '')
 if not image: return False
 if persistent():
  old=supa('instagram_profiles',params={'device_id':f'eq.{device_id}','username':f'eq.{u}','select':'first_seen,seen_count,embedding,thumbnail_base64,index_status,index_attempts'})
  now=_iso_now()
  if old:
   o=old[0]; first=o['first_seen']; seen=int(o.get('seen_count') or 1)+1
   # Already indexed rows remain indexed unless the image URL has changed materially; keep their vector.
   status='indexed' if o.get('embedding') else 'queued'
   attempts=int(o.get('index_attempts') or 0) if status=='indexed' else 0
   emb=o.get('embedding'); tb=o.get('thumbnail_base64') or ''
  else:
   first=now; seen=1; status='queued'; attempts=0; emb=None; tb=''
  payload={'device_id':device_id,'username':u,'full_name':full,'profile_url':profile,'image_url':image,
   'thumbnail_base64':tb,'embedding':emb,'source':'ios_shortcut','post_url':source_url,
   'first_seen':first,'last_seen':now,'seen_count':seen,'index_status':status,'index_attempts':attempts,
   'index_error':None if status=='queued' else None,'indexed_at':o.get('indexed_at') if old and status=='indexed' else None}
  _bulk_upsert([payload])
  return True
 # Local fallback keeps old synchronous behavior semantics.
 with db() as c:
  old=c.execute('select first_seen,seen_count,embedding,thumbnail_b64 from clip_profiles where device_id=? and username=?',(device_id,u)).fetchone(); now=time.time()
  first_seen=old['first_seen'] if old else now; seen_count=(old['seen_count']+1) if old else 1
  emb=old['embedding'] if old else None; tb=old['thumbnail_b64'] if old else ''
  c.execute("insert or replace into clip_profiles(device_id,username,full_name,profile_url,source_url,original_image_url,thumbnail_b64,embedding,first_seen,last_seen,seen_count) values(?,?,?,?,?,?,?,?,?,?,?)",
            (device_id,u,full,profile,source_url,image,tb,emb,first_seen,now,seen_count)); c.commit()
 return True

def save_pending_bulk(device_id, records, page_url=''):
 if not records: return {'captured':0,'queued':0,'existing':0,'skipped':0}
 clean={}
 skipped=0
 for rec in records[:INGEST_MAX]:
  u=str(rec.get('username','')).strip().lstrip('@').lower()
  image=str(rec.get('image_url') or '')
  if not u or not image:
   skipped+=1; continue
  old=clean.get(u)
  if old is None or (not old.get('image_url') and image): clean[u]=rec
 if not clean: return {'captured':0,'queued':0,'existing':0,'skipped':skipped}
 if not persistent():
  queued=0
  for rec in clean.values():
   if save_pending(device_id,rec,page_url): queued+=1
  return {'captured':len(clean),'queued':queued,'existing':0,'skipped':skipped}
 names=list(clean)
 existing=set()
 for i in range(0,len(names),200):
  chunk=names[i:i+200]
  rows=supa('instagram_profiles',params={'device_id':f'eq.{device_id}','username':f"in.({','.join(chunk)})",'select':'username'})
  existing.update(r['username'] for r in rows)
 now=_iso_now(); payload=[]
 for u,rec in clean.items():
  if u in existing: continue
  payload.append({
   'device_id':device_id,'username':u,'full_name':str(rec.get('full_name') or '')[:300],
   'profile_url':str(rec.get('profile_url') or f'https://www.instagram.com/{u}/'),
   'image_url':str(rec.get('image_url') or ''),'thumbnail_base64':'','embedding':None,
   'source':str(rec.get('source') or 'manual_safari_scroll')[:100],
   'post_url':str(rec.get('post_url') or page_url or '')[:1000],
   'first_seen':now,'last_seen':now,'seen_count':1,'index_status':'queued','index_attempts':0,
   'index_error':None,'indexed_at':None
  })
 for i in range(0,len(payload),250): _bulk_upsert(payload[i:i+250])
 return {'captured':len(clean),'queued':len(payload),'existing':len(existing),'skipped':skipped}

def _download_one(row):
 try:
  return row, download_avatar(str(row.get('image_url') or '')), None
 except Exception as e:
  return row, None, str(e)[:240]

def _mark_failed(row, message):
 global LAST_ERROR
 LAST_ERROR=f"{row.get('username')}: {message}"
 attempts=int(row.get('index_attempts') or 0)+1
 status='failed' if attempts < 4 else 'dead'
 supa('instagram_profiles','PATCH',{'index_status':status,'index_attempts':attempts,'index_error':message},
      {'device_id':f"eq.{row['device_id']}",'username':f"eq.{row['username']}"})

def _mark_indexed(rows, datas, embeddings):
 now=_iso_now(); out=[]
 for row,data,emb in zip(rows,datas,embeddings):
  x=dict(row)
  x['thumbnail_base64']=thumb(data)
  x['embedding']='['+','.join(str(float(v)) for v in emb)+']'
  x['index_status']='indexed'; x['index_attempts']=int(row.get('index_attempts') or 0)+1
  x['index_error']=None; x['indexed_at']=now; x['last_seen']=row.get('last_seen') or now
  # Remove helper/unknown aliases if any.
  for k in ['source_url','original_image_url','thumbnail_b64']:
   x.pop(k,None)
  out.append(x)
 _bulk_upsert(out)
 with INDEX_LOCK:
  t=time.time()
  RATE_EVENTS.extend([t]*len(out))

def _rate():
 now=time.time()
 with INDEX_LOCK:
  while RATE_EVENTS and now-RATE_EVENTS[0] > 60: RATE_EVENTS.popleft()
  if len(RATE_EVENTS)<2: return 0.0
  span=max(now-RATE_EVENTS[0],1.0)
  return len(RATE_EVENTS)/span

def index_worker():
 global PROCESSING, LAST_ERROR
 pool=concurrent.futures.ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS)
 while True:
  try:
   if not persistent():
    time.sleep(2); continue
   rows=_pending_rows(BATCH_SIZE)
   if not rows:
    time.sleep(POLL_SECONDS); continue
   _claim_rows(rows)
   with INDEX_LOCK: PROCESSING=len(rows)
   results=list(pool.map(_download_one,rows))
   good_rows=[]; datas=[]
   for row,data,err in results:
    if err or data is None: _mark_failed(row,err or 'avatar download failed')
    else: good_rows.append(row); datas.append(data)
   if good_rows:
    try:
     embeddings=image_embeddings(datas)
     _mark_indexed(good_rows,datas,embeddings)
    except Exception as e:
     for row in good_rows: _mark_failed(row,f'CLIP batch: {str(e)[:180]}')
  except Exception as e:
   LAST_ERROR=str(e)[:240]
   time.sleep(2)
  finally:
   with INDEX_LOCK: PROCESSING=0

def recover_stale_jobs():
 if not persistent(): return
 # Render can die while a batch is marked indexing. Treat all such rows as queued on process start.
 try:
  supa('instagram_profiles','PATCH',{'index_status':'queued'}, {'embedding':'is.null','index_status':'eq.indexing'})
 except Exception:
  pass

@app.on_event('startup')
def start_index_worker():
 recover_stale_jobs()
 if not any(t.name=='clip-index-worker' and t.is_alive() for t in threading.enumerate()):
  threading.Thread(target=index_worker,name='clip-index-worker',daemon=True).start()

def download_avatar(url):
 r=HTTP.get(url,headers={'User-Agent':'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1','Referer':'https://www.instagram.com/'},timeout=12)
 r.raise_for_status();
 if len(r.content)>8_000_000: raise ValueError('image too large')
 return r.content

def thumb(data):
 im=Image.open(io.BytesIO(data)).convert('RGB'); im.thumbnail((160,160)); out=io.BytesIO(); im.save(out,'JPEG',quality=70); return base64.b64encode(out.getvalue()).decode()

def vec(x):
 if isinstance(x,list): return np.asarray(x,dtype=np.float32)
 return np.asarray(json.loads(x),dtype=np.float32)

def result_row(r,score=None):
 x={k:r.get(k) for k in ['username','full_name','profile_url','source_url','original_image_url','seen_count','first_seen','last_seen']}; x['image_data_url']='data:image/jpeg;base64,'+(r.get('thumbnail_b64') or '') if r.get('thumbnail_b64') else ''
 if score is not None: x['score']=round(float(score),5)
 return x

class Ingest(BaseModel):
 device_id:str=Field(min_length=4,max_length=100); records:list[dict[str,Any]]=Field(default_factory=list,max_length=1000); page_url:str|None=None; message:str|None=None

class ShortcutIngest(BaseModel):
 payload:str=Field(min_length=2,max_length=1000000); device_id:str=Field(default='iphone',min_length=1,max_length=100)

class BulkImport(BaseModel):
 device_id:str=Field(default='iphone',min_length=1,max_length=100)
 text:str=Field(min_length=1,max_length=5000000)

class HtmlImport(BaseModel):
 device_id:str=Field(default='iphone',min_length=1,max_length=100)
 html:str=Field(min_length=1,max_length=15000000)

class BrowserIngest(BaseModel):
 device_id:str=Field(default='iphone',min_length=1,max_length=100)
 records:list[dict[str,Any]]=Field(default_factory=list,max_length=1000)
 page_url:str|None=Field(default=None,max_length=2000)


_USERNAME_RE=re.compile(r'^[A-Za-z0-9._]{1,64}$')
_IG_URL_RE=re.compile(r'https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,64})(?:/|$)',re.I)
_IMG_URL_RE=re.compile(r'https?://[^\s,"\']+',re.I)

def _normalize_username(value):
 v=str(value or '').strip()
 m=_IG_URL_RE.search(v)
 if m: v=m.group(1)
 v=v.lstrip('@').strip().lower()
 return v if _USERNAME_RE.fullmatch(v) else ''

def _parse_bulk_text(text):
 found={}
 # JSON exported from this app/scraper is supported directly.
 try:
  obj=json.loads(text)
  rows=obj.get('records',obj) if isinstance(obj,dict) else obj
  if isinstance(rows,list):
   for r in rows:
    if not isinstance(r,dict): continue
    u=_normalize_username(r.get('username') or r.get('profile_url'))
    if not u: continue
    rec={'username':u,'profile_url':str(r.get('profile_url') or f'https://www.instagram.com/{u}/'),
         'image_url':str(r.get('image_url') or r.get('original_image_url') or ''),
         'full_name':str(r.get('full_name') or r.get('name') or ''),'post_url':str(r.get('post_url') or '')}
    old=found.get(u)
    if not old or (not old.get('image_url') and rec.get('image_url')): found[u]=rec
   if found: return list(found.values())
 except Exception:
  pass

 # Text lines: username, @username, profile URL, or username<TAB>avatar_url.
 for raw in text.splitlines():
  line=raw.strip()
  if not line: continue
  ig=_IG_URL_RE.search(line)
  first=line.split()[0] if line.split() else line
  u=_normalize_username(ig.group(0) if ig else first)
  if not u: continue
  image=''
  urls=_IMG_URL_RE.findall(line)
  for url in urls:
   if 'instagram.com/'+u in url.lower(): continue
   if any(x in url.lower() for x in ('cdninstagram','fbcdn','scontent')) or re.search(r'\.(?:jpe?g|png|webp)(?:\?|$)',url,re.I):
    image=url; break
  rec={'username':u,'profile_url':f'https://www.instagram.com/{u}/','image_url':image,'full_name':'','post_url':''}
  old=found.get(u)
  if not old or (not old.get('image_url') and image): found[u]=rec
 return list(found.values())

def _resolve_avatar(rec):
 if rec.get('image_url'): return rec, None
 u=rec['username']; url=f'https://www.instagram.com/{u}/'
 try:
  r=HTTP.get(url,headers={
   'User-Agent':'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1',
   'Accept-Language':'en-US,en;q=0.9'
  },timeout=15)
  r.raise_for_status()
  body=html.unescape(r.text)
  patterns=[
   r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
   r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
   r'"profile_pic_url_hd"\s*:\s*"([^"]+)"',
   r'"profile_pic_url"\s*:\s*"([^"]+)"'
  ]
  image=''
  for pat in patterns:
   m=re.search(pat,body,re.I)
   if m:
    image=m.group(1).replace(r'\/','/').replace(r'\u0026','&')
    break
  if not image: return rec, 'profile image not exposed by Instagram'
  out=dict(rec); out['image_url']=image
  # Best-effort full name from og:title.
  mt=re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',body,re.I)
  if mt and not out.get('full_name'): out['full_name']=html.unescape(mt.group(1))[:300]
  return out,None
 except Exception as e:
  return rec,str(e)[:180]


class _InstagramHTMLParser(HTMLParser):
 def __init__(self):
  super().__init__(convert_charrefs=True)
  self.records={}
  self.anchor_stack=[]
  self.blocked={'explore','reels','accounts','direct','stories','p','reel','tv','about','developer','legal','privacy','terms','web','emails','challenge','api','directory','push','settings'}

 def _username(self, href):
  if not href: return ''
  try:
   m=re.search(r'(?:https?://(?:www\.)?instagram\.com)?/([A-Za-z0-9._]{1,64})(?:/|(?:\?|#|$))',href,re.I)
   if not m: return ''
   u=m.group(1).lower()
   if u in self.blocked: return ''
   return u if re.fullmatch(r'[a-z0-9._]{1,64}',u,re.I) else ''
  except Exception:
   return ''

 def handle_starttag(self, tag, attrs):
  d=dict(attrs)
  if tag=='a':
   u=self._username(d.get('href',''))
   self.anchor_stack.append(u)
   if u and u not in self.records:
    self.records[u]={'username':u,'profile_url':f'https://www.instagram.com/{u}/','image_url':'','full_name':'','post_url':''}
  elif tag=='img':
   src=d.get('src') or d.get('data-src') or ''
   alt=(d.get('alt') or '').strip()
   if self.anchor_stack:
    u=self.anchor_stack[-1]
    if u and u in self.records and src:
     rec=self.records[u]
     if not rec.get('image_url'):
      rec['image_url']=src
     if alt and not rec.get('full_name'):
      # Keep alt text only when it looks person/profile-like, not huge post captions.
      if len(alt)<=160:
       rec['full_name']=alt

 def handle_endtag(self, tag):
  if tag=='a' and self.anchor_stack:
   self.anchor_stack.pop()

def _extract_html_profiles(raw_html):
 parser=_InstagramHTMLParser()
 try:
  parser.feed(raw_html)
 except Exception:
  pass
 found=parser.records

 # Fallback: mine embedded JSON / escaped HTML for profile links and avatar URLs.
 for m in re.finditer(r'(?:https?:\\/\\/www\.instagram\.com\\/|https?://www\.instagram\.com/|href=["\']\/)([A-Za-z0-9._]{1,64})(?:\\/|/|["\'])',raw_html,re.I):
  u=m.group(1).lower()
  if u in parser.blocked or not re.fullmatch(r'[a-z0-9._]{1,64}',u,re.I): continue
  found.setdefault(u,{'username':u,'profile_url':f'https://www.instagram.com/{u}/','image_url':'','full_name':'','post_url':''})

 # Try to associate CDN image URLs found close to a username occurrence.
 # This is best-effort only; the resolver can fill remaining avatars.
 cdn_re=re.compile(r'https?(?::|%3A)?(?:\\\\?/){2}[^"\s<]*(?:cdninstagram|fbcdn|scontent)[^"\s<]*',re.I)
 for u,rec in list(found.items()):
  if rec.get('image_url'): continue
  pos=raw_html.lower().find(u.lower())
  if pos<0: continue
  chunk=raw_html[max(0,pos-2500):pos+5000]
  mm=cdn_re.search(chunk)
  if mm:
   img=html.unescape(mm.group(0)).replace(r'\/','/').replace(r'\u0026','&')
   rec['image_url']=img

 return list(found.values())

@app.get('/',response_class=HTMLResponse)
def home(): return (BASE/'web'/'index.html').read_text()
@app.get('/health')
def health(): return {'ok':True,'version':'7.0-turbo','persistent':persistent(),'clip':'openai/clip-vit-base-patch32 ONNX quantized'}
@app.get('/api/config')
def config(): return {'persistent':persistent(),'clip_enabled':True,'supabase_configured':persistent()}

def ingest_records(device_id:str, records:list[dict[str,Any]], page_url:str|None=None):
 try:
  out=save_pending_bulk(device_id,records,page_url or '')
  return {**out,'failed_to_queue':0,'errors':[],'persistent':persistent()}
 except Exception as e:
  return {'captured':0,'queued':0,'existing':0,'skipped':0,'failed_to_queue':len(records),'errors':[str(e)[:240]],'persistent':persistent()}

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


@app.post('/api/bulk/import')
def bulk_import(req:BulkImport):
 records=_parse_bulk_text(req.text)
 if not records:
  raise HTTPException(400,'No Instagram usernames or profile URLs were found')
 if len(records)>10000:
  raise HTTPException(400,'Maximum 10,000 unique profiles per import')
 ready=[r for r in records if r.get('image_url')]
 unresolved=[r for r in records if not r.get('image_url')]
 resolved=[]; resolve_errors=[]
 if unresolved:
  with concurrent.futures.ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS,16)) as pool:
   for rec,err in pool.map(_resolve_avatar,unresolved):
    if err: resolve_errors.append({'username':rec['username'],'error':err})
    else: resolved.append(rec)
 queueable=ready+resolved
 result=ingest_records(req.device_id,queueable,'bulk_import')
 return {
  'parsed':len(records),
  'with_avatar_already':len(ready),
  'avatars_resolved':len(resolved),
  'unresolved':len(resolve_errors),
  'captured':result.get('captured',0),
  'queued':result.get('queued',0),
  'failed_to_queue':result.get('failed_to_queue',0),
  'sample_errors':resolve_errors[:20]
 }


@app.post('/api/html/import')
def html_import(req:HtmlImport):
 records=_extract_html_profiles(req.html)
 if not records:
  raise HTTPException(400,'No Instagram profile links were found in the pasted HTML')
 if len(records)>10000:
  records=records[:10000]
 ready=[r for r in records if r.get('image_url')]
 unresolved=[r for r in records if not r.get('image_url')]
 resolved=[]; resolve_errors=[]
 if unresolved:
  with concurrent.futures.ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS,16)) as pool:
   for rec,err in pool.map(_resolve_avatar,unresolved):
    if err: resolve_errors.append({'username':rec['username'],'error':err})
    else: resolved.append(rec)
 queueable=ready+resolved
 result=ingest_records(req.device_id,queueable,'html_import')
 return {
  'extracted':len(records),
  'avatars_in_html':len(ready),
  'avatars_resolved':len(resolved),
  'unresolved':len(resolve_errors),
  'queued':result.get('queued',0),
  'captured':result.get('captured',0),
  'sample_errors':resolve_errors[:20]
 }


@app.post('/api/browser/ingest')
def browser_ingest(req:BrowserIngest):
 # De-dupe this incoming batch first.
 unique={}
 for r in req.records:
  u=_normalize_username(r.get('username') or r.get('profile_url'))
  if not u: continue
  rec=dict(r)
  rec['username']=u
  rec['profile_url']=str(rec.get('profile_url') or f'https://www.instagram.com/{u}/')
  old=unique.get(u)
  if not old or (not old.get('image_url') and rec.get('image_url')):
   unique[u]=rec
 records=list(unique.values())
 if not records:
  return {'received':0,'captured':0,'queued':0,'resolved':0,'unresolved':0}

 ready=[r for r in records if r.get('image_url')]
 missing=[r for r in records if not r.get('image_url')]
 resolved=[]; errors=[]
 if missing:
  with concurrent.futures.ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS,16)) as pool:
   for rec,err in pool.map(_resolve_avatar,missing):
    if err: errors.append({'username':rec['username'],'error':err})
    else: resolved.append(rec)

 result=ingest_records(req.device_id,ready+resolved,req.page_url or 'browser_scroll')
 return {
  'received':len(records),
  'captured':result.get('captured',0),
  'queued':result.get('queued',0),
  'resolved':len(resolved),
  'unresolved':len(errors),
  'sample_errors':errors[:10]
 }

@app.get('/api/browser/ping')
def browser_ping():
 return {'ok':True,'version':'7.0-turbo'}

@app.get('/api/live/stats')
def stats(device_id:str):
 if persistent():
  rows=supa('instagram_profiles',params={'device_id':f'eq.{device_id}','select':'index_status'})
  captured=len(rows); indexed=sum(1 for r in rows if r.get('index_status')=='indexed')
  queued=sum(1 for r in rows if r.get('index_status') in (None,'queued'))
  failed=sum(1 for r in rows if r.get('index_status') in ('failed','dead'))
 else:
  rows=fetch_all(device_id); captured=len(rows); indexed=sum(1 for r in rows if r.get('embedding')); queued=max(captured-indexed,0); failed=0
 with INDEX_LOCK:
  processing=PROCESSING; last_error=LAST_ERROR
 rate=_rate(); remaining=max(captured-indexed-failed,0); eta=(remaining/rate if rate>0 else None)
 return {'profiles_collected':captured,'captured':captured,'queued':queued,'processing':processing,
         'indexed':indexed,'failed':failed,'last_error':last_error,'persistent':persistent(),
         'rate_per_sec':round(rate,2),'eta_seconds':round(eta) if eta is not None else None,
         'batch_size':BATCH_SIZE,'download_workers':DOWNLOAD_WORKERS,
         'message':('CLIP indexing in progress' if remaining else 'CLIP library ready')}
@app.get('/api/live/profiles')
def profiles(device_id:str,limit:int=100): return {'profiles':[result_row(r) for r in fetch_all(device_id)[:min(limit,500)]]}
@app.delete('/api/live/profiles')
def clear(device_id:str):
 if persistent(): supa('instagram_profiles','DELETE',params={'device_id':f'eq.{device_id}'})
 else:
  with db() as c: c.execute('delete from clip_profiles where device_id=?',(device_id,)); c.commit()
 return {'ok':True}

def _rpc_match(device_id, embedding, limit):
 payload={'query_embedding':'['+','.join(str(float(v)) for v in embedding)+']','match_device_id':device_id,'match_count':min(limit,200)}
 try:
  rows=supa('rpc/match_instagram_profiles','POST',payload)
  out=[]
  for r in rows:
   x={'username':r.get('username'),'full_name':r.get('full_name'),'profile_url':r.get('profile_url'),
      'source_url':r.get('post_url') or '','original_image_url':r.get('image_url') or '',
      'seen_count':r.get('seen_count'),'first_seen':r.get('first_seen'),'last_seen':r.get('last_seen'),
      'image_data_url':'data:image/jpeg;base64,'+(r.get('thumbnail_base64') or '') if r.get('thumbnail_base64') else '',
      'score':round(float(r.get('similarity') or 0),5)}
   out.append(x)
  return out
 except Exception:
  return None

@app.get('/api/clip/search/text')
def search_text(device_id:str,q:str,limit:int=50):
 if not q.strip():
  rows=fetch_all(device_id); return {'profiles':[result_row(r) for r in rows[:limit]]}
 qv=text_embedding(q.strip())
 matched=_rpc_match(device_id,qv,limit)
 if matched is not None: return {'profiles':matched,'query':q,'semantic':True,'engine':'supabase_vector'}
 rows=[r for r in fetch_all(device_id) if r.get('embedding')]
 qn=np.asarray(qv,dtype=np.float32); scored=[(float(np.dot(qn,vec(r['embedding']))),r) for r in rows]; scored.sort(key=lambda x:x[0],reverse=True)
 return {'profiles':[result_row(r,sc) for sc,r in scored[:min(limit,200)]],'query':q,'semantic':True,'engine':'fallback'}

@app.post('/api/clip/search/image')
async def search_image(device_id:str,file:UploadFile=File(...),limit:int=50):
 data=await file.read(); qv=image_embedding(data)
 matched=_rpc_match(device_id,qv,limit)
 if matched is not None: return {'profiles':matched,'semantic':True,'engine':'supabase_vector'}
 rows=[r for r in fetch_all(device_id) if r.get('embedding')]; qn=np.asarray(qv,dtype=np.float32); scored=[(float(np.dot(qn,vec(r['embedding']))),r) for r in rows]; scored.sort(key=lambda x:x[0],reverse=True)
 return {'profiles':[result_row(r,sc) for sc,r in scored[:min(limit,200)]],'semantic':True,'engine':'fallback'}
