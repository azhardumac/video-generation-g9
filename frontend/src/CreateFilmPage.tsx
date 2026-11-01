import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, Film, LoaderCircle, Pause, Play, ShieldCheck, Square, WandSparkles } from 'lucide-react'
import GeminiConnectionPanel, { type StoryPlannerConfig } from './GeminiConnectionPanel'
import FilmRunReviewAction from './FilmRunReviewAction'

type Project = { id: string; name: string }
type Preset = { name: string; frames: number; fps: number; status: string; visual_drift_warning?: boolean }
type Reference = { id: string; name: string; category: string; mime_type: string }
type FilmRun = {
  id: string; project_id: string; prompt: string; reference_ids: string[]; preset: string; updated_at: string
  archived_at?: string|null; deduplicated?:boolean
  replan_deduplicated?: boolean
  status: string; stage: string; revision: number; budget: { max_shots: number; max_retries_per_shot: number; max_generation_seconds: number; max_output_bytes: number }
  completed_stages: string[]; error: string | null; events: { revision: number; level: string; message: string; time: string }[]
  generation_jobs_created?: number; planner_status?: string; message?: string
  storyboard_id?: string; creative_review_status?: string|null
  request?: {planner_provider?:'gemini'|'local';planner_model?:string|null;planner_response_mode?:'schema'|'json_only';plan_only?:boolean;remote_processing_consent?:{confirmed?:boolean}}
  checkpoint?:{understand_story?:{title:string;logline:string;visual_style:string;scenes:{title:string;story_beat:string;shots:{title:string;prompt:string;framing:string}[]}[]}}
  planner_request?: {status:string;provider:string;model:string;revision:number;schema_version:string;response_mode?:string;attempts:number;remaining_attempts:number;error_code:string|null;error_details?:{http_status?:number;provider_status?:string;reason?:string;field_paths?:string[];category?:string;response_mode?:string;validation_attempts?:{attempt:number;issues:{path:string;type:string}[]}[]}|null}|null
  planner_attempts?: {revision:number;provider:string;model:string;schema_version:string;response_mode?:string;status:string;attempts:number;remaining_attempts:number;error_code:string|null;error_details?:Record<string,unknown>|null;consent:Record<string,unknown>|null}[]
  planner_schema_current?:string;planner_attempt_limit?:number
  generation_progress?: {total:number;verified:number;queued:number;failed:number;progress:number;shots:{shot_id:string;job_id?:string|null;verified:boolean;reviewability_reason?:string|null;title:string;scene_title:string;status:string;progress:number;events:{time:string;level:string;message:string}[]}[]}|null
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

export default function CreateFilmPage({project,presets,onMessage,onNavigate,onReview}:{project:Project|null;presets:Preset[];onMessage:(message:string)=>void;onNavigate:(page:string)=>void;onReview:(runId:string,shotId:string)=>void}){
  const [prompt,setPrompt]=useState('');const [references,setReferences]=useState<Reference[]>([]);const [selectedRefs,setSelectedRefs]=useState<string[]>([])
  const [preset,setPreset]=useState('Short');const [maxShots,setMaxShots]=useState(1);const [maxRetries,setMaxRetries]=useState(1);const [maxRuntime,setMaxRuntime]=useState(3600);const [maxBytes,setMaxBytes]=useState(8*1024**3)
  const [runs,setRuns]=useState<FilmRun[]>([]);const [capabilities,setCapabilities]=useState<Capabilities|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState('')
  const [showArchived,setShowArchived]=useState(false)
  const [plannerProvider,setPlannerProvider]=useState<'gemini'|'local'>('gemini');const [geminiConfig,setGeminiConfig]=useState<StoryPlannerConfig|null>(null);const [remoteConsent,setRemoteConsent]=useState(false);const [responseMode,setResponseMode]=useState<'schema'|'json_only'>('schema')
  const pendingRequest=useRef<{signature:string;key:string}|null>(null)
  const projectId=project?.id
  const load=useCallback(async()=>{
    if(!projectId){setRuns([]);setReferences([]);return}
    try{const [runList,referenceList]=await Promise.all([
      requestJson<FilmRun[]>(`/api/projects/${projectId}/film-runs${showArchived?'?include_archived=true':''}`),
      requestJson<Reference[]>(`/api/projects/${projectId}/references`),
    ]);setRuns(runList.filter(run=>showArchived?!!run.archived_at:!run.archived_at));setReferences(referenceList.filter(ref=>ref.mime_type.startsWith('image/')))}catch(e){setError(e instanceof Error?e.message:String(e))}
  },[projectId,showArchived])
  useEffect(()=>{
    setPrompt('');setSelectedRefs([]);pendingRequest.current=null
    if(projectId){try{const saved=localStorage.getItem(`framework-film-idempotency:${projectId}`);if(saved)pendingRequest.current=JSON.parse(saved)}catch{}}
    void load();const timer=window.setInterval(()=>void load(),2500);return()=>clearInterval(timer)
  },[load,projectId])
  useEffect(()=>{void requestJson<Capabilities>('/api/film-orchestration/capabilities').then(setCapabilities).catch(e=>setError(e instanceof Error?e.message:String(e)))},[])
  useEffect(()=>{void requestJson<StoryPlannerConfig>('/api/story-planner/config').then(setGeminiConfig).catch(e=>setError(e instanceof Error?e.message:String(e)))},[])
  const selectedPreset=presets.find(item=>item.name===preset)
  function toggleReference(id:string){setSelectedRefs(current=>current.includes(id)?current.filter(item=>item!==id):[...current,id])}
  async function createFilm(planOnly=false,freshFrom?:FilmRun){
    if(!project||!(freshFrom?.prompt??prompt).trim())return
    const requestProvider=freshFrom?.request?.planner_provider??plannerProvider
    const requestPlanOnly=freshFrom?!!freshFrom.request?.plan_only:planOnly
    const requestPreset=freshFrom?.preset??preset
    const budget=freshFrom?.budget??{max_shots:maxShots,max_retries_per_shot:maxRetries,max_generation_seconds:maxRuntime,max_output_bytes:maxBytes}
    const requestModel=requestProvider==='gemini'?(geminiConfig?.gemini.model??freshFrom?.request?.planner_model??null):null
    const requestResponseMode=requestProvider==='gemini'?(freshFrom?.request?.planner_response_mode??responseMode):'schema'
    const consentGiven=requestProvider==='gemini'&&(remoteConsent||!!freshFrom?.request?.remote_processing_consent?.confirmed)
    const input={prompt:(freshFrom?.prompt??prompt).trim(),reference_ids:freshFrom?.reference_ids??selectedRefs,preset:requestPreset,budget,planner_provider:requestProvider,
      planner_model:requestModel,planner_response_mode:requestResponseMode,plan_only:requestPlanOnly,
      remote_processing_consent:consentGiven}
    const signature=JSON.stringify(input)
    const signatureHash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(signature)))).map(value=>value.toString(16).padStart(2,'0')).join('')
    if(freshFrom||!pendingRequest.current||pendingRequest.current.signature!==signatureHash){pendingRequest.current={signature:signatureHash,key:crypto.randomUUID()}}
    try{localStorage.setItem(`framework-film-idempotency:${project.id}`,JSON.stringify(pendingRequest.current))}catch{}
    setBusy(true);setError('')
    try{
      const preview=await requestJson<Preview>(`/api/projects/${project.id}/film-runs/preview`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...input,idempotency_key:pendingRequest.current.key})})
      const timeEstimate=preview.generation_time_estimate_seconds==null
        ? 'No matching local benchmark is available, so wall-clock time cannot be estimated honestly.'
        : `Measured estimate: about ${Math.ceil(preview.generation_time_estimate_seconds/60)} minutes from local benchmark history.`
      const sizeEstimate=preview.estimated_output_bytes==null
        ? `No matching verified project output-size sample is available; output storage cap is ${formatBytes(budget.max_output_bytes)}.`
        : `Estimated outputs: about ${formatBytes(preview.estimated_output_bytes)} from ${preview.storage_sample_count} verified project sample(s); storage cap ${formatBytes(budget.max_output_bytes)}.`
      if(!requestPlanOnly&&!preview.disk_sufficient_for_budget)throw new Error(`The storage cap (${formatBytes(budget.max_output_bytes)}) exceeds available space (${formatBytes(preview.available_output_bytes)}). Lower the cap or free disk space, then retry.`)
      if(!requestPlanOnly&&preview.estimated_output_within_cap===false)throw new Error('Measured output-size samples exceed this storage cap. Increase the cap or reduce the shot/retry limits, then retry.')
      if(!requestPlanOnly&&preview.generation_time_within_cap===false)throw new Error('The measured generation estimate exceeds this time cap. Increase the cap or reduce the shot/retry limits, then retry.')
      const requestPresetInfo=presets.find(item=>item.name===requestPreset)
      const presetWarning=requestPresetInfo?.visual_drift_warning?'\nWARNING: Experimental preset may increase visual drift.':''
      const confirmation=`${freshFrom?'Start a fresh run with this saved prompt?':requestPlanOnly?'Plan this story only?':'Create this film request?'}

${freshFrom?'This creates a new film run and idempotency key using the current FRAMEWORK planning schema and selected Gemini model. The saved prompt, references, and generation budget are reused; prior run history is preserved.':''}

Maximum authorized shots: ${budget.max_shots}
Maximum footage at ${requestPreset}: ${preview.maximum_footage_seconds.toFixed(1)} seconds
${timeEstimate}
${sizeEstimate}
Generation time cap: ${Math.ceil(budget.max_generation_seconds/60)} minutes
Available output disk: ${formatBytes(preview.available_output_bytes)}
Storage hard cap: ${formatBytes(budget.max_output_bytes)}
Retries per shot: ${budget.max_retries_per_shot}${presetWarning}

Planner: ${requestProvider==='gemini'?`Gemini API${requestModel?` · ${requestModel}`:''} · ${requestResponseMode==='schema'?'provider JSON Schema':'JSON-only with strict local validation'}; one authorized planning action can include one bounded repair request`:'configured local planner'}

${requestProvider==='gemini'?'This confirmed action sends the story prompt, project style text, preset/budget, and opaque selected-reference IDs/categories to Google. No images, generated media, filenames, paths, reference notes or project database are uploaded. Gemini planning may incur charges; the account tier and exact price are not known to FRAMEWORK.':'Story planning stays local.'} ${requestPlanOnly?'This is Plan Only: no LTX work is authorized. A valid plan will be saved for review, and generation requires a separate confirmation.':'LTX generation and Remotion rendering remain local.'}

Technical checks do not approve creative quality; the run will stop for creative review after generation.`
      if(!window.confirm(confirmation))return
      const run=await requestJson<FilmRun>(`/api/projects/${project.id}/film-runs`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...input,idempotency_key:pendingRequest.current.key,initial_confirmation:true})})
      pendingRequest.current=null
      try{localStorage.removeItem(`framework-film-idempotency:${project.id}`)}catch{}
      await load();onMessage(run.deduplicated?'Reused the existing active run; no duplicate jobs were created.':run.message??'Film request saved.')
    }catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy(false)}
  }
  async function control(run:FilmRun,action:'pause'|'resume'|'cancel'){
    if(action==='resume'&&run.stage==='understand_story'&&run.request?.planner_provider==='gemini'&&run.planner_request&&['started','failed'].includes(run.planner_request.status)&&!['missing_key','model_missing','model_unavailable'].includes(run.planner_request.error_code??'')){
      if(!window.confirm('The previous Gemini request may have reached Google and may be billable. FRAMEWORK will not retry automatically. Authorize one new explicit planning attempt?'))return
    }
    setBusy(true);setError('')
    try{const updated=await requestJson<FilmRun>(`/api/film-runs/${run.id}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:run.revision})});setRuns(current=>current.map(item=>item.id===updated.id?updated:item));onMessage(`Film request ${action}d.`)}catch(e){setError(e instanceof Error?e.message:String(e));await load()}finally{setBusy(false)}
  }
  async function archiveRun(run:FilmRun,archived:boolean){
    const action=archived?'archive':'restore'
    const confirmation=archived
      ?`Hide this ${run.status.replaceAll('_',' ')} film run from the normal view? Its run, Gemini attempts, logs, LTX jobs, references, and media remain in history and on disk.`
      :'Restore this film run to the normal view? Its history and media remain unchanged.'
    if(!window.confirm(confirmation))return
    setBusy(true);setError('')
    try{await requestJson<FilmRun>(`/api/projects/${run.project_id}/film-runs/${run.id}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:run.revision})});await load();onMessage(archived?'Film run archived; history and media were preserved.':'Film run restored.')}
    catch(e){setError(e instanceof Error?e.message:String(e));await load()}finally{setBusy(false)}
  }
  async function authorizeGeneration(run:FilmRun){
    const confirmed=window.confirm(`Authorize local LTX generation from the saved plan?\n\nMaximum shots: ${run.budget.max_shots}\nGeneration time cap: ${Math.ceil(run.budget.max_generation_seconds/60)} minutes\nOutput storage cap: ${formatBytes(run.budget.max_output_bytes)}\n\nThis uses the existing validated plan and does not send another Gemini request. The local resource checks will run again. Confirm to allow shot jobs to be queued.`)
    if(!confirmed)return
    setBusy(true);setError('')
    try{const updated=await requestJson<FilmRun>(`/api/film-runs/${run.id}/authorize`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:run.revision,confirmed:true,budget_acknowledged:true})});setRuns(current=>current.map(item=>item.id===updated.id?updated:item));onMessage('Local generation separately authorized; shot planning can now continue.')}
    catch(e){setError(e instanceof Error?e.message:String(e));await load()}finally{setBusy(false)}
  }
  async function replan(run:FilmRun,model:string,mode:'schema'|'json_only'){
    const revisionKey=`framework-film-replan:${run.id}:${run.revision}:${model}:${mode}`
    let idempotencyKey=''
    try{idempotencyKey=localStorage.getItem(revisionKey)??''}catch{}
    if(!idempotencyKey){idempotencyKey=crypto.randomUUID();try{localStorage.setItem(revisionKey,idempotencyKey)}catch{}}
    const confirmed=window.confirm(`Authorize a new Gemini planning revision using ${model} with ${mode==='json_only'?'JSON-only output and strict local validation':'provider JSON Schema'}?\n\nThis sends this run's saved story prompt, project style text, preset/budget, and opaque reference IDs/categories to Google. It does not upload media. The request may incur a charge. One explicitly authorized planner action may include one bounded repair request if the returned plan fails local validation.\n\nThe original ${run.budget.max_shots}-shot generation budget is unchanged. No LTX job is created until the plan is valid and you separately authorize local generation. Test connection only lists models; this is a real structured-planning request.`)
    if(!confirmed)return
    setBusy(true);setError('')
    try{
      const updated=await requestJson<FilmRun>(`/api/film-runs/${run.id}/replan`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:run.revision,planner_model:model,response_mode:mode,idempotency_key:idempotencyKey,confirm_billable_planning:true})})
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
          <div className="create-film-estimate"><b>Approval preview</b><span>Up to {(maxShots*((selectedPreset?.frames??49)/(selectedPreset?.fps??24))).toFixed(1)}s of requested footage · {Math.ceil(maxRuntime/60)} minute time cap · {formatBytes(maxBytes)} output cap.</span><small>Plan Only does not authorize LTX generation; disk and benchmark checks are repeated before separate generation authorization. Create Film uses this budget after its confirmation.</small></div>
        </details>
        {plannerProvider==='gemini'&&<label className="setting-field create-film-response-mode"><span>GEMINI RESPONSE FORMAT</span><select value={responseMode} onChange={event=>setResponseMode(event.target.value as 'schema'|'json_only')}><option value="schema">Structured JSON Schema</option><option value="json_only">JSON-only fallback · strict local validation</option></select><small>JSON-only omits provider-side schema enforcement if this model rejects it. FRAMEWORK still validates the full StoryPlan before saving or generation.</small></label>}
        {plannerProvider==='local'&&<div className="create-film-limitation"><ShieldCheck size={15}/><div><b>Optional local planner · {capabilities?.story_planners?.local.status.replaceAll('_',' ')??'checking'}</b><span>{capabilities?.story_planners?.local.detail??'Checking local planner availability.'} Every result is schema and project-scope validated.</span></div></div>}
        <div className="create-film-actions"><button className="button primary" disabled={busy||!prompt.trim()||(plannerProvider==='gemini'&&!remoteConsent)} onClick={()=>void createFilm(false)}>{busy?<LoaderCircle className="spin" size={15}/>:<WandSparkles size={15}/>} Create Film</button><button className="button secondary" disabled={busy||!prompt.trim()||(plannerProvider==='gemini'&&!remoteConsent)} onClick={()=>void createFilm(true)}>{busy?<LoaderCircle className="spin" size={15}/>:<ShieldCheck size={15}/>} Plan Only / Test Planner</button><span>Plan Only saves a validated plan and stops before LTX generation. Create Film includes your confirmed local generation budget. Gemini calls require the explicit opt-in above; creative review remains separate.</span></div>
      </>}
      {error&&<div className="model-caution"><AlertTriangle size={13}/>{error}</div>}
    </section>
    <section className="panel create-film-runs"><div className="section-kicker">DURABLE RUN HISTORY</div><div className="film-run-list-heading"><div><h2>Film runs</h2><p>Film runs record planning and production. Individual LTX jobs and their logs remain in Job Queue.</p></div><button className="button secondary mini" disabled={busy} onClick={()=>setShowArchived(value=>!value)}>{showArchived?'Hide archived':'Show archived'}</button></div>{runs.length?runs.map(run=><FilmRunCard key={run.id} run={run} busy={busy} models={geminiConfig?.gemini.models??[]} geminiConnected={geminiConfig?.gemini.status==='connected'} onControl={control} onReplan={replan} onAuthorizeGeneration={authorizeGeneration} onArchive={archiveRun} onStartFresh={source=>void createFilm(true,source)} onReview={onReview}/>):<p className="empty-workspace">{showArchived?'No archived film runs in this project.':'No active film runs in this project yet.'}</p>}</section>
  </div>
}

