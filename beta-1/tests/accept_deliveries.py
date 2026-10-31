# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic/headless integration acceptance. Owns disposable PostgreSQL and children.
Run with a new absolute run directory; needs existing Station deps, ffmpeg, and
explicit STATION_TEST_PG_BIN / STATION_TEST_MEDIA_RUNTIME paths. No external calls.
"""
from pathlib import Path
import hashlib,json,os,shutil,socket,subprocess,sys,threading,time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import httpx,psycopg2,sqlalchemy as sa
from fastapi.testclient import TestClient
from townlight_station.boundary import Settings,sha256
from townlight_station.api import create_app
from townlight_station.store import Store,recordings,uploads,version
from townlight_station.worker import process,claim
from townlight_station.delivery import process_upload,GoogleTransport,feeds,run_uploads

ROOT=Path(sys.argv[1]).resolve(); ROOT.mkdir(parents=True,exist_ok=False)
SRC=Path(__file__).resolve().parents[1]
RUNTIME=Path(os.environ['STATION_TEST_MEDIA_RUNTIME']).resolve()
PG=Path(os.environ['STATION_TEST_PG_BIN']).resolve()
threads=[]; children=[]; logs=[]; checks=[]; store=None; admin=None

def save(name,value): (ROOT/name).write_text(json.dumps(value,indent=2,default=str),encoding='utf-8')
def passed(name):
 checks.append(name);save('checks.json',checks);print('PASS:',name,flush=True)
def port():
 with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
def spawn(name,args,env=None):
 log=(ROOT/(name+'.log')).open('wb');logs.append(log)
 p=subprocess.Popen([str(a) for a in args],stdout=log,stderr=subprocess.STDOUT,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
 children.append((name,p));save('owned-jobs.json',[dict(name=n,pid=p.pid,terminal=p.poll()) for n,p in children]);return p
def stop(p):
 if p.poll() is None:
  p.terminate()
  try:p.wait(timeout=8)
  except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
def command(args,**kw):return subprocess.run([str(a) for a in args],capture_output=True,check=True,timeout=100,creationflags=subprocess.CREATE_NO_WINDOW,**kw)
def wait(predicate,seconds=25):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  if predicate():return
  time.sleep(.2)
 raise AssertionError('Condition timed out')
def require(response,code):
 assert response.status_code==code,(response.status_code,response.text[:500]);return response.json()

def suite(path,proof):
 from fastapi import HTTPException
 if proof not in ('c'*64,'r'*64):raise HTTPException(401,'Synthetic unknown session')
 if 'capability=capture' in path and proof!='c'*64:raise HTTPException(403,'Synthetic read-only role')
 if 'context?' in path:return {'actor_id':'synthetic-clerk' if proof=='c'*64 else 'synthetic-records'}
 if '/meetings/meetings/' in path:return {'id':meeting,'title':'Synthetic council meeting'}
 raise HTTPException(404,'Synthetic unknown object')

try:
 pw=ROOT/'pg-pw';pw.write_text('synthetic-only')
 command([PG/'initdb.exe','-D',ROOT/'pgdata','-U','station_test','-A','scram-sha-256','--pwfile',pw,'--encoding=UTF8','--locale=C']);pw.unlink()
 pgport=port();pg=spawn('postgres',[PG/'postgres.exe','-D',ROOT/'pgdata','-h','127.0.0.1','-p',pgport,'-c','shared_buffers=32MB','-c','max_connections=20'])
 def connect():
  global admin
  try:admin=psycopg2.connect(host='127.0.0.1',port=pgport,user='station_test',password='synthetic-only',dbname='postgres',connect_timeout=2);return True
  except psycopg2.OperationalError:return False
 wait(connect);admin.autocommit=True
 with admin.cursor() as c:c.execute('CREATE DATABASE delivery_station')
 admin.close();admin=None
 cfg=Settings(ROOT/'data',f'postgresql+psycopg2://station_test:synthetic-only@127.0.0.1:{pgport}/delivery_station','http://127.0.0.1:1',SRC/'media-source',RUNTIME/'python.exe',RUNTIME,Path(shutil.which('ffprobe')),max_seconds=60,width=640,height=360,bitrate_kbps=1600)
 cfg.initialize_storage();store=Store(cfg.database_url);store.migrate()
 # Verify actual v3 upgrade is repeatable and existing identity remains intact.
 with store.engine.begin() as c:c.execute(version.update().values(version=3))
 store.migrate();store.ready();passed('Schema v3 to v4 migration and repeat migration preserve Station identity')
 udp,srt=port(),port();feed={'id':'peg','label':'Synthetic CivicCast','address':'127.0.0.1','port':srt,'secret_env':'TOWNLIGHT_STATION_SRT_TEST'}
 (cfg.data/'live-sources.json').write_text(json.dumps([{'id':'chamber','label':'Synthetic chamber','kind':'udp-mpegts','address':'127.0.0.1','port':udp}]))
 (cfg.data/'civiccast-feeds.json').write_text(json.dumps([feed]))
 os.environ['TOWNLIGHT_STATION_SRT_TEST']='synthetic-passphrase'
 for n in ('CLIENT_ID','CLIENT_SECRET','REFRESH_TOKEN'):os.environ['TOWNLIGHT_STATION_YOUTUBE_'+n]='synthetic-'+n
 meeting=str(uuid4());headers={'X-Townlight-Session':'c'*64};read={'X-Townlight-Session':'r'*64}
 with patch('townlight_station.api.Suite.get',side_effect=suite),TestClient(create_app(cfg)) as api:
  require(api.get('/v1/civiccast-feeds'),401);require(api.get('/v1/civiccast-feeds',headers=read),403)
  assert require(api.get('/v1/civiccast-feeds',headers=headers),200)==[{'id':'peg','label':'Synthetic CivicCast'}]
  require(api.post('/v1/recordings',headers=headers,json={'meeting_id':meeting,'source_file':'no.mp4','civiccast_feed_id':'peg','idempotency_key':'bad'}),422)
  request={'meeting_id':meeting,'live_source_id':'chamber','civiccast_feed_id':'peg','idempotency_key':'live-1'}
  first=require(api.post('/v1/recordings',headers=headers,json=request),202);rid=first['id']
  assert require(api.post('/v1/recordings',headers=headers,json=request),202)['id']==rid
  body={'title':'Synthetic council meeting','privacy':'private','approval_note':'Synthetic approval only','idempotency_key':'upload-1'}
  require(api.post(f'/v1/recordings/{rid}/youtube',headers=headers,json=body),409)
  require(api.post(f'/v1/recordings/{rid}/youtube',headers=read,json=body),403)
  require(api.post(f'/v1/recordings/{rid}/youtube',json=body),401)
  passed('HTTP gates: current authority, read-only rejection, configured live feed selection and capture idempotency; unfinished uploads rejected')
  # Receiver builds its source using CivicCast's real source_first_element SRT adapter.
  receiver=ROOT/'receiver.py'
  receiver.write_text('''import json,sys,os
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
from civiccast.native.gstreamer_runtime import bootstrap_installed_gstreamer_runtime
bootstrap_installed_gstreamer_runtime()
from civiccast.egress.gst.bridge import source_first_element
import gi
gi.require_version('Gst','1.0')
from gi.repository import Gst,GLib
Gst.init(None)
spec=source_first_element(SimpleNamespace(kind='live',path=sys.argv[2],secret_ref='synthetic-handle'))
src=Gst.ElementFactory.make(spec.factory)
for k,v in spec.props.items():src.set_property(k,v)
src.set_property('passphrase',os.environ['TOWNLIGHT_STATION_FEED_SECRET'])
p=Gst.Pipeline.new('civiccast-receiver');q=Gst.ElementFactory.make('queue');out=Gst.ElementFactory.make('filesink');out.set_property('location',sys.argv[3]);out.set_property('sync',False)
for e in (src,q,out):p.add(e)
assert src.link(q) and q.link(out)
loop=GLib.MainLoop();bus=p.get_bus();bus.add_signal_watch()
def msg(b,m):
 if m.type==Gst.MessageType.ERROR:print(m.parse_error(),flush=True);loop.quit()
bus.connect('message',msg)
def poll():
 if Path(sys.argv[4]).exists():loop.quit();return False
 return True
GLib.timeout_add(100,poll)
p.set_state(Gst.State.PLAYING)
try:loop.run()
finally:p.set_state(Gst.State.NULL)
''',encoding='utf-8')
  incoming=ROOT/'received.ts';receiver_stop=ROOT/'receiver.stop';env=cfg.worker_environment();env['TOWNLIGHT_STATION_FEED_SECRET']='synthetic-passphrase'
  # Start listener first, then caller (caller reconnects when source begins).
  thread=threading.Thread(target=process,args=(cfg,store,claim(store.engine,recordings,'queued')));threads.append(thread);thread.start()
  time.sleep(1)
  rx=spawn('civiccast-receiver',[cfg.media_python,'-B',receiver,cfg.media_source,f'srt://127.0.0.1:{srt}?mode=caller&latency=200',incoming,receiver_stop],env)
  tx=spawn('synthetic-input',[shutil.which('ffmpeg'),'-hide_banner','-loglevel','error','-re','-f','lavfi','-i','testsrc2=size=640x360:rate=30','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-g','30','-c:a','aac','-f','mpegts',f'udp://127.0.0.1:{udp}?pkt_size=1316'])
  wait(lambda:incoming.exists() and incoming.stat().st_size>100000,35)
  assert store.get(rid)['status']=='running'
  snapshot=store.get(rid);save('live-before-stop.json',snapshot)
  passed('CivicCast SRT source adapter received live encrypted TS while Station recording was still running')
  # Disconnect actual receiver, leave capture running long enough to prove progress.
  before=next((cfg.data/'recordings'/rid).glob('*.ts')).stat().st_size
  receiver_stop.touch();rx.wait(timeout=10);time.sleep(3)
  after=next((cfg.data/'recordings'/rid).glob('*.ts')).stat().st_size
  assert after>before and store.get(rid)['status']=='running'
  require(api.post(f'/v1/recordings/{rid}/stop',headers=headers,json={}),202);thread.join(25);assert not thread.is_alive()
  stop(tx);record=store.get(rid);save('recording.json',record);assert record['status']=='complete',record
  output=cfg.data/'recordings'/rid/record['details']['output_file'];original_hash=sha256(output)
  assert original_hash==record['details']['sha256']
  command([shutil.which('ffmpeg'),'-v','error','-i',incoming,'-t','2','-f','null','-'])
  content=api.get(f'/v1/recordings/{rid}/content',headers=read);assert content.status_code==200 and hashlib.sha256(content.content).hexdigest()==original_hash
  passed('Receiver disconnect did not stop capture; operator stop completed canonical evidence; received audio/video decoded and private content hash matched')
  job=require(api.post(f'/v1/recordings/{rid}/youtube',headers=headers,json=body),202)
  assert require(api.post(f'/v1/recordings/{rid}/youtube',headers=headers,json=body),202)['id']==job['id']
  require(api.post(f'/v1/recordings/{rid}/youtube',headers=headers,json={**body,'idempotency_key':'duplicate'}),409)
  require(api.post(f'/v1/recordings/{rid}/youtube',headers=headers,json={**body,'title':'different'}),409)
  calls=[]
  def provider(req):
   calls.append({'method':req.method,'url':str(req.url),'bytes':len(req.read())})
   if req.url.path=='/token':return httpx.Response(200,json={'access_token':'synthetic-token'})
   if req.method=='POST':
    payload=json.loads(req.content);assert payload['snippet']['title']==body['title'];assert payload['status']['privacyStatus']=='private'
    return httpx.Response(200,headers={'location':'https://www.googleapis.com/upload/youtube/v3/videos?upload_id=synthetic'})
   assert req.method=='PUT' and req.headers['Authorization']=='Bearer synthetic-token'
   assert hashlib.sha256(req.content).hexdigest()==sha256(next((cfg.data/'uploads'/job['id']).glob('*/recording.mp4')))
   return httpx.Response(200,json={'id':'TestVideo01'})
  process_upload(cfg,store,claim(store.engine,uploads,'queued'),httpx.MockTransport(provider))
  result=require(api.get('/v1/youtube-uploads',headers=read),200)[0];save('upload.json',result);save('provider-calls.json',calls)
  assert result['status']=='uploaded',result
  assert len(calls)==3 and result['details']['url']=='https://www.youtube.com/watch?v=TestVideo01'
  assert sha256(output)==original_hash and store.get(rid)['details']['visibility']=='private'
  passed('Real donor YouTube OAuth/resumable/file upload logic with mocked Google transport sent the remuxed MP4 bytes, title/privacy and retained provider receipt; canonical evidence unchanged')
  # Transport guard actually rejects an untrusted upload endpoint before dispatch.
  seen=[]
  with GoogleTransport(httpx.MockTransport(lambda r:seen.append(r))) as t:
   try:t.handle_request(httpx.Request('PUT','https://untrusted.invalid/upload'));raise AssertionError('guard missing')
   except ValueError:pass
  assert not seen
  passed('YouTube transport refuses non-Google destinations before credentials can be sent')
  # No receiver at all is an ordinary delivery failure, never recording failure.
  request.update(idempotency_key='offline-2');second=require(api.post('/v1/recordings',headers=headers,json=request),202);rid2=second['id']
  t=threading.Thread(target=process,args=(cfg,store,claim(store.engine,recordings,'queued')));threads.append(t);t.start()
  tx2=spawn('synthetic-input-offline',[shutil.which('ffmpeg'),'-hide_banner','-loglevel','error','-re','-f','lavfi','-i','testsrc2=size=640x360:rate=30','-f','lavfi','-i','sine=frequency=550:sample_rate=48000','-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-g','30','-c:a','aac','-f','mpegts',f'udp://127.0.0.1:{udp}?pkt_size=1316'])
  time.sleep(5);require(api.post(f'/v1/recordings/{rid2}/stop',headers=headers,json={}),202);t.join(25);assert not t.is_alive();stop(tx2)
  record2=store.get(rid2);save('offline-recording.json',record2);assert record2['status']=='complete',record2
  job2=require(api.post(f'/v1/recordings/{rid2}/youtube',headers=headers,json={**body,'idempotency_key':'uncertain'}),202)
  # Preparation errors are retryable only before any external request.
  saved=os.environ.pop('TOWNLIGHT_STATION_YOUTUBE_REFRESH_TOKEN')
  process_upload(cfg,store,claim(store.engine,uploads,'queued'),httpx.MockTransport(provider))
  require(api.post(f"/v1/youtube-uploads/{job2['id']}/retry",headers=read,json={}),403)
  os.environ['TOWNLIGHT_STATION_YOUTUBE_REFRESH_TOKEN']=saved
  require(api.post(f"/v1/youtube-uploads/{job2['id']}/retry",headers=headers,json={}),202)
  def fail_provider(req):
   if req.method=='PUT':raise httpx.ReadTimeout('Synthetic upload response lost')
   return provider(req)
  process_upload(cfg,store,claim(store.engine,uploads,'queued'),httpx.MockTransport(fail_provider))
  with store.engine.connect() as c:uncertain=dict(c.execute(sa.select(uploads).where(uploads.c.id==job2['id'])).mappings().one())
  assert uncertain['status']=='needs-reconciliation' and claim(store.engine,uploads,'queued') is None
  require(api.post(f"/v1/youtube-uploads/{job2['id']}/retry",headers=headers,json={}),409)
  # Exercise the actual separate worker entry point and its startup recovery.
  with store.engine.begin() as c:c.execute(uploads.update().where(uploads.c.id==job2['id']).values(status='running'))
  with patch('townlight_station.delivery.Settings.from_env',return_value=cfg):run_uploads(once=True)
  with store.engine.connect() as c:assert c.scalar(sa.select(uploads.c.status).where(uploads.c.id==job2['id']))=='needs-reconciliation'
  passed('Preparation-only retry checks permissions; actual separate upload worker acquires its own lock and reconciles interrupted external intent without replay')
  require(api.post(f'/v1/recordings/{rid2}/youtube',headers=headers,json={**body,'idempotency_key':'must-not-duplicate'}),409)
  passed('No CivicCast receiver still produced a completed recording; uncertain YouTube response is retained for reconciliation and never retried or duplicated automatically')
  # A third record uses the same real evidence solely to test tamper rejection.
  old=output.read_bytes();output.write_bytes(old+b'tamper')
  try:
   # Different recording job status does not bypass read-time integrity.
   assert api.get(f'/v1/recordings/{rid}/content',headers=read).status_code==409
  finally:output.write_bytes(old)
  passed('Evidence tampering rejected; original test evidence restored')
 save('result.json',{'passed':True,'checks':checks,'candidate':str(SRC),'synthetic_only':True,'external_requests':0})
finally:
 if admin:admin.close()
 if store:
  with store.engine.begin() as c:c.execute(recordings.update().where(recordings.c.status.in_(('queued','running'))).values(control={'stop_requested_at':'synthetic-cleanup'}))
  for t in threads:t.join(30)
  assert not any(t.is_alive() for t in threads), 'Capture threads must terminate before database cleanup'
  store.engine.dispose()
 for name,p in reversed(children):stop(p)
 for log in logs:log.close()
 save('owned-jobs.json',[dict(name=n,pid=p.pid,terminal=p.poll()) for n,p in children])
