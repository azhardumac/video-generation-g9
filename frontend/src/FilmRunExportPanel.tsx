import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Check, Clapperboard, Film, LoaderCircle, Play, RefreshCw } from 'lucide-react'

type ReviewShot = { shot_id:string; title:string; verified:boolean }
type FilmRun = { id:string; project_id:string; status:string; stage:string; revision:number; completed_stages:string[]; storyboard_id?:string; archived_at?:string|null; generation_progress?:{total:number;verified:number;progress:number;shots:ReviewShot[]}|null }
type RenderJob = { id:string; status:string; progress:number; result_path:string|null; elapsed_seconds:number|null; error?:string|null; current:boolean; verification_metadata?:{verified_output?:{width:number;height:number;fps:number;frames:number}}|null }
type ExportStatus = { run_id:string; run_revision:number; storyboard_id:string; revision:number; total_shots:number; verified_shots:number; all_verified:boolean; approved:boolean; reconciled:boolean; creative_review_status:string|null; ready_to_prepare:boolean; preparation_blockers:string[]; manifest_ready:boolean; manifest:{duration_in_frames:number;width:number;height:number;fps:number}|null; remotion_ready:boolean; latest_render:RenderJob|null; verified_final:boolean }
type Action = 'review'|'preparing'|'rendering'|'watch'|'prepare'|'render'|'authorize'|'progress'|'blocked'

export function resolveFilmRunNextAction(run:FilmRun,status:ExportStatus|null):Action {
  if(run.archived_at)return 'blocked'
  if(status?.verified_final)return 'watch'
  if(status?.latest_render&&['queued','running'].includes(status.latest_render.status))return 'rendering'
  if(status?.ready_to_prepare&&!status.manifest_ready)return 'prepare'
  if(status?.manifest_ready&&status.approved&&status.all_verified&&status.reconciled)return status.remotion_ready?'render':'blocked'
  if(run.status==='awaiting_authorization'&&run.stage==='define_visual_bible')return 'authorize'
  if(run.status==='needs_review'&&(status?.verified_shots??run.generation_progress?.verified??0)>0)return 'review'
  if(['running','paused','waiting_for_planner','awaiting_authorization'].includes(run.status))return 'progress'
  return 'blocked'
}

async function requestJson<T>(url:string,init?:RequestInit):Promise<T>{
  const response=await fetch(url,init)
  const data=await response.json().catch(()=>({detail:'The local API returned an unreadable response'}))
  if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'The local API could not complete this export action')
  return data as T
}
const post=(body?:unknown):RequestInit=>({method:'POST',...(body===undefined?{}:{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})})