function FilmRunCard({run,busy,models,geminiConnected,onControl,onReplan,onAuthorizeGeneration,onArchive,onStartFresh,onReview}:{run:FilmRun;busy:boolean;models:{id:string;display_name:string}[];geminiConnected:boolean;onControl:(run:FilmRun,action:'pause'|'resume'|'cancel')=>void;onReplan:(run:FilmRun,model:string,mode:'schema'|'json_only')=>void;onAuthorizeGeneration:(run:FilmRun)=>void;onArchive:(run:FilmRun,archived:boolean)=>void;onStartFresh:(run:FilmRun)=>void;onReview:(runId:string,shotId:string)=>void}){
  const [replanModel,setReplanModel]=useState('')
  const [replanMode,setReplanMode]=useState<'schema'|'json_only'>('schema')
  const modelIds=models.map(model=>model.id).join('|')
  useEffect(()=>{const replacement=models.find(model=>model.id!==run.request?.planner_model)?.id??run.request?.planner_model??'';setReplanModel(replacement);setReplanMode(run.planner_request?.error_code==='schema_rejected'?'json_only':run.request?.planner_response_mode??'schema')},[run.request?.planner_model,run.request?.planner_response_mode,run.planner_request?.error_code,modelIds])
  const stageIndex=Math.max(0,run.completed_stages.length)
  const progress=Math.round(stageIndex/10*100)
  const blocked=run.status==='needs_attention'
  const isGeminiPlanningFailure=['needs_attention','failed'].includes(run.status)&&run.stage==='understand_story'&&run.request?.planner_provider==='gemini'&&!!run.planner_request&&['failed','started'].includes(run.planner_request.status)
  const schemaMatches=run.planner_request?.schema_version===run.planner_schema_current
  const canRetry=blocked&&((run.stage==='understand_story'&&run.request?.planner_provider==='gemini'&&schemaMatches&&(run.planner_request?.remaining_attempts??0)>0&&run.planner_request?.error_code!=='schema_rejected')||run.stage==='generate_shots')&&!['not_configured','executable_missing','invalid_configuration'].includes(run.planner_status??'')
  const canReplan=isGeminiPlanningFailure&&geminiConnected
  const planOnlyReady=run.status==='awaiting_authorization'&&run.stage==='define_visual_bible'&&run.request?.plan_only===true&&!!run.checkpoint?.understand_story
  const canArchive=!!run.archived_at||['needs_attention','failed','cancelled','completed'].includes(run.status)
  const canStartFresh=isGeminiPlanningFailure&&(run.generation_jobs_created??0)===0
  const diagnostic:Record<string,string>={model_or_endpoint_incompatible:'Gemini returned 404: check model availability, API enablement, and the Google project/key access. This is not a schema rejection.',model_invalid:'The selected model is no longer in the compatible account model list. Test the connection and choose a currently listed text-output model.',model_unavailable:'The selected model is unavailable to this account. Test the connection and switch to a listed text-output model.',model_parameter_rejected:'Gemini rejected the model request parameter (400); this is a model or endpoint issue.',schema_rejected:'Gemini rejected the JSON Schema request (400). This does not prove the model is invalid; try the JSON-only fallback to isolate provider schema support.',invalid_request:'Gemini rejected a request parameter (400). Use the sanitized field path below to identify which parameter Google rejected.',service_unavailable:'Gemini returned a transient service/capacity error (such as HTTP 503). This is not a model-access or schema failure. Retry only by explicitly authorizing a new planning request.',non_text_response:'Gemini returned HTTP 200 with audio output but no text. This is a successful audio response, not a text-generation failure; choose a text-output planner.',no_text_response:'Gemini returned HTTP 200 without usable text. The response did not prove an audio-only model; check provider safety/output details before changing models.',rate_limit:'Google returned its account rate or quota limit (429). This is the provider limit, not a FRAMEWORK cooldown.',auth_denied:'Gemini rejected the key or account permissions. Fix the connection in Settings before retrying.',invalid_output:'The response did not pass FRAMEWORK’s strict StoryPlan checks after the bounded repair. No LTX jobs were queued.',incomplete_output:'Gemini stopped before completing the structured plan. This is an output or response-budget issue; the incomplete plan was not accepted.',blocked_content:'Gemini blocked the prompt under its safety policy. Review the story prompt before authorizing another request.',timeout:'The request timed out and may have reached Google. Its outcome may be billable; it was not resent automatically.',provider_error:'Gemini planning failed; provider response text was omitted to protect private data.'}
  return <article className="film-run-card">
    <header><div><b>{run.prompt.length>100?`${run.prompt.slice(0,100)}…`:run.prompt}</b><small>{new Date(run.updated_at).toLocaleString()} · {run.preset} · {run.budget.max_shots} shot cap</small></div><span className={`job-status ${run.status}`}>{run.status.replaceAll('_',' ')}</span></header>
    <div className="film-run-progress"><span style={{width:`${progress}%`}}/></div>
    <ol className="film-run-timeline">{["understand_story","define_visual_bible","plan_scenes","anchor_visuals","generate_shots","inspect_clips","creative_review","repair_failed_shots","compose_manifest","render_film","done"].map((stage,index)=>{
      const complete=run.completed_stages.includes(stage);const current=run.stage===stage
      return <li className={`${complete?'complete':''} ${current?'current':''}`} key={stage}><span>{complete?<Check size={12}/>:index+1}</span><small>{stageLabels[stage]}</small></li>
    })}</ol>
    {blocked&&<div className="create-film-limitation"><AlertTriangle size={14}/><span>{run.error??'Waiting for a verified general story-planning capability. No shot jobs have been queued.'}</span></div>}
    {isGeminiPlanningFailure&&(run.generation_jobs_created??0)===0&&<div className="create-film-limitation"><AlertTriangle size={14}/><span>Failed Gemini planning · 0 LTX generation jobs created. The planner attempt is separate from the local job queue.</span></div>}
    {planOnlyReady&&<section className="plan-only-result"><div className="section-kicker">PLAN ONLY · VALIDATED · NO VIDEO JOBS</div><h3>{run.checkpoint?.understand_story?.title}</h3><p>{run.checkpoint?.understand_story?.logline}</p>{run.checkpoint?.understand_story?.scenes.map((scene,index)=><details key={`${index}-${scene.title}`} open={index===0}><summary>Scene {index+1}: {scene.title} · {scene.shots.length} shot(s)</summary><ol>{scene.shots.map((shot,shotIndex)=><li key={`${shotIndex}-${shot.title}`}><b>{shot.title}</b><small>{shot.framing}</small><p>{shot.prompt}</p></li>)}</ol></details>)}<small>This strict local StoryPlan checkpoint is saved. Generation is waiting for a separate authorization. No LTX jobs have been created.</small></section>}
    {isGeminiPlanningFailure&&<section className="planner-recovery-panel">
      <div className="section-kicker">GEMINI PLANNING DIAGNOSTIC</div>
      <p><b>{diagnostic[run.planner_request?.error_code??'']??'Planning stopped before any shot job was queued.'}</b></p>
      <p>Run is pinned to <code>{run.planner_request?.model??run.request?.planner_model??'no model'}</code> · schema <code>{run.planner_request?.schema_version??'unknown'}</code> · revision {run.planner_request?.revision??1}.</p>
      <p>Revision {run.planner_request?.revision??1}: {run.planner_request?.attempts??0}/{run.planner_attempt_limit??3} explicit attempts used, {run.planner_request?.remaining_attempts??0} remaining in this revision. FRAMEWORK has no hourly planner cap; Google account quota and 429 responses still apply.</p>
      {run.planner_request?.error_details&&<details open><summary>Sanitized provider diagnostics</summary><ul>{run.planner_request.error_details.http_status&&<li>HTTP status: {run.planner_request.error_details.http_status}</li>}{run.planner_request.error_details.provider_status&&<li>Provider status: <code>{run.planner_request.error_details.provider_status}</code></li>}{run.planner_request.error_details.reason&&<li>Reason: <code>{run.planner_request.error_details.reason}</code></li>}{run.planner_request.error_details.field_paths?.map(field=><li key={field}>Rejected field: <code>{field}</code></li>)}{run.planner_request.error_details.validation_attempts?.map(item=><li key={`validation-${item.attempt}`}><b>Response {item.attempt} validation:</b> {item.issues.map(issue=><span key={`${issue.path}-${issue.type}`}> <code>{issue.path}</code> · <code>{issue.type}</code>; </span>)}</li>)}{run.planner_request.error_details.category&&<li>Classification: {run.planner_request.error_details.category.replaceAll('_',' ')}</li>}</ul><small>Provider response text, prompt content, credentials and private metadata are not shown.</small></details>}
      {run.planner_attempts&&run.planner_attempts.length>0&&<details><summary>Planner revision history ({run.planner_attempts.length})</summary><ol className="planner-attempt-history">{run.planner_attempts.map(item=><li key={item.revision}><b>Revision {item.revision}</b> · {item.model} · {item.schema_version} · {item.response_mode??'schema'} · {item.attempts} attempt(s) · {item.status}{item.error_code?` · ${item.error_code}`:''}{Array.isArray(item.error_details?.field_paths)?` · rejected ${String(item.error_details.field_paths[0])}`:''}</li>)}</ol></details>}
      <div className="planner-recovery-controls"><label className="setting-field"><span>ACCOUNT-AVAILABLE MODEL FOR THIS RUN</span><select value={replanModel} disabled={busy||!geminiConnected||!models.length} onChange={event=>setReplanModel(event.target.value)}>{models.map(model=><option key={model.id} value={model.id}>{model.display_name} · {model.id}{model.id===run.planner_request?.model?' · current':''}</option>)}</select></label>
        <label className="setting-field"><span>RESPONSE FORMAT FOR THIS ATTEMPT</span><select value={replanMode} disabled={busy} onChange={event=>setReplanMode(event.target.value as 'schema'|'json_only')}><option value="schema">Structured JSON Schema</option><option value="json_only">JSON-only fallback · strict local validation</option></select></label>
        <button className="button primary mini" disabled={busy||!canReplan||!replanModel} onClick={()=>onReplan(run,replanModel,replanMode)}>{replanModel!==run.planner_request?.model?'Switch model and retry planning':replanMode==='json_only'?'Retry as JSON-only on this model':run.planner_request?.schema_version!==run.planner_schema_current?'Retry with updated schema':'Authorize another planning revision'}</button></div>
      {geminiConnected&&models.length>0&&<p>These account-listed models expose the supported planning endpoint, but model listing does not guarantee acceptance of FRAMEWORK’s structured StoryPlan schema. Switching performs a real planning request only after confirmation; it may be billable.</p>}
      {!geminiConnected&&<p>Connect Gemini and run Test connection to list account models. That connection test does not send a planning request.</p>}
      {geminiConnected&&models.every(model=>model.id===run.planner_request?.model)&&<p>No alternate models are available in the current account list. You can retry the same account-listed model using JSON-only format; this changes the tracked response-mode revision, not the model.</p>}
      <small>Each action creates a new durable revision and preserves all prior attempt counts, errors, model, schema, and usage. There is no FRAMEWORK cooldown or revision ceiling. Every request requires confirmation and may be billable. Connection testing only lists models; it does not test schema compatibility.</small>
    </section>}
    {run.status==='needs_review'&&<FilmRunReviewAction runId={run.id} shots={run.generation_progress?.shots??[]} onReview={onReview}/>}
    {run.generation_progress&&<div className="film-run-shot-progress"><header><b>Local generation queue</b><span>{run.generation_progress.verified}/{run.generation_progress.total} technically verified · {run.generation_progress.progress}%</span></header>{run.generation_progress.shots.map((shot,index)=><div className="film-run-shot" key={`${index}-${shot.title}`}><span>{shot.scene_title} / {shot.title}</span><b className={`job-status ${shot.status}`}>{shot.status} {shot.status==='running'?`${shot.progress}%`:''}</b>{shot.shot_id&&<button className="button secondary mini" onClick={()=>onReview(run.id,shot.shot_id)}>{shot.verified?'Review clip':'Check review blocker'}</button>}{!shot.verified&&shot.reviewability_reason&&<small>{shot.reviewability_reason}</small>}{shot.events.slice(-1).map((event,eventIndex)=><small key={eventIndex}>{event.message}</small>)}</div>)}</div>}
    <div className="create-film-run-meta"><span>Authorization {run.status==='awaiting_authorization'?'not granted':'recorded'}</span><span>Generation jobs created: {run.generation_jobs_created??0}</span><span>Technical verification and creative approval remain separate.</span></div>
    {run.events.length>0&&<details><summary>Checkpoint log</summary><div className="create-film-log">{run.events.map((event,index)=><p key={`${event.revision}-${index}`}><time>{new Date(event.time).toLocaleTimeString()}</time> {event.message}</p>)}</div></details>}
    <footer>{planOnlyReady&&<button className="button primary mini" disabled={busy} onClick={()=>onAuthorizeGeneration(run)}><WandSparkles size={12}/> Authorize local generation</button>}{canStartFresh&&<button className="button primary mini" disabled={busy||!geminiConnected} onClick={()=>onStartFresh(run)}><WandSparkles size={12}/> Start Fresh</button>}{((activeStatuses.has(run.status)&&run.status!=='awaiting_authorization')||canRetry)&&<button className="button secondary mini" disabled={busy} onClick={()=>onControl(run,run.status==='paused'||blocked?'resume':'pause')}>{run.status==='paused'||blocked?<Play size={12}/>:<Pause size={12}/>} {blocked?'Retry stage':run.status==='paused'?'Resume':'Pause'}</button>}{!['cancelled','completed','failed','needs_review'].includes(run.status)&&<button className="button secondary mini" disabled={busy} onClick={()=>onControl(run,'cancel')}><Square size={12}/> Cancel run</button>}{run.archived_at?<button className="button secondary mini" disabled={busy} onClick={()=>onArchive(run,false)}>Restore</button>:canArchive&&<button className="button secondary mini" disabled={busy} onClick={()=>onArchive(run,true)}>Archive / Hide</button>}</footer>
  </article>
}
