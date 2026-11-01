import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, Film, LoaderCircle, Pause, Play, ShieldCheck, Square, WandSparkles } from 'lucide-react'
import GeminiConnectionPanel, { type StoryPlannerConfig } from './GeminiConnectionPanel'

type Project = { id: string; name: string }
type Preset = { name: string; frames: number; fps: number; status: string; visual_drift_warning?: boolean }
type Reference = { id: string; name: string; category: string; mime_type: string }
type FilmRun = {
  id: string; project_id: string; prompt: string; reference_ids: string[]; preset: string; updated_at: string
  replan_deduplicated?: boolean
  status: string; stage: string; revision: number; budget: { max_shots: number; max_retries_per_shot: number; max_generation_seconds: number; max_output_bytes: number }
  completed_stages: string[]; error: string | null; events: { revision: number; level: string; message: string; time: string }[]
  generation_jobs_created?: number; planner_status?: string; message?: string
  storyboard_id?: string; creative_review_status?: string|null
  request?: {planner_provider?:'gemini'|'local';planner_model?:string|null}
  planner_request?: {status:string;provider:string;model:string;revision:number;schema_version:string;attempts:number;remaining_attempts:number;error_code:string|null}|null
  planner_attempts?: {revision:number;provider:string;model:string;schema_version:string;status:string;attempts:number;remaining_attempts:number;error_code:string|null;consent:Record<string,unknown>|null}[]
  planner_schema_current?:string;planner_revision_limit?:number;planner_attempt_limit?:number;planner_hourly_attempt_limit?:number;planner_hourly_attempts_used?:number
  generation_progress?: {total:number;verified:number;queued:number;failed:number;progress:number;shots:{title:string;scene_title:string;status:string;progress:number;events:{time:string;level:string;message:string}[]}[]}|null
}
type Preview = { maximum_footage_seconds:number; generation_time_estimate_seconds:number|null; estimated_output_bytes:number|null; storage_hard_cap_bytes:number; generation_time_hard_cap_seconds:number; storage_sample_count:number; available_output_bytes:number; disk_sufficient_for_budget:boolean; estimated_output_within_cap:boolean|null; generation_time_within_cap:boolean|null; planner_message:string;general_story_planner_available:boolean }
type Capabilities = {
  stages: string[]
  general_story_planner: { available: boolean; status: string; detail: string }
  legacy_planner: { available: boolean; status: string; detail: string }
  creative_quality_detection: { available: boolean; status: string; detail: string }
  story_planners?: { recommended:string; gemini:{available:boolean;status:string;detail:string};local:{available:boolean;status:string;detail:string} }
}
const stageLabels: Record<string,string> = {
  understand_story:'Understand story', define_visual_bible:'Define visual bible', plan_scenes:'Plan scenes',
  anchor_visuals:'Anchor visuals', generate_shots:'Generate shots', inspect_clips:'Inspect clips',
  creative_review:'Creative review',
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
  const [preset,setPreset]=useState('Short');const [maxShots,setMaxShots]=useState(1);const [maxRetries,setMaxRetries]=useState(1);const [maxRuntime,setMaxRuntime]=useState(3600);const [maxBytes,setMaxBytes]=useState(8*1024**3)
  const [runs,setRuns]=useState<FilmRun[]>([]);const [capabilities,setCapabilities]=useState<Capabilities|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState('')
  const [plannerProvider,setPlannerProvider]=useState<'gemini'|'local'>('gemini');const [geminiConfig,setGeminiConfig]=useState<StoryPlannerConfig|null>(null);const [remoteConsent,setRemoteConsent]=useState(false)
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
  useEffect(()=>{void requestJson<StoryPlannerConfig>('/api/story-planner/config').then(setGeminiConfig).catch(e=>setError(e instanceof Error?e.message:String(e)))},[])
  const selectedPreset=presets.find(item=>item.name===preset)
  function toggleReference(id:string){setSelectedRefs(current=>current.includes(id)?current.filter(item=>item!==id):[...current,id])}
  async function createFilm(){
    if(!project||!prompt.trim())return
    const budget={max_shots:maxShots,max_retries_per_shot:maxRetries,max_generation_seconds:maxRuntime,max_output_bytes:maxBytes}
    const input={prompt:prompt.trim(),reference_ids:selectedRefs,preset,budget,planner_provider:plannerProvider,
      planner_model:plannerProvider==='gemini'?geminiConfig?.gemini.model??null:null,remote_processing_consent:plannerProvider==='gemini'&&remoteConsent}
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

Planner: ${plannerProvider==='gemini'?`Gemini API${geminiConfig?.gemini.model?` · ${geminiConfig.gemini.model}`:''} (up to three explicitly authorized planning attempts for this revision; model/schema changes are separately tracked)`:'configured local planner'}

${plannerProvider==='gemini'?'You opted in to send the story prompt, project style text, preset/budget, and opaque selected-reference IDs/categories to Google. No images, generated media, filenames, paths, reference notes or project database are uploaded. Gemini planning may incur charges; the account tier and exact price are not known to FRAMEWORK.':'Story planning stays local.'} LTX generation and Remotion rendering remain local.

Technical checks do not approve creative quality; the run will stop for creative review after generation.`
      if(!window.confirm(confirmation))return
      const run=await requestJson<FilmRun>(`/api/projects/${project.id}/film-runs`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...input,idempotency_key:pendingRequest.current.key,initial_confirmation:true})})
      pendingRequest.current=null
      try{localStorage.removeItem(`framework-film-idempotency:${project.id}`)}catch{}
      setRuns(current=>[run,...current.filter(item=>item.id!==run.id)]);onMessage(run.message??'Film request saved.')
    }catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy(false)}
  }
  async function control(run:FilmRun,action:'pause'|'resume'|'cancel'){
    if(action==='resume'&&run.stage==='understand_story'&&run.request?.planner_provider==='gemini'&&run.planner_request&&['started','failed'].includes(run.planner_request.status)&&!['missing_key','model_missing','model_unavailable'].includes(run.planner_request.error_code??'')){
      if(!window.confirm('The previous Gemini request may have reached Google and may be billable. FRAMEWORK will not retry automatically. Authorize one new explicit planning attempt?'))return
    }
    setBusy(true);setError('')
    try{const updated=await requestJson<FilmRun>(`/api/film-runs/${run.id}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:run.revision})});setRuns(current=>current.map(item=>item.id===updated.id?updated:item));onMessage(`Film request ${action}d.`)}catch(e){setError(e instanceof Error?e.message:String(e));await load()}finally{setBusy(false)}
  }
  async function replan(run:FilmRun,model:string){
    const sameModel=model===run.request?.planner_model
    const revisionKey=`framework-film-replan:${run.id}:${run.revision}`
    let idempotencyKey=''
    try{idempotencyKey=localStorage.getItem(revisionKey)??''}catch{}
    if(!idempotencyKey){idempotencyKey=crypto.randomUUID();try{localStorage.setItem(revisionKey,idempotencyKey)}catch{}}
    const confirmed=window.confirm(`Authorize a new Gemini planning revision using ${model}${sameModel?' and the current schema':' instead of '+(run.request?.planner_model??'the prior model')}?\n\nThis sends this run's saved story prompt, project style text, preset/budget, and opaque reference IDs/categories to Google. It does not upload media. The request may incur a charge.\n\nThe original ${run.budget.max_shots}-shot and time/storage budget and generation authorization remain unchanged. No LTX job is created until the new plan passes validation. Test connection only lists models; this is the actual structured-planning request.`)
    if(!confirmed)return
    setBusy(true);setError('')
    try{
      const updated=await requestJson<FilmRun>(`/api/film-runs/${run.id}/replan`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:run.revision,planner_model:model,idempotency_key:idempotencyKey,confirm_billable_planning:true})})
      try{localStorage.removeItem(revisionKey)}catch{}
      setRuns(current=>[updated,...current.filter(item=>item.id!==updated.id)])
      onMessage(updated.replan_deduplicated?'Reused the already authorized planning revision.':'New planning revision authorized. Previous attempts and the run budget are preserved.')
    }catch(e){setError(e instanceof Error?e.message:String(e));await load()}finally{setBusy(false)}
  }
  return <div className="create-film-page">
    <section className="panel create-film-hero">
      <div className="section-kicker">FRAMEWORK · PROMPT TO FILM</div><h2>Create a film from one prompt</h2>
      <p>Describe the story and authorize its limits once. FRAMEWORK will plan, validate, queue eligible shots, and report progress. Generated clips remain pending creative review.</p>
      {!project?<div className="model-caution"><AlertTriangle size={14}/> Select or create a project before starting a film request. <button className="text-button" onClick={()=>onNavigate('Projects')}>Open Projects</button></div>:<>
        <div className="create-film-project"><Film size={15}/><span>Project</span><b>{project.name}</b></div>
        <label className="setting-field create-film-prompt"><span>STORY PROMPT</span><textarea maxLength={20000} value={prompt} onChange={event=>setPrompt(event.target.value)} placeholder="What story should the film tell? Describe the characters, what changes, and how it ends."/><small>{prompt.length.toLocaleString()} / 20,000</small></label>
        <div className="create-film-planner-choice"><div><div className="section-kicker">STORY PLANNING PROVIDER</div><p>Gemini API is recommended for general stories. Your selected planner only creates the structured plan; all video work remains local.</p></div><label><input type="radio" name="story-planner-provider" checked={plannerProvider==='gemini'} onChange={()=>{setPlannerProvider('gemini');setRemoteConsent(false)}}/> Gemini API <small>Recommended · cloud</small></label><label><input type="radio" name="story-planner-provider" checked={plannerProvider==='local'} onChange={()=>{setPlannerProvider('local');setRemoteConsent(false)}}/> Local planner <small>Optional · offline</small></label></div>
        {plannerProvider==='gemini'&&<><GeminiConnectionPanel compact onConfig={setGeminiConfig}/><label className="gemini-consent"><input type="checkbox" checked={remoteConsent} onChange={event=>setRemoteConsent(event.target.checked)}/><span>I opt in to send the disclosed prompt and minimal planning metadata to Google Gemini for this film run. No reference image or generated media is uploaded.</span></label>{geminiConfig?.gemini.status!=='connected'&&<div className="model-caution"><AlertTriangle size={13}/>{geminiConfig?.gemini.detail??'Connect and test a Gemini API key before planning. No cloud request is made automatically.'}</div>}</>}
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
        {plannerProvider==='local'&&<div className="create-film-limitation"><ShieldCheck size={15}/><div><b>Optional local planner · {capabilities?.story_planners?.local.status.replaceAll('_',' ')??'checking'}</b><span>{capabilities?.story_planners?.local.detail??'Checking local planner availability.'} Every result is schema and project-scope validated.</span></div></div>}
        <div className="create-film-actions"><button className="button primary" disabled={busy||!prompt.trim()||(plannerProvider==='gemini'&&!remoteConsent)} onClick={()=>void createFilm()}>{busy?<LoaderCircle className="spin" size={15}/>:<WandSparkles size={15}/>} Create Film</button><span>One action requests planning and authorizes the declared local generation budget. Gemini planning requires the explicit opt-in above; creative review remains separate.</span></div>
      </>}
      {error&&<div className="model-caution"><AlertTriangle size={13}/>{error}</div>}
    </section>
    <section className="panel create-film-runs"><div className="section-kicker">DURABLE RUN HISTORY</div><h2>Film runs</h2>{runs.length?runs.map(run=><FilmRunCard key={run.id} run={run} busy={busy} models={geminiConfig?.gemini.models??[]} geminiConnected={geminiConfig?.gemini.status==='connected'} onControl={control} onReplan={replan} onNavigate={onNavigate}/>):<p className="empty-workspace">No film runs in this project yet.</p>}</section>
  </div>
}

