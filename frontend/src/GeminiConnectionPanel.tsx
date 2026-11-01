import { useEffect, useState } from 'react'
import { AlertTriangle, Check, KeyRound, LoaderCircle, RefreshCw, Trash2, Zap } from 'lucide-react'

export type GeminiConfig = { status:string; source:string|null; model:string|null; models:{id:string;display_name:string;output_modalities:string[];text_output_source:string}[]; detail:string }
export type StoryPlannerConfig = { provider_default:string; gemini:GeminiConfig; gemini_timeout_seconds:number; local:{available:boolean;status:string;detail:string}; privacy:{remote_fields:string[];omitted_fields:string[];uploads_media:boolean} }

async function responseBody(response:Response){
  const body=await response.json().catch(()=>({}))
  if(!response.ok){const detail=body.detail;const message=typeof detail==='string'?detail:detail?.message??'The local API could not complete the Gemini settings action';const diagnostics=detail?.diagnostics;const safe=[diagnostics?.http_status?`HTTP ${diagnostics.http_status}`:null,diagnostics?.provider_status?`provider ${diagnostics.provider_status}`:null,diagnostics?.reason?`reason ${diagnostics.reason}`:null,diagnostics?.category?`category ${diagnostics.category}`:null,diagnostics?.finish_reasons?.length?`finish ${diagnostics.finish_reasons.join(', ')}`:null,diagnostics?.prompt_block_reason?`block ${diagnostics.prompt_block_reason}`:null,diagnostics?.thoughts_token_count!=null?`thinking tokens ${diagnostics.thoughts_token_count}`:null,diagnostics?.max_output_tokens?`output cap ${diagnostics.max_output_tokens}`:null,diagnostics?.output_modalities?.length?`output ${diagnostics.output_modalities.join(', ')}`:null,diagnostics?.endpoint_host&&diagnostics?.endpoint_path?`route https://${diagnostics.endpoint_host}${diagnostics.endpoint_path}`:null].filter(Boolean);const error= new Error(`${message}${safe.length?`\n${safe.join(' · ')}`:''}`) as Error&{code?:string};error.code=detail?.code;throw error}
  return body
}

type EndpointProbe = {success:boolean;http_status:number;model:string;api_version:string;endpoint_host:string;endpoint_path:string;request_shape:string;thinking_level:string|null;response_text:string;response_metadata:{finish_reasons:string[];output_modalities:string[];prompt_token_count?:number;candidates_token_count?:number;thoughts_token_count?:number;total_token_count?:number;max_output_tokens:number}}

