import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, Film, LoaderCircle, Pause, Play, ShieldCheck, Square, WandSparkles } from 'lucide-react'

type Project = { id: string; name: string }
type Preset = { name: string; frames: number; fps: number; status: string; visual_drift_warning?: boolean }
type Reference = { id: string; name: string; category: string; mime_type: string }
type FilmRun = {
  id: string; project_id: string; prompt: string; reference_ids: string[]; preset: string; updated_at: string
  status: string; stage: string; revision: number; budget: { max_shots: number; max_retries_per_shot: number; max_generation_seconds: number; max_output_bytes: number }
  completed_stages: string[]; error: string | null; events: { revision: number; level: string; message: string; time: string }[]
  generation_jobs_created?: number; planner_status?: string; message?: string
}
type Preview = { maximum_footage_seconds:number; generation_time_estimate_seconds:number|null; estimated_output_bytes:number|null; storage_hard_cap_bytes:number; generation_time_hard_cap_seconds:number; storage_sample_count:number; available_output_bytes:number; disk_sufficient_for_budget:boolean; estimated_output_within_cap:boolean|null; generation_time_within_cap:boolean|null; planner_message:string }
type Capabilities = {
  stages: string[]
  general_story_planner: { available: boolean; status: string; detail: string }
  legacy_planner: { available: boolean; status: string; detail: string }
  creative_quality_detection: { available: boolean; status: string; detail: string }
}
const stageLabels: Record<string,string> = {
  understand_story:'Understand story', define_visual_bible:'Define visual bible', plan_scenes:'Plan scenes',
  anchor_visuals:'Anchor visuals', generate_shots:'Generate shots', inspect_clips:'Inspect clips',
  repair_failed_shots:'Repair failed shots', compose_manifest:'Prepare composition', render_film:'Render film', done:'Done',
}
const activeStatuses = new Set(['awaiting_authorization','waiting_for_planner','running','paused'])

async function requestJson<T>(url:string,init?:RequestInit):Promise<T>{
  const response=await fetch(url,init);const data=await response.json()
  if(!response.ok)throw new Error(data.detail??'The local API could not complete this action')
  return data as T
}
function formatBytes(bytes:number){const units=['B','KB','MB','GB','TB'];let value=bytes,index=0;while(value>=1024&&index<units.length-1){value/=1024;index++}return `${value.toFixed(index>1?1:0)} ${units[index]}`}