export default function FilmRunExportPanel({run,onReview,onMessage,onAuthorizeGeneration}:{run:FilmRun;onReview:(runId:string,shotId:string)=>void;onMessage:(message:string)=>void;onAuthorizeGeneration:(runId:string)=>void}){
  const [status,setStatus]=useState<ExportStatus|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState('')
  const refresh=useCallback(async()=>{
    if(!run.storyboard_id){setStatus(null);return}
    const value=await requestJson<ExportStatus>(`/api/film-runs/${encodeURIComponent(run.id)}/export-status`)
    setStatus(value)
  },[run.id,run.storyboard_id])
  useEffect(()=>{if(!run.storyboard_id)return;let live=true;const poll=async()=>{try{if(live)await refresh()}catch(e){if(live)setError(e instanceof Error?e.message:'Could not load production status')}};void poll();const timer=window.setInterval(poll,2000);return()=>{live=false;window.clearInterval(timer)}},[refresh,run.storyboard_id])
  const action=resolveFilmRunNextAction(run,status)
  const verifiedShot=run.generation_progress?.shots.find(shot=>shot.verified)
  const prepare=async()=>{
    if(!status)return
    setBusy(true);setError('')
    try{
      const result=await requestJson<{export_status:ExportStatus;reconciled_shots:number;render_started:boolean}>(`/api/film-runs/${encodeURIComponent(run.id)}/prepare`,post({expected_run_revision:status.run_revision,expected_storyboard_revision:status.revision}))
      setStatus(result.export_status)
      onMessage(`Film prepared from ${result.reconciled_shots} existing verified clip(s). No LTX job or Remotion render was started.`)
    }catch(e){setError(e instanceof Error?e.message:'Could not prepare the existing film clips');await refresh().catch(()=>{})}
    finally{setBusy(false)}
  }
  const render=async()=>{
    if(!status||!status.storyboard_id)return
    const retry=status.latest_render?.status==='failed'
    if(!window.confirm(`${retry?'Retry':'Render'} this prepared film to a final MP4 using local Remotion? This is a separate resource-intensive render. No LTX generation will run.`))return
    setBusy(true);setError('')
    try{const job=await requestJson<{id:string}>(`/api/film-runs/${encodeURIComponent(run.id)}/render`,post({expected_run_revision:status.run_revision,expected_storyboard_revision:status.revision,confirmed:true}));await refresh();onMessage(`Final MP4 render ${job.id} was queued. Progress is saved and will return after refresh.`)}
    catch(e){setError(e instanceof Error?e.message:'Could not queue the final Remotion render');await refresh().catch(()=>{})}
    finally{setBusy(false)}
  }
  const reveal=async()=>{
    const path=status?.latest_render?.result_path
    if(!path)return
    try{await requestJson('/api/outputs/reveal',post({path}));onMessage('Opened the verified final MP4 in Finder.')}
    catch(e){setError(e instanceof Error?e.message:'Could not reveal the final MP4')}
  }
  const review=()=>{if(verifiedShot)onReview(run.id,verifiedShot.shot_id)}
  const timeline=[
    {label:'Prompt & plan',done:run.completed_stages.includes('understand_story')||!!run.storyboard_id},
    {label:'Generate',done:!!status?.all_verified},
    {label:'Creative review',done:status?.creative_review_status==='approved'},
    {label:'Prepare film',done:!!status?.manifest_ready},
    {label:'Render MP4',done:!!status?.verified_final},
  ]
  return <section className="run-export-flow" aria-label="Film production timeline">
    <ol className="run-export-timeline">{timeline.map((step,index)=><li key={step.label} className={step.done?'complete':''}><span>{step.done?<Check size={12}/>:index+1}</span><small>{step.label}</small></li>)}</ol>
    <div className="run-export-next-action">
      {action==='review'&&<><div><b>Next: review the verified clip</b><p>{status?.verified_shots??run.generation_progress?.verified??0}/{status?.total_shots??run.generation_progress?.total??0} clips passed technical verification. Creative approval is a separate decision.</p></div><button className="button primary" disabled={!verifiedShot} onClick={review}><Play size={14}/> Review verified clip</button></>}
      {action==='prepare'&&<><div><b>Next: prepare the film</b><p>Approval is current for every shot. This reconciles the existing verified jobs and writes composition metadata only.</p></div><button className="button primary" disabled={busy} onClick={()=>void prepare()}>{busy?<LoaderCircle size={14} className="spin"/>:<Clapperboard size={14}/>} {busy?'Preparing…':'Prepare Film'}</button></>}
      {action==='render'&&<><div><b>Composition ready</b><p>{status?.manifest?.duration_in_frames} frames · {status?.manifest?.width} × {status?.manifest?.height} · {status?.manifest?.fps} fps. Rendering is a separate local Remotion action.</p></div><button className="button primary" disabled={busy} onClick={()=>void render()}>{busy?<LoaderCircle size={14} className="spin"/>:<Clapperboard size={14}/>} Render Final MP4</button></>}
      {action==='authorize'&&<><div><b>Validated plan ready</b><p>Authorize the saved plan to queue its approved local shot budget. No planning request will be repeated.</p></div><button className="button primary" onClick={()=>onAuthorizeGeneration(run.id)}><Clapperboard size={14}/> Authorize local generation</button></>}
      {action==='rendering'&&<div className="run-export-progress"><div><b>{status?.latest_render?.status==='queued'?'Render queued':'Rendering final MP4'}</b><span>{status?.latest_render?.progress??0}%</span></div><div className="batch-progress" role="progressbar" aria-label="Final film render progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={status?.latest_render?.progress??0}><span style={{width:`${status?.latest_render?.progress??0}%`}}/></div><small>Progress is stored with the render job and will resume here after refresh.</small></div>}
      {action==='watch'&&<><div><b>Final MP4 is ready</b><p>{status?.latest_render?.elapsed_seconds!=null?`Rendered in ${status.latest_render.elapsed_seconds.toFixed(1)} seconds.`:'Verified final render is available.'} Playback is verified against the prepared composition.</p></div><button className="button secondary" onClick={()=>void reveal()}><Film size={14}/> Reveal in Finder</button></>}
      {action==='progress'&&<div><b>{run.status==='awaiting_authorization'?'Plan ready for authorization':'Production in progress'}</b><p>{run.status==='awaiting_authorization'?'Authorize local generation when ready.':`${run.generation_progress?.verified??0}/${run.generation_progress?.total??0} shots technically verified.`}</p></div>}
      {action==='blocked'&&<div><b>{run.archived_at?'Film run is archived':status?.latest_render?.status==='failed'?'Render failed':status?.preparation_blockers?.length?'Production is blocked':'Export is not ready'}</b><p>{run.archived_at?'Restore this run to continue its production.':status?.latest_render?.status==='failed'?status.latest_render.error||'The last render failed. Review job details before retrying.':status?.preparation_blockers?.join(' ')||(!status?.remotion_ready?'Local Remotion is unavailable. Check Settings.':'Current review and verified output are required before preparation.')}</p>{!run.archived_at&&status?.latest_render?.status==='failed'&&status.manifest_ready&&status.remotion_ready&&status.approved&&status.all_verified&&status.reconciled&&<button className="button primary" disabled={busy} onClick={()=>void render()}><RefreshCw size={13}/> Render Final MP4</button>}</div>}
      {busy&&action!=='rendering'&&<span className="run-export-busy" role="status"><LoaderCircle size={13} className="spin"/> Updating saved production status…</span>}
    </div>
    {error&&<div className="run-export-error"><AlertTriangle size={13}/><span>{error}</span><button className="button secondary mini" onClick={()=>void refresh().catch(e=>setError(e.message))}>Refresh status</button></div>}
    {status?.manifest_ready&&status.manifest&&<small className="run-export-composition">Prepared composition · {status.manifest.duration_in_frames} frames · {(status.manifest.duration_in_frames/status.manifest.fps).toFixed(2)}s · metadata only.</small>}
    {status?.latest_render?.status==='failed'&&action!=='blocked'&&<div className="run-export-error"><AlertTriangle size={13}/><span>Final render failed: {status.latest_render.error||'No error details were saved.'}</span></div>}
    {status?.verified_final&&status.latest_render?.result_path&&<div className="run-export-video"><video controls playsInline preload="metadata" aria-label="Verified final film" src={`/api/media/file?path=${encodeURIComponent(status.latest_render.result_path)}`}/><div><b>Final MP4 · {status.latest_render.verification_metadata?.verified_output?.width} × {status.latest_render.verification_metadata?.verified_output?.height}</b><button className="text-button" onClick={()=>void reveal()}>Reveal in Finder</button></div></div>}
  </section>
}