function FilmRunCard({run,busy,models,geminiConnected,onControl,onReplan,onNavigate}:{run:FilmRun;busy:boolean;models:{id:string;display_name:string}[];geminiConnected:boolean;onControl:(run:FilmRun,action:'pause'|'resume'|'cancel')=>void;onReplan:(run:FilmRun,model:string)=>void;onNavigate:(page:string)=>void}){
  const [replanModel,setReplanModel]=useState('')
  const modelIds=models.map(model=>model.id).join('|')
  useEffect(()=>{const replacement=models.find(model=>model.id!==run.request?.planner_model)?.id??run.request?.planner_model??'';setReplanModel(replacement)},[run.request?.planner_model,modelIds])
  const stageIndex=Math.max(0,run.completed_stages.length)
  const progress=Math.round(stageIndex/10*100)
  const blocked=run.status==='needs_attention'
  const isGeminiPlanningFailure=blocked&&run.stage==='understand_story'&&run.request?.planner_provider==='gemini'&&!!run.planner_request&&['failed','started'].includes(run.planner_request.status)
  const schemaMatches=run.planner_request?.schema_version===run.planner_schema_current
  const canRetry=blocked&&((run.stage==='understand_story'&&run.request?.planner_provider==='gemini'&&schemaMatches&&(run.planner_request?.remaining_attempts??0)>0)||run.stage==='generate_shots')&&!['not_configured','executable_missing','invalid_configuration'].includes(run.planner_status??'')
  const canReplan=isGeminiPlanningFailure&&geminiConnected&&(run.planner_attempts?.length??0)<(run.planner_revision_limit??3)&&(run.planner_hourly_attempts_used??0)<(run.planner_hourly_attempt_limit??5)
  const diagnostic:Record<string,string>={model_or_endpoint_incompatible:'The selected model or endpoint was unavailable (404). This points to model/API compatibility; choose another account-listed model.',model_invalid:'The selected model is no longer in the compatible account model list. Test the connection and choose a currently listed model.',model_unavailable:'The selected model is unavailable to this account. Test the connection and switch to a listed model.',invalid_request:'Gemini rejected the structured planning request (400). A 400 is not proof that the model is faulty; the request schema may be unsupported.',service_unavailable:'Gemini returned a temporary provider/service error. Wait before spending another attempt.',rate_limit:'Gemini or the account quota rate-limited this request. Wait for the hourly safeguard or account quota to recover.',auth_denied:'Gemini rejected the key or account permissions. Fix the connection in Settings before retrying.',invalid_output:'The response did not pass FRAMEWORK’s strict StoryPlan checks after its bounded repair.',incomplete_output:'Gemini stopped before completing the structured plan. This is an output or response-budget issue; the incomplete plan was not accepted.',blocked_content:'Gemini blocked the prompt under its safety policy. Review the story prompt before authorizing another request.',timeout:'The request timed out and may have reached Google. Its outcome may be billable; it was not resent automatically.',provider_error:'Gemini planning failed; the original provider response is hidden to protect private data.'}
  return <article className="film-run-card">
    <header><div><b>{run.prompt.length>100?`${run.prompt.slice(0,100)}…`:run.prompt}</b><small>{new Date(run.updated_at).toLocaleString()} · {run.preset} · {run.budget.max_shots} shot cap</small></div><span className={`job-status ${run.status}`}>{run.status.replaceAll('_',' ')}</span></header>
    <div className="film-run-progress"><span style={{width:`${progress}%`}}/></div>
    <ol className="film-run-timeline">{["understand_story","define_visual_bible","plan_scenes","anchor_visuals","generate_shots","inspect_clips","creative_review","repair_failed_shots","compose_manifest","render_film","done"].map((stage,index)=>{
      const complete=run.completed_stages.includes(stage);const current=run.stage===stage
      return <li className={`${complete?'complete':''} ${current?'current':''}`} key={stage}><span>{complete?<Check size={12}/>:index+1}</span><small>{stageLabels[stage]}</small></li>
    })}</ol>
    {blocked&&<div className="create-film-limitation"><AlertTriangle size={14}/><span>{run.error??'Waiting for a verified general story-planning capability. No shot jobs have been queued.'}</span></div>}
    {isGeminiPlanningFailure&&<section className="planner-recovery-panel">
      <div className="section-kicker">GEMINI PLANNING DIAGNOSTIC</div>
      <p><b>{diagnostic[run.planner_request?.error_code??'']??'Planning stopped before any shot job was queued.'}</b></p>
      <p>Run is pinned to <code>{run.planner_request?.model??run.request?.planner_model??'no model'}</code> · schema <code>{run.planner_request?.schema_version??'unknown'}</code> · revision {run.planner_request?.revision??1}.</p>
      <p>Current revision: {run.planner_request?.attempts??0}/{run.planner_attempt_limit??3} planning attempts used, {run.planner_request?.remaining_attempts??0} remaining. Hourly safeguard: {run.planner_hourly_attempts_used??0}/{run.planner_hourly_attempt_limit??5} attempts used. Revisions: {run.planner_attempts?.length??0}/{run.planner_revision_limit??3}.</p>
      {run.planner_attempts&&run.planner_attempts.length>0&&<details><summary>Planner revision history</summary><ol className="planner-attempt-history">{run.planner_attempts.map(item=><li key={item.revision}><b>Revision {item.revision}</b> · {item.model} · {item.schema_version} · {item.attempts} attempt(s) · {item.status}{item.error_code?` · ${item.error_code}`:''}</li>)}</ol></details>}
      <div className="planner-recovery-controls"><label className="setting-field"><span>ACCOUNT-AVAILABLE MODEL FOR THIS RUN</span><select value={replanModel} disabled={busy||!geminiConnected||!models.length} onChange={event=>setReplanModel(event.target.value)}>{models.map(model=><option key={model.id} value={model.id}>{model.display_name} · {model.id}{model.id===run.planner_request?.model?' · current':''}</option>)}</select></label>
        <button className="button primary mini" disabled={busy||!canReplan||!replanModel} onClick={()=>onReplan(run,replanModel)}>{replanModel!==run.planner_request?.model?'Switch model and retry planning':run.planner_request?.schema_version!==run.planner_schema_current?'Retry with updated schema':'Authorize a separate planning revision'}</button></div>
      {geminiConnected&&models.length>0&&<p>These account-listed models expose the supported planning endpoint, but model listing does not guarantee acceptance of FRAMEWORK’s structured StoryPlan schema. Switching performs a real planning request only after confirmation; it may be billable.</p>}
      {!geminiConnected&&<p>Connect Gemini and run Test connection to list account models. That connection test does not send a planning request.</p>}
      {geminiConnected&&models.every(model=>model.id===run.planner_request?.model)&&<p>No alternate compatible models were returned by this account. A confirmed retry can still use the revised schema with the current model.</p>}
      {(run.planner_attempts?.length??0)>=(run.planner_revision_limit??3)&&<p>This run has reached its revision limit. Its history is preserved; review account usage before starting a new film run.</p>}
      {(run.planner_hourly_attempts_used??0)>=(run.planner_hourly_attempt_limit??5)&&<p>The hourly planning safeguard is exhausted. Wait before authorizing another request.</p>}
      <small>Switching does not alter global Settings, reset old attempt counts, or change this run’s shot/time/storage budget. The next confirmed action sends a real structured-planning request and may be billable. Connection testing only lists models; it does not test schema compatibility.</small>
    </section>}
    {run.status==='needs_review'&&<div className="create-film-limitation"><ShieldCheck size={14}/><div><span>All {run.generation_progress?.verified??0} clip(s) passed technical verification. Creative review is pending; no automatic visual approval or Remotion render occurred.</span><button className="text-button" onClick={()=>onNavigate('Story to Film')}>Review verified clips</button></div></div>}
    {run.generation_progress&&<div className="film-run-shot-progress"><header><b>Local generation queue</b><span>{run.generation_progress.verified}/{run.generation_progress.total} technically verified · {run.generation_progress.progress}%</span></header>{run.generation_progress.shots.map((shot,index)=><div className="film-run-shot" key={`${index}-${shot.title}`}><span>{shot.scene_title} / {shot.title}</span><b className={`job-status ${shot.status}`}>{shot.status} {shot.status==='running'?`${shot.progress}%`:''}</b>{shot.events.slice(-1).map((event,eventIndex)=><small key={eventIndex}>{event.message}</small>)}</div>)}</div>}
    <div className="create-film-run-meta"><span>Authorization {run.status==='awaiting_authorization'?'not granted':'recorded'}</span><span>Generation jobs created: {run.generation_jobs_created??0}</span><span>Technical verification and creative approval remain separate.</span></div>
    {run.events.length>0&&<details><summary>Checkpoint log</summary><div className="create-film-log">{run.events.map((event,index)=><p key={`${event.revision}-${index}`}><time>{new Date(event.time).toLocaleTimeString()}</time> {event.message}</p>)}</div></details>}
    <footer>{((activeStatuses.has(run.status)&&run.status!=='awaiting_authorization')||canRetry)&&<button className="button secondary mini" disabled={busy} onClick={()=>onControl(run,run.status==='paused'||blocked?'resume':'pause')}>{run.status==='paused'||blocked?<Play size={12}/>:<Pause size={12}/>} {blocked?'Retry stage':run.status==='paused'?'Resume':'Pause'}</button>}{!['cancelled','completed','failed','needs_review'].includes(run.status)&&<button className="button secondary mini" disabled={busy} onClick={()=>onControl(run,'cancel')}><Square size={12}/> Cancel run</button>}</footer>
  </article>
}