export default function CreateFilmPage({project,presets,onMessage,onNavigate}:{project:Project|null;presets:Preset[];onMessage:(message:string)=>void;onNavigate:(page:string)=>void}){
  const [prompt,setPrompt]=useState('');const [references,setReferences]=useState<Reference[]>([]);const [selectedRefs,setSelectedRefs]=useState<string[]>([])
  const [preset,setPreset]=useState('Short');const [maxShots,setMaxShots]=useState(12);const [maxRetries,setMaxRetries]=useState(1);const [maxRuntime,setMaxRuntime]=useState(3600);const [maxBytes,setMaxBytes]=useState(8*1024**3)
  const [runs,setRuns]=useState<FilmRun[]>([]);const [capabilities,setCapabilities]=useState<Capabilities|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState('')
  const pendingRequest=useRef<{signature:string;key:string}|null>(null)
  const projectId=project?.id
  const load=useCallback(async()=>{
    if(!projectId){setRuns([]);setReferences([]);return}
    try{const [runList,referenceList]=await Promise.all([
      requestJson<FilmRun[]>(`/api/projects/${projectId}/film-runs`),
      requestJson<Reference[]>(`/api/projects/${projectId}/references`),
    ]);setRuns(runList);setReferences(referenceList.filter(ref=>ref.mime_type.startsWith('image/')))}catch(e){setError(e instanceof Error?e.message:String(e))}
  },[projectId])
  useEffect(()=>{
    setPrompt('');setSelectedRefs([]);pendingRequest.current=null
    if(projectId){try{const saved=localStorage.getItem(`framework-film-idempotency:${projectId}`);if(saved)pendingRequest.current=JSON.parse(saved)}catch{}}
    void load();const timer=window.setInterval(()=>void load(),2500);return()=>clearInterval(timer)
  },[load,projectId])
  useEffect(()=>{void requestJson<Capabilities>('/api/film-orchestration/capabilities').then(setCapabilities).catch(e=>setError(e instanceof Error?e.message:String(e)))},[])
  const selectedPreset=presets.find(item=>item.name===preset)
  function toggleReference(id:string){setSelectedRefs(current=>current.includes(id)?current.filter(item=>item!==id):[...current,id])}
  async function createFilm(){
    if(!project||!prompt.trim())return
    const budget={max_shots:maxShots,max_retries_per_shot:maxRetries,max_generation_seconds:maxRuntime,max_output_bytes:maxBytes}
    const input={prompt:prompt.trim(),reference_ids:selectedRefs,preset,budget}
    const signature=JSON.stringify(input)
    const signatureHash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(signature)))).map(value=>value.toString(16).padStart(2,'0')).join('')
    if(!pendingRequest.current||pendingRequest.current.signature!==signatureHash){pendingRequest.current={signature:signatureHash,key:crypto.randomUUID()}}
    try{localStorage.setItem(`framework-film-idempotency:${project.id}`,JSON.stringify(pendingRequest.current))}catch{}
    setBusy(true);setError('')
    try{
      const preview=await requestJson<Preview>(`/api/projects/${project.id}/film-runs/preview`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...input,idempotency_key:pendingRequest.current.key})})
      const timeEstimate=preview.generation_time_estimate_seconds==null
        ? 'No matching local benchmark is available, so wall-clock time cannot be estimated honestly.'
        : `Measured estimate: about ${Math.ceil(preview.generation_time_estimate_seconds/60)} minutes from local benchmark history.`
      const sizeEstimate=preview.estimated_output_bytes==null
        ? `No matching verified project output-size sample is available; output storage cap is ${formatBytes(maxBytes)}.`
        : `Estimated outputs: about ${formatBytes(preview.estimated_output_bytes)} from ${preview.storage_sample_count} verified project sample(s); storage cap ${formatBytes(maxBytes)}.`
      if(!preview.disk_sufficient_for_budget)throw new Error(`The storage cap (${formatBytes(maxBytes)}) exceeds available space (${formatBytes(preview.available_output_bytes)}). Lower the cap or free disk space, then retry.`)
      if(preview.estimated_output_within_cap===false)throw new Error('Measured output-size samples exceed this storage cap. Increase the cap or reduce the shot/retry limits, then retry.')
      if(preview.generation_time_within_cap===false)throw new Error('The measured generation estimate exceeds this time cap. Increase the cap or reduce the shot/retry limits, then retry.')
      const presetWarning=selectedPreset?.visual_drift_warning?'\nWARNING: Experimental preset may increase visual drift.':''
      const confirmation=`Create this film request?

Maximum authorized shots: ${maxShots}
Maximum footage at ${preset}: ${preview.maximum_footage_seconds.toFixed(1)} seconds
${timeEstimate}
${sizeEstimate}
Generation time cap: ${Math.ceil(maxRuntime/60)} minutes
Available output disk: ${formatBytes(preview.available_output_bytes)}
Storage hard cap: ${formatBytes(maxBytes)}
Retries per shot: ${maxRetries}${presetWarning}

This confirms the declared local LTX generation scope. No jobs will be queued until a story planner is available; this build will save a durable request only.`
      if(!window.confirm(confirmation))return
      const run=await requestJson<FilmRun>(`/api/projects/${project.id}/film-runs`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...input,idempotency_key:pendingRequest.current.key,initial_confirmation:true})})
      pendingRequest.current=null
      try{localStorage.removeItem(`framework-film-idempotency:${project.id}`)}catch{}
      setRuns(current=>[run,...current.filter(item=>item.id!==run.id)]);onMessage(run.message??'Film request saved.')
    }catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy(false)}
  }
  async function control(run:FilmRun,action:'pause'|'resume'|'cancel'){
    setBusy(true);setError('')
    try{const updated=await requestJson<FilmRun>(`/api/film-runs/${run.id}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:run.revision})});setRuns(current=>current.map(item=>item.id===updated.id?updated:item));onMessage(`Film request ${action}d.`)}catch(e){setError(e instanceof Error?e.message:String(e));await load()}finally{setBusy(false)}
  }
  return <div className="create-film-page">
    <section className="panel create-film-hero">
      <div className="section-kicker">FRAMEWORK · PROMPT TO FILM</div><h2>Create a film from one prompt</h2>
      <p>Describe the story. Select only project-approved image references. FRAMEWORK will keep a durable record of the run and its checkpoints.</p>
      {!project?<div className="model-caution"><AlertTriangle size={14}/> Select or create a project before starting a film request. <button className="text-button" onClick={()=>onNavigate('Projects')}>Open Projects</button></div>:<>
        <div className="create-film-project"><Film size={15}/><span>Project</span><b>{project.name}</b></div>
        <label className="setting-field create-film-prompt"><span>STORY PROMPT</span><textarea maxLength={20000} value={prompt} onChange={event=>setPrompt(event.target.value)} placeholder="What story should the film tell? Describe the characters, what changes, and how it ends."/><small>{prompt.length.toLocaleString()} / 20,000</small></label>
        <details className="create-film-advanced"><summary>References and generation budget</summary>
          <p>Choose linked local images for characters or environments. Historical outputs are not accepted as silent references.</p>
          {references.length?<div className="create-film-reference-list">{references.map(ref=><label key={ref.id}><input type="checkbox" checked={selectedRefs.includes(ref.id)} onChange={()=>toggleReference(ref.id)}/><span>{ref.name}</span><small>{ref.category}</small></label>)}</div>:<p>No linked image references in this project. You can add them in <button className="text-button" onClick={()=>onNavigate('References')}>References</button>.</p>}
          <div className="create-film-budget-grid">
            <label className="setting-field">Shot preset<select value={preset} onChange={event=>setPreset(event.target.value)}>{presets.map(item=><option key={item.name} value={item.name}>{item.name} · {item.frames} frames · {item.fps} fps{item.visual_drift_warning?' · drift warning':''}</option>)}</select></label>
            <label className="setting-field">Maximum shots<input type="number" min="1" max="40" value={maxShots} onChange={event=>setMaxShots(Math.max(1,Math.min(40,Number(event.target.value)||1)))}/></label>
            <label className="setting-field">Retries per shot<input type="number" min="0" max="3" value={maxRetries} onChange={event=>setMaxRetries(Math.max(0,Math.min(3,Number(event.target.value)||0)))}/></label>
            <label className="setting-field">Time budget (minutes)<input type="number" min="1" max="1440" value={Math.round(maxRuntime/60)} onChange={event=>setMaxRuntime(Math.max(60,Math.min(86400,Number(event.target.value)*60||60)))}/></label>
            <label className="setting-field">Output storage cap (GB)<input type="number" min="0.25" max="50" step="0.25" value={Number((maxBytes/1024**3).toFixed(2))} onChange={event=>setMaxBytes(Math.max(256*1024**2,Math.min(50*1024**3,Number(event.target.value)*1024**3||256*1024**2)))}/></label>
          </div>
          <div className="create-film-estimate"><b>Approval preview</b><span>Up to {(maxShots*((selectedPreset?.frames??49)/(selectedPreset?.fps??24))).toFixed(1)}s of requested footage · {Math.ceil(maxRuntime/60)} minute time cap · {formatBytes(maxBytes)} output cap.</span><small>Click Create Film to get a read-only time estimate from local benchmark history and a size estimate from this project’s verified outputs before confirming. Missing measurements are shown as unavailable rather than guessed.</small></div>
        </details>
        {capabilities&&!capabilities.general_story_planner.available&&<div className="create-film-limitation"><ShieldCheck size={15}/><div><b>Automation capability status</b><span>{capabilities.general_story_planner.detail} {capabilities.legacy_planner.detail} Creative QA remains manual; ffprobe only verifies technical media properties.</span><button className="text-button" onClick={()=>onNavigate('Story to Film')}>Open the existing production workflow</button></div></div>}
        <div className="create-film-actions"><button className="button primary" disabled={busy||!prompt.trim()} onClick={()=>void createFilm()}>{busy?<LoaderCircle className="spin" size={15}/>:<WandSparkles size={15}/>} Create Film</button><span>One initial confirmation records the generation authorization and budget. Creative review remains separate.</span></div>
      </>}
      {error&&<div className="model-caution"><AlertTriangle size={13}/>{error}</div>}
    </section>
    <section className="panel create-film-runs"><div className="section-kicker">DURABLE RUN HISTORY</div><h2>Film runs</h2>{runs.length?runs.map(run=><FilmRunCard key={run.id} run={run} busy={busy} onControl={control}/>):<p className="empty-workspace">No film runs in this project yet.</p>}</section>
  </div>
}

function FilmRunCard({run,busy,onControl}:{run:FilmRun;busy:boolean;onControl:(run:FilmRun,action:'pause'|'resume'|'cancel')=>void}){
  const stageIndex=Math.max(0,run.completed_stages.length)
  const progress=Math.round(stageIndex/9*100)
  const blocked=run.status==='waiting_for_planner'||run.status==='needs_attention'
  return <article className="film-run-card">
    <header><div><b>{run.prompt.length>100?`${run.prompt.slice(0,100)}…`:run.prompt}</b><small>{new Date(run.updated_at).toLocaleString()} · {run.preset} · {run.budget.max_shots} shot cap</small></div><span className={`job-status ${run.status}`}>{run.status.replaceAll('_',' ')}</span></header>
    <div className="film-run-progress"><span style={{width:`${progress}%`}}/></div>
    <ol className="film-run-timeline">{["understand_story","define_visual_bible","plan_scenes","anchor_visuals","generate_shots","inspect_clips","repair_failed_shots","compose_manifest","render_film","done"].map((stage,index)=>{
      const complete=run.completed_stages.includes(stage);const current=run.stage===stage
      return <li className={`${complete?'complete':''} ${current?'current':''}`} key={stage}><span>{complete?<Check size={12}/>:index+1}</span><small>{stageLabels[stage]}</small></li>
    })}</ol>
    {blocked&&<div className="create-film-limitation"><AlertTriangle size={14}/><span>{run.error??'Waiting for a verified general story-planning capability. No shot jobs have been queued.'}</span></div>}
    <div className="create-film-run-meta"><span>Authorization {run.status==='awaiting_authorization'?'not granted':'recorded'}</span><span>Generation jobs created: {run.generation_jobs_created??0}</span><span>Technical verification and creative approval remain separate.</span></div>
    {run.events.length>0&&<details><summary>Checkpoint log</summary><div className="create-film-log">{run.events.map((event,index)=><p key={`${event.revision}-${index}`}><time>{new Date(event.time).toLocaleTimeString()}</time> {event.message}</p>)}</div></details>}
    <footer>{activeStatuses.has(run.status)&&run.status!=='awaiting_authorization'&&<button className="button secondary mini" disabled={busy} onClick={()=>onControl(run,run.status==='paused'?'resume':'pause')}>{run.status==='paused'?<Play size={12}/>:<Pause size={12}/>} {run.status==='paused'?'Resume':'Pause'}</button>}{!['cancelled','completed','failed'].includes(run.status)&&<button className="button secondary mini" disabled={busy} onClick={()=>onControl(run,'cancel')}><Square size={12}/> Cancel run</button>}</footer>
  </article>
}
