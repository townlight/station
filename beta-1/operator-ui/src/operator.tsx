// SPDX-License-Identifier: AGPL-3.0-only
import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import './operator.css';
type Catalog = {meetings:any[]; agenda:any[]; requests:any[]; can_capture:boolean; can_attach:boolean; max_seconds:number; live_sources?:any[]};
type Air = {mode:string; live_source_id?:string; reason?:string; since?:string; returned_at?:string; air?:any};
async function api(path:string, body?:unknown){
 const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Station-UI':'1'},body:body===undefined?undefined:JSON.stringify(body)});
 const data=await response.json();if(!response.ok)throw Object.assign(new Error(data.detail||'Request failed'),{status:response.status});return data;
}
function App(){
 const [connected,setConnected]=useState(false),[message,setMessage]=useState('Connecting to your Townlight staff session…'),[error,setError]=useState(false);
 const [catalog,setCatalog]=useState<Catalog|null>(null),[sources,setSources]=useState<any[]>([]),[records,setRecords]=useState<any[]>([]);
 const [schedules,setSchedules]=useState<any[]>([]),[playouts,setPlayouts]=useState<any[]>([]),[air,setAir]=useState<Air>({mode:'schedule'});
 const [meeting,setMeeting]=useState(''),[agenda,setAgenda]=useState<string[]>([]),[source,setSource]=useState(''),[busy,setBusy]=useState(false);
 const [sourceFile,setSourceFile]=useState(''),[captionsFile,setCaptionsFile]=useState(''),[startsAt,setStartsAt]=useState('');
 const [onAirSource,setOnAirSource]=useState(''),[reason,setReason]=useState('');
 const pending=useRef<any>(null), refreshing=useRef(false);
 const tell=(text:string,bad=false)=>{setMessage(text);setError(bad);};
 const fail=(e:any)=>{tell(e.message,true);if(e.status===401)setConnected(false);};
 // Polling must not re-render identical state: a fresh object every 2s detaches
 // the DOM node a staff member is clicking (measured: an acceptance click retried
 // until it timed out because the poll kept replacing the button).
 const last=useRef({records:'',schedules:'',playouts:'',air:''});
 const replace=(key:string,value:any,set:(v:any)=>void)=>{
  const serialized=JSON.stringify(value);
  if(last.current[key]===serialized)return;
  last.current[key]=serialized;set(value);
 };
 async function refresh(){
  if(refreshing.current)return;refreshing.current=true;
  try{
   const views=await Promise.all([api('/v1/recordings'),api('/v1/schedules'),api('/v1/playouts'),api('/v1/on-air')]);
   replace('records',views[0],setRecords);replace('schedules',views[1],setSchedules);
   replace('playouts',views[2],setPlayouts);replace('air',views[3],setAir);
  }catch(e){fail(e);}finally{refreshing.current=false;}
 }
 useEffect(()=>{let alive=true;(async()=>{try{
 const ticket=new URLSearchParams(location.hash.slice(1)).get('ticket');history.replaceState(null,'',location.pathname);
 if(ticket)await api('/operator/session',{ticket});await api('/v1/identity');const data=await api('/operator/catalog');
 if(!alive)return;setCatalog(data);if(data.can_capture){const inputs=await api('/v1/live-sources');setSources(inputs);setSource(inputs[0]?.id||'');setOnAirSource(inputs[0]?.id||'');}
 setConnected(true);tell(data.can_capture?'Select a meeting and live input to begin.':'Select a completed recording and Records request.');
 await refresh();
 }catch(e){fail(e);}})();return()=>{alive=false;};},[]);
 useEffect(()=>{if(!connected)return;const timer=setInterval(refresh,2000);return()=>clearInterval(timer);},[connected]);
 async function start(e:React.FormEvent){e.preventDefault();if(busy)return;setBusy(true);try{
 const values={meeting_id:meeting,agenda_ids:agenda,live_source_id:source};
 if(!pending.current||JSON.stringify(pending.current.values)!==JSON.stringify(values))pending.current={values,key:crypto.randomUUID()};
 await api('/v1/recordings',{...values,idempotency_key:pending.current.key});pending.current=null;tell('Recording accepted. Watch its status below.');await refresh();
 }catch(e){fail(e);}finally{setBusy(false);}}
 async function stop(id:string){try{await api(`/v1/recordings/${id}/stop`,{});tell('Stop requested. Station is finishing and verifying the recording.');await refresh();}catch(e){fail(e);}}
 async function attach(id:string,hash:string,request:string){try{const receipt=await api('/operator/handoff',{request_id:request,recording_id:id,expected_sha256:hash});tell(`Attached privately to Records. Request ${receipt.request.id} has ${receipt.request.documents.length} document(s).`);}catch(e){fail(e);}}
 async function act(label:string, path:string, body:unknown, done:string){
  if(busy)return;setBusy(true);
  try{await api(path,body);tell(done);await refresh();}catch(e){fail(e);}finally{setBusy(false);}
 }
 const active=records.some(r=>['running','queued'].includes(r.status));
 const scheduleKey=useRef(crypto.randomUUID()),play=(id:string,mode:string,busyFlag:boolean)=>act(
  mode==='commit'?'Commit to air':'Check playout',"/v1/playouts",{recording_id:id,mode,idempotency_key:crypto.randomUUID()},
  mode==='commit'?'Playout queued. Station will render and switch to it.':'Playout dry run queued.');
 return <><header><div><span className="eyebrow">TOWNLIGHT</span><h1>Station</h1><p>Meeting recordings · Schedules · Playout · Staff workspace</p></div>{connected&&<button onClick={async()=>{try{await api('/operator/logout',{});setConnected(false);setRecords([]);tell('Signed out. Reopen Station from your signed-in Townlight session.');}catch(e){fail(e);}}}>Sign out of Station</button>}</header>
 <main><p id="message" role="status" aria-live="polite" className={error?'error':''}>{message}{!connected&&' Reopen Station from your signed-in Townlight session.'}</p>
 {connected&&catalog?.can_capture&&<section id="on-air"><div className="section-heading"><h2>On air</h2><span id="on-air-mode" className="status" data-mode={air.mode}>{air.mode==='live'?`Live override (${air.live_source_id})`:'Scheduled program'}</span></div>
 <p>Station airs the newest verified committed playout unless you take a live override. Returning to schedule puts the scheduled program back.</p>
 {air.mode==='live'&&<form onSubmit={e=>{e.preventDefault();act('return','/v1/on-air/return-to-schedule',{},'Returned to the scheduled program.');}}><button id="return-to-schedule" disabled={busy}>Return to schedule</button></form>}
 {air.mode!=='live'&&<form onSubmit={e=>{e.preventDefault();act('live','/v1/on-air/live',{live_source_id:onAirSource,reason},'Live override is on air. The scheduled program is superseded, not deleted.');}}>
 <label>Live input<select id="on-air-source" required value={onAirSource} onChange={e=>setOnAirSource(e.target.value)}>{sources.map(s=><option key={s.id} value={s.id}>{s.label}</option>)}</select></label>
 <label>Reason<input id="on-air-reason" required minLength={3} maxLength={200} value={reason} placeholder="e.g. Council returns from recess" onChange={e=>setReason(e.target.value)}/></label>
 <button id="go-live" disabled={!onAirSource||reason.trim().length<3||busy}>Take live override</button></form>}
 {!sources.length&&<p>No live input configured. Ask your Station administrator to configure the chamber feed.</p>}</section>}
 {connected&&catalog?.can_capture&&<section id="capture"><h2>Record a meeting</h2><p>Select the meeting and chamber feed. Recordings remain private for Records review.</p>
 <form id="capture-form" onSubmit={start}><label>Meeting<select id="meeting-id" required value={meeting} onChange={e=>{setMeeting(e.target.value);setAgenda([]);}}><option value="">Choose a meeting</option>{catalog.meetings.map(m=><option key={m.id} value={m.id}>{m.title}</option>)}</select></label>
 <label>Live input<select id="source" required value={source} onChange={e=>setSource(e.target.value)}>{sources.map(s=><option key={s.id} value={s.id}>{s.label}</option>)}</select></label><button id="start" disabled={!meeting||!source||active||busy}>Start recording</button></form>
 {catalog.agenda.filter(a=>a.meeting_id===meeting).map(a=><label className="agenda-choice" key={a.id}><input type="checkbox" checked={agenda.includes(a.id)} onChange={e=>setAgenda(e.target.checked?[...agenda,a.id]:agenda.filter(id=>id!==a.id))}/>{a.title}</label>)}
 {!sources.length&&<p>No live input configured. Ask your Station administrator to configure the chamber feed.</p>}
 <p>Automatic stop after {Math.round(catalog.max_seconds/60*10)/10} minutes. Start another recording if the meeting continues.</p></section>}
 {connected&&catalog?.can_capture&&<section id="schedules"><div className="section-heading"><h2>Schedules</h2><button onClick={refresh}>Refresh</button></div>
 <p>A schedule is durable intent: Station starts it even across a restart, and only once. A schedule whose start time has passed beyond the grace window is marked missed, never aired late.</p>
 <form id="schedule-form" onSubmit={e=>{e.preventDefault();act('schedule','/v1/schedules',{meeting_id:meeting,agenda_ids:agenda,source_file:sourceFile||null,captions_file:captionsFile||null,start_at:new Date(startsAt).toISOString(),idempotency_key:scheduleKey.current},'Schedule accepted.').then(()=>{scheduleKey.current=crypto.randomUUID();});}}>
 <label>Meeting<select id="schedule-meeting" required value={meeting} onChange={e=>{setMeeting(e.target.value);setAgenda([]);}}><option value="">Choose a meeting</option>{catalog.meetings.map(m=><option key={m.id} value={m.id}>{m.title}</option>)}</select></label>
 <label>Start<input id="schedule-start" type="datetime-local" required value={startsAt} onChange={e=>setStartsAt(e.target.value)}/></label>
 <label>Source file in managed ingest<input id="schedule-source" value={sourceFile} placeholder="synthetic-program.mp4" onChange={e=>setSourceFile(e.target.value)}/></label>
 <label>Caption sidecar<input id="schedule-captions" value={captionsFile} placeholder="synthetic-captions.srt" onChange={e=>setCaptionsFile(e.target.value)}/></label>
 <button id="schedule-create" disabled={!meeting||!startsAt||!sourceFile||busy}>Schedule</button></form>
 {!schedules.length&&<p>No schedules yet.</p>}
 {schedules.map(s=><article key={s.id} data-schedule={s.id}><h3>{s.details?.meeting?.title||s.meeting_id}</h3><p className="status">{s.status}</p><p>Starts {new Date(s.start_at).toLocaleString()}</p>
 {s.status==='scheduled'&&<button disabled={busy} onClick={()=>act('cancel',`/v1/schedules/${s.id}/cancel`,{},'Schedule cancelled; it will not start.')}>Cancel schedule</button>}
 {s.status==='dispatched'&&s.details?.recording_id&&<p>Recording {s.details.recording_id}</p>}</article>)}</section>}
 {connected&&<section id="recordings"><div className="section-heading"><h2>Recordings</h2><button onClick={refresh}>Refresh</button></div><p>Status updates automatically. Stop finishes and verifies media before Records can accept it.</p>
 {!records.length&&<p>No recordings yet.</p>}{records.map(r=><Recording key={r.id} record={r} catalog={catalog!} stop={stop} attach={attach} play={play} busy={busy}/>)}</section>}
 {connected&&<section id="playouts"><div className="section-heading"><h2>Playout and playback</h2><button onClick={refresh}>Refresh</button></div>
 <p>A playout renders a completed recording for broadcast. Nothing becomes public until a supervisor approves it and a staff member explicitly publishes it; publication packages the program into a browser-playable MP4 (with the supplied captions as a WebVTT track).</p>
 {!playouts.length&&<p>No playouts yet.</p>}
 {playouts.map(p=><article key={p.id} data-playout={p.id}><h3>{p.mode==='dry-run'?'Playout check':'Commit to air'}</h3><p className="status">{p.status}</p>
 {p.error&&<p className="error">Playout failed: {p.error}. Retained attempt log requires review.</p>}
 {p.status==='complete'&&p.mode!=='dry-run'&&<>
  <p>{p.duration_seconds?`${p.duration_seconds.toFixed(1)} seconds · `:''}{p.sha256?'Verified broadcast output':'Planned only'}</p>
  {p.approved?<p>Approved by {p.approved.actor_id} — “{p.approved.note}”</p>:<form onSubmit={e=>{e.preventDefault();const note=(e.target as any).note.value;act('approve',`/v1/playouts/${p.id}/approve`,{note},'Approved for public playback.');}}>
   <label>Approval note<input name="note" required minLength={3} maxLength={500} placeholder="e.g. Verified against the meeting record"/></label>
   <button disabled={busy}>Approve for public playback</button></form>}
  {p.published?<p><a href={`/v1/public/playback/${p.id}`} target="_blank" rel="noreferrer">Open public playback</a></p>
   :<form onSubmit={e=>{e.preventDefault();act('publish',`/v1/playouts/${p.id}/publish`,{},'Published. Public playback now serves the packaged MP4.');}}><button id="publish" disabled={!p.approved||busy}>Publish locally</button></form>}
  {p.playback_ready&&<><h4>Public playback</h4>
   <video id={`player-${p.id}`} data-playout-player={p.id} controls preload="auto" width="640" src={`/v1/public/playback/${p.id}`}>
    {p.published?.captions_file&&<track kind="captions" src={`/v1/public/playback/${p.id}/captions`} srcLang="en" label="Captions" default/>}
   </video><p>Playback shown here is the published program. If captions were supplied, they are included as a WebVTT track.</p></>}
 </>}</article>)}</section>}
 </main><footer>Townlight Station · AGPL 3.0 · Private staff evidence</footer></>;
}
function Recording({record:r,catalog,stop,attach,play,busy}:{record:any;catalog:Catalog;stop:(id:string)=>Promise<void>;attach:(id:string,hash:string,request:string)=>Promise<void>;play:(id:string,mode:string,busy:boolean)=>void;busy:boolean}){
 const [request,setRequest]=useState(''),[local,setLocal]=useState(false);
 const status=r.control.stop_requested_at&&['queued','running'].includes(r.status)?'Stopping — finishing and verifying media':r.status==='running'?(r.live_progress?.bytes_written>0?'Recording live feed':'Starting — waiting for live input'):r.status;
 return <article data-id={r.id}><h3>{r.details.meeting.title}</h3><p className="status">{status}</p>
 {r.status==='running'&&r.live_progress?.bytes_written>0&&<p>{(r.live_progress.bytes_written/1048576).toFixed(2)} MiB captured</p>}
 {catalog.can_capture&&r.details.live_source&&['queued','running'].includes(r.status)&&<button disabled={busy||local||!!r.control.stop_requested_at} onClick={async()=>{setLocal(true);try{await stop(r.id);}finally{setLocal(false);}}}>Stop recording</button>}
 {['failed','interrupted'].includes(r.status)&&<p>Recording did not complete. Partial evidence is retained. Start a new live recording; missing live time cannot be recovered.</p>}
 {r.status==='complete'&&<><p>{r.details.duration_seconds.toFixed(1)} seconds · {(r.details.size_bytes/1048576).toFixed(2)} MiB · Verified private evidence</p>
 {catalog.can_capture&&<div className="section-heading"><button disabled={busy} onClick={()=>play(r.id,'dry-run',busy)}>Check playout</button><button id="commit-to-air" disabled={busy} onClick={()=>play(r.id,'commit',busy)}>Commit to air</button></div>}
 {catalog.can_attach?<form onSubmit={async e=>{e.preventDefault();setLocal(true);try{await attach(r.id,r.details.sha256,request);}finally{setLocal(false);}}}><label>Records request<select aria-label="Records request" required value={request} onChange={e=>setRequest(e.target.value)}><option value="">Choose a request</option>{catalog.requests.map(q=><option key={q.id} value={q.id}>{q.summary}</option>)}</select></label><button disabled={!request||busy||local}>Attach privately to Records</button></form>:<p>Records staff can attach this recording after opening Station with their own Townlight session.</p>}<p>This does not publish or approve disclosure.</p></>}
 <details><summary>Evidence reference</summary><p>{r.id}</p>{r.details.sha256&&<p>SHA-256: {r.details.sha256}</p>}</details></article>;
}
createRoot(document.getElementById('root')!).render(<App/>);
