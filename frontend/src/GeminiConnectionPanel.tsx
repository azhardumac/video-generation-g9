import { useEffect, useState } from 'react'
import { AlertTriangle, Check, KeyRound, LoaderCircle, RefreshCw, Trash2 } from 'lucide-react'

export type GeminiConfig = { status:string; source:string|null; model:string|null; models:{id:string;display_name:string}[]; detail:string }
export type StoryPlannerConfig = { provider_default:string; gemini:GeminiConfig; local:{available:boolean;status:string;detail:string}; privacy:{remote_fields:string[];omitted_fields:string[];uploads_media:boolean} }

async function responseBody(response:Response){
  const body=await response.json().catch(()=>({}))
  if(!response.ok){const detail=body.detail;throw new Error(typeof detail==='string'?detail:detail?.message??'The local API could not complete the Gemini settings action')}
  return body
}

export default function GeminiConnectionPanel({compact=false,onConfig}:{compact?:boolean;onConfig?:(config:StoryPlannerConfig)=>void}){
  const [config,setConfig]=useState<StoryPlannerConfig|null>(null)
  const [apiKey,setApiKey]=useState('')
  const [busy,setBusy]=useState('')
  const [message,setMessage]=useState('')
  const [error,setError]=useState('')
  async function load(){
    const response=await fetch('/api/story-planner/config')
    const result=await responseBody(response) as StoryPlannerConfig
    setConfig(result);onConfig?.(result)
  }
  useEffect(()=>{void load().catch(e=>setError(e instanceof Error?e.message:String(e)))},[])
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
  async function chooseModel(model:string){
    setBusy('model');setError('');setMessage('')
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
  return <section className={`panel gemini-connection-panel ${compact?'compact':''}`}>
    <div className="section-kicker">OPTIONAL CLOUD STORY PLANNING</div>
    <div className="gemini-panel-heading"><div><h3>Connect Gemini</h3><p>Recommended story planner. Video generation and rendering stay on this Mac.</p></div><span className={`job-status ${connected?'completed':gemini?.status==='missing_key'?'needs_attention':'queued'}`}>{gemini?.status?.replaceAll('_',' ')??'checking'}</span></div>
    <div className="gemini-status-copy">{gemini?.detail??'Checking backend-only credential status…'}{gemini?.source&&<small>Credential source: {gemini.source==='local_file'?'protected local file':'backend environment'} · never returned by the API.</small>}</div>
    {!connected&&<div className="gemini-key-entry">{gemini?.source!=='environment'&&<><label className="setting-field"><span>YOUR GEMINI API KEY</span><input type="password" autoComplete="new-password" spellCheck={false} value={apiKey} onChange={e=>setApiKey(e.target.value)} placeholder="Entered once and stored by the backend" aria-label="Gemini API key"/></label><button className="button primary" disabled={busy!==''||!apiKey.trim()} onClick={()=>void connect()}>{busy==='connect'?<LoaderCircle className="spin" size={14}/>:<KeyRound size={14}/>} Connect Gemini</button></>}{gemini?.source&&<button className="button secondary mini" disabled={busy!==''} onClick={()=>void test()}>{busy==='test'?<LoaderCircle className="spin" size={13}/>:<RefreshCw size={13}/>} Test connection</button>}</div>}
    {connected&&<div className="gemini-model-row"><label className="setting-field"><span>ACCOUNT-AVAILABLE FLASH MODEL</span><select value={gemini?.model??''} disabled={busy!==''} onChange={e=>void chooseModel(e.target.value)}>{gemini?.models.map(model=><option value={model.id} key={model.id}>{model.display_name} · {model.id}</option>)}</select></label><div className="gemini-actions"><button className="button secondary mini" disabled={busy!==''} onClick={()=>void test()}>{busy==='test'?<LoaderCircle className="spin" size={13}/>:<RefreshCw size={13}/>} Test connection</button>{gemini?.source==='local_file'&&<button className="button secondary mini" disabled={busy!==''} onClick={()=>void remove()}><Trash2 size={13}/> Remove key</button>}</div></div>}
    <details className="gemini-disclosure" open>
      <summary>Privacy, data use and possible cost</summary>
      <p>On an opted-in film run, FRAMEWORK sends the story prompt, project style text, generation preset/budget, and opaque selected-reference IDs/categories to Google for planning. It does not upload reference images, generated media, filenames, file paths, reference notes, or the project database. The local LTX runner and Remotion perform media work on this Mac.</p>
      <p>Gemini usage may incur charges and is subject to account quota. Google says free-tier content may be used to improve products; paid-tier content is not used for that purpose. FRAMEWORK cannot detect your account tier and cannot quote a reliable price, so it records token usage when returned but does not invent a dollar estimate.</p>
      <p><a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noreferrer">Get or manage a Gemini API key</a> · <a href="https://ai.google.dev/gemini-api/docs/pricing" target="_blank" rel="noreferrer">Current pricing and tier details</a></p>
    </details>
    {message&&<div className="gemini-notice"><Check size={13}/>{message}</div>}{error&&<div className="model-caution"><AlertTriangle size={13}/>{error}</div>}
  </section>
}