export default function GeminiConnectionPanel({compact=false,onConfig}:{compact?:boolean;onConfig?:(config:StoryPlannerConfig)=>void}){
  const [config,setConfig]=useState<StoryPlannerConfig|null>(null)
  const [apiKey,setApiKey]=useState('')
  const [busy,setBusy]=useState('')
  const [message,setMessage]=useState('')
  const [error,setError]=useState('')
  const [endpointProbe,setEndpointProbe]=useState<EndpointProbe|null>(null)
  const [endpointProbeError,setEndpointProbeError]=useState('')
  const [probeStartedAt,setProbeStartedAt]=useState<number|null>(null)
  const [probeElapsedSeconds,setProbeElapsedSeconds]=useState(0)
  async function load(){
    const response=await fetch('/api/story-planner/config')
    const result=await responseBody(response) as StoryPlannerConfig
    setConfig(result);onConfig?.(result)
  }
  useEffect(()=>{void load().catch(e=>setError(e instanceof Error?e.message:String(e)))},[])
  useEffect(()=>{if(busy!=='probe'||probeStartedAt===null)return;const timer=window.setInterval(()=>setProbeElapsedSeconds(Math.floor((Date.now()-probeStartedAt)/1000)),1000);return()=>window.clearInterval(timer)},[busy,probeStartedAt])
  async function connect(){
    const key=apiKey
    setApiKey('') // Keep the one-shot key out of component state while the request runs.
    if(!key.trim())return
    setBusy('connect');setError('');setMessage('')
    try{const response=await fetch('/api/story-planner/gemini/key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key})});const result=await responseBody(response);setMessage(result.detail);await load()}
    catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy('')}
  }
  async function test(){
    setBusy('test');setError('');setMessage('')
    try{const response=await fetch('/api/story-planner/gemini/test',{method:'POST'});const result=await responseBody(response);setMessage(result.detail);await load()}
    catch(e){setError(e instanceof Error?e.message:String(e));await load().catch(()=>{})}finally{setBusy('')}
  }
  async function testGenerationEndpoint(){
    const model=gemini?.model
    if(!model)return
    if(!window.confirm(`Send one real, potentially billable Gemini request using ${model}?\n\nThis endpoint check sends only a fixed harmless text prompt. It includes no story prompt, media, reference, or JSON schema and creates no film run or generation job.`))return
    const started=Date.now();setProbeStartedAt(started);setProbeElapsedSeconds(0);setBusy('probe');setError('');setMessage('');setEndpointProbe(null);setEndpointProbeError('')
    try{const response=await fetch('/api/story-planner/gemini/test-generation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model,confirmed:true})});const result=await responseBody(response) as EndpointProbe;setEndpointProbe(result);setMessage(`Basic generateContent succeeded for ${result.model} in ${Math.max(0,Math.floor((Date.now()-started)/1000))}s (HTTP ${result.http_status}).`)}
    catch(e){const failure=e as Error&{code?:string};setEndpointProbeError(failure.code??'');const elapsed=Math.max(0,Math.floor((Date.now()-started)/1000));setError(`${e instanceof Error?e.message:String(e)}\nRequest ended after ${elapsed}s.`)}finally{setProbeElapsedSeconds(Math.max(0,Math.floor((Date.now()-started)/1000)));setProbeStartedAt(null);setBusy('')}
  }
  async function chooseModel(model:string){
    setBusy('model');setError('');setMessage('');setEndpointProbe(null);setEndpointProbeError('')
    try{const response=await fetch('/api/story-planner/gemini/model',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({model})});await responseBody(response);await load();setMessage('Gemini planning model selected locally.')}
    catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy('')}
  }
  async function remove(){
    if(!window.confirm('Remove the Gemini API key stored by FRAMEWORK? Film plans and production history will remain unchanged.'))return
    setBusy('remove');setError('');setMessage('')
    try{const response=await fetch('/api/story-planner/gemini/key',{method:'DELETE'});await responseBody(response);await load();setMessage('Stored Gemini key removed.')}
    catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy('')}
  }
  const gemini=config?.gemini
  const connected=gemini?.status==='connected'
  const needsAttention=!!gemini&&['missing_key','no_text_models','connection_failed','storage_error'].includes(gemini.status)
  return <section className={`panel gemini-connection-panel ${compact?'compact':''}`}>
    <div className="section-kicker">OPTIONAL CLOUD STORY PLANNING</div>
    <div className="gemini-panel-heading"><div><h3>Connect Gemini</h3><p>Recommended story planner. Video generation and rendering stay on this Mac.</p></div><span className={`job-status ${connected?'completed':needsAttention?'needs_attention':'queued'}`}>{gemini?.status?.replaceAll('_',' ')??'checking'}</span></div>
    <div className="gemini-status-copy">{gemini?.detail??'Checking backend-only credential status…'}{gemini?.source&&<small>Credential source: {gemini.source==='local_file'?'protected local file':'backend environment'} · never returned by the API.</small>}<small>Test connection only checks the key and lists account models. It does not send a StoryPlan or verify structured-planning compatibility.</small></div>
    {!connected&&<div className="gemini-key-entry">{gemini?.source!=='environment'&&<><label className="setting-field"><span>YOUR GEMINI API KEY</span><input type="password" autoComplete="new-password" spellCheck={false} value={apiKey} onChange={e=>setApiKey(e.target.value)} placeholder="Entered once and stored by the backend" aria-label="Gemini API key"/></label><button className="button primary" disabled={busy!==''||!apiKey.trim()} onClick={()=>void connect()}>{busy==='connect'?<LoaderCircle className="spin" size={14}/>:<KeyRound size={14}/>} Connect Gemini</button></>}{gemini?.source&&<button className="button secondary mini" disabled={busy!==''} onClick={()=>void test()}>{busy==='test'?<LoaderCircle className="spin" size={13}/>:<RefreshCw size={13}/>} Test connection</button>}</div>}
    {connected&&<div className="gemini-model-row"><label className="setting-field"><span>TEXT-OUTPUT STORY PLANNER MODEL</span><select value={gemini?.model??''} disabled={busy!==''} onChange={e=>void chooseModel(e.target.value)}>{gemini?.models.map(model=><option value={model.id} key={model.id}>{model.display_name} · {model.id} · {model.output_modalities?.length?model.output_modalities.join('+')+' metadata':'text by name filter'}</option>)}</select></label><div className="gemini-actions"><button className="button secondary mini" disabled={busy!==''} onClick={()=>void test()}>{busy==='test'?<LoaderCircle className="spin" size={13}/>:<RefreshCw size={13}/>} Test connection</button>{gemini?.source==='local_file'&&<button className="button secondary mini" disabled={busy!==''} onClick={()=>void remove()}><Trash2 size={13}/> Remove key</button>}</div></div>}
    {connected&&<div className="gemini-endpoint-probe"><div><b>Basic generation diagnostic</b><small>One confirmed request, fixed text only. No story, schema, media, film run, or local generation. Uses low thinking and a 512-token output cap; a 503 retry is manual and confirmed.</small></div><button className="button secondary mini" disabled={busy!==''||!gemini?.model} onClick={()=>void testGenerationEndpoint()}>{busy==='probe'?<LoaderCircle className="spin" size={13}/>:<Zap size={13}/>} {busy==='probe'?'Testing endpoint…':endpointProbeError==='service_unavailable'?'Retry short endpoint test':'Test Gemini generation endpoint'}</button></div>}
    {busy==='probe'&&<div className="gemini-probe-progress" role="status" aria-live="polite"><span className="gemini-probe-indicator"/><div><b>Waiting for Gemini response · {probeElapsedSeconds}s elapsed</b><small>One request is in progress. The backend timeout is {config?.gemini_timeout_seconds??120}s. FRAMEWORK will not retry automatically.</small></div></div>}
    {endpointProbe&&<div className="gemini-endpoint-result"><b>generateContent succeeded · HTTP {endpointProbe.http_status} · {endpointProbe.response_metadata.finish_reasons?.join(', ')||'finish reason unavailable'}</b><code>https://{endpointProbe.endpoint_host}{endpointProbe.endpoint_path}</code><small>SDK {endpointProbe.api_version} · thinking {endpointProbe.thinking_level??'default'} · output cap {endpointProbe.response_metadata.max_output_tokens} · {endpointProbe.model}</small><small>Tokens · prompt {endpointProbe.response_metadata.prompt_token_count??'—'} · visible output {endpointProbe.response_metadata.candidates_token_count??'—'} · thinking {endpointProbe.response_metadata.thoughts_token_count??'—'} · total {endpointProbe.response_metadata.total_token_count??'—'} · finished in {probeElapsedSeconds}s</small><p>{endpointProbe.response_text}</p></div>}
    <details className="gemini-disclosure" open>
      <summary>Privacy, data use and possible cost</summary>
      <p>On an opted-in film run, FRAMEWORK sends the story prompt, project style text, generation preset/budget, and opaque selected-reference IDs/categories to Google for planning. It does not upload reference images, generated media, filenames, file paths, reference notes, or the project database. The local LTX runner and Remotion perform media work on this Mac.</p>
      <p>Gemini usage may incur charges and is subject to account quota. Google says free-tier content may be used to improve products; paid-tier content is not used for that purpose. FRAMEWORK cannot detect your account tier and cannot quote a reliable price, so it records token usage when returned but does not invent a dollar estimate.</p>
      <p><a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noreferrer">Get or manage a Gemini API key</a> · <a href="https://ai.google.dev/gemini-api/docs/pricing" target="_blank" rel="noreferrer">Current pricing and tier details</a></p>
    </details>
    {message&&<div className="gemini-notice"><Check size={13}/>{message}</div>}{error&&<div className="model-caution"><AlertTriangle size={13}/>{error}</div>}
  </section>
}
