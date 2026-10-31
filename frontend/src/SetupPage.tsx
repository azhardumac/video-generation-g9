import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Check, Download, LoaderCircle, PackageCheck, RefreshCw, ShieldCheck, Square } from 'lucide-react'

type ComponentStatus={id:string;title:string;status:'installed'|'missing'|'incompatible'|'downloading';detail:string;path:string|null}
type SetupStatus={components:ComponentStatus[];ready:boolean;disk_free_bytes:number;model_disk_free_bytes:number;minimum_non_model_free_bytes:number;disk_sufficient_for_bootstrap:boolean;installing:string|null;install_state:'idle'|'running'|'cancelling'|'completed'|'failed'|'cancelled';download_progress_percent:number|null;logs:string[];exit_code:number|null}

async function getStatus():Promise<SetupStatus>{const response=await fetch('/api/setup/status');const value=await response.json();if(!response.ok)throw new Error(value.detail??'Could not read setup status');return value}
function formatBytes(value:number){const units=['B','KB','MB','GB','TB'];let size=value,index=0;while(size>=1024&&index<units.length-1){size/=1024;index++}return `${size.toFixed(index>1?1:0)} ${units[index]}`}

export default function SetupPage({onMessage,onContinue}:{onMessage:(message:string)=>void;onContinue:()=>void}){
  const [status,setStatus]=useState<SetupStatus|null>(null);const [loading,setLoading]=useState(true);const [busy,setBusy]=useState(false);const [includeModel,setIncludeModel]=useState(false);const [error,setError]=useState('')
  const reload=useCallback(async()=>{try{const data=await getStatus();setStatus(data);setError('')}catch(e){setError(e instanceof Error?e.message:String(e))}finally{setLoading(false)}},[])
  useEffect(()=>{void reload();const timer=window.setInterval(()=>void reload(),1800);return()=>clearInterval(timer)},[reload])
  const running=status?.install_state==='running'||status?.install_state==='cancelling'
  const model=status?.components.find(item=>item.id==='model')
  async function install(){
    if(!status?.disk_sufficient_for_bootstrap){setError(`Setup needs at least ${formatBytes(status?.minimum_non_model_free_bytes??0)} free for app dependencies; free disk space and retry.`);return}
    let confirmed=false
    if(includeModel&&model?.status!=='installed'){
      confirmed=window.confirm('This explicitly authorizes downloading missing dgrauet/ltx-2.3-mlx-q8 model weights. The transfer can be many tens of gigabytes. Setup first reads Hugging Face file-size metadata and checks free disk plus 8 GiB headroom. Downloads are resumable. Continue?')
      if(!confirmed)return
    }
    setBusy(true);setError('')
    try{const response=await fetch('/api/setup/install',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({download_model:confirmed,confirm_model_download:confirmed})});const data=await response.json();if(!response.ok)throw new Error(data.detail??'Could not start setup');onMessage(data.message??'Setup started');await reload()}
    catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy(false)}
  }
  async function stop(){setBusy(true);setError('');try{const response=await fetch('/api/setup/cancel',{method:'POST'});const data=await response.json();if(!response.ok)throw new Error(data.detail??'Could not stop setup');onMessage('Setup stop requested. Partial downloads are retained for resume.');await reload()}catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy(false)}}
  const dismiss=()=>{try{localStorage.setItem('ai-video-studio-setup-dismissed','1')}catch{};onContinue()}
  return <section className="panel setup-page">
    <div className="panel-heading"><div><div className="section-kicker">FIRST-RUN · LOCAL MACHINE</div><h2>Set up AI Video Studio</h2><p>Verify and install local dependencies. Nothing runs in the cloud; ordinary startup never downloads model weights.</p></div><PackageCheck size={20}/></div>
    {error&&<div className="model-caution"><AlertTriangle size={14}/><span>{error}</span></div>}
    <div className="setup-safety"><ShieldCheck size={16}/><span>Bootstrap is resumable. Existing database, references, generated videos, model files, and a manually edited .env are preserved. No LTX inference or Remotion render runs during setup.</span></div>
    <div className={`setup-disk-check ${status?.disk_sufficient_for_bootstrap?'pass':'fail'}`}><b>{status?.disk_sufficient_for_bootstrap?'Disk check passed':'Disk space check needed'}</b><span>{status?`App volume: ${formatBytes(status.disk_free_bytes)} free · ${formatBytes(status.minimum_non_model_free_bytes)} recommended for dependencies · model cache volume: ${formatBytes(status.model_disk_free_bytes)} free`:'Checking available disk space…'}</span></div>
    <div className="setup-component-list">{(status?.components??[]).map(item=><article className="setup-component" key={item.id}><div className={`setup-status-mark ${item.status}`}>{item.status==='installed'?<Check size={13}/>:item.status==='downloading'?<LoaderCircle className="spin" size={13}/>:<AlertTriangle size={13}/>}</div><div className="setup-component-copy"><b>{item.title}</b><small>{item.detail}</small>{item.path&&<code title={item.path}>{item.path}</code>}</div><span className={`setup-status-label ${item.status}`}>{item.status}</span></article>)}</div>
    {model?.status!=='installed'&&<div className="setup-model-consent"><label><input type="checkbox" checked={includeModel} disabled={running||busy} onChange={event=>setIncludeModel(event.target.checked)}/> Include the optional LTX Q8 model download in this setup</label><p>Before transferring weights, setup queries the repository’s file sizes, checks free disk space for remaining files plus 8 GiB headroom, then uses the Hugging Face resumable cache. If access is gated, accept the model terms and run <code>hf auth login</code> before retrying. No token is collected by this app.</p></div>}
    {running&&<div className="setup-progress"><div className="setup-progress-heading"><b>{status?.installing??'Installing components'}</b>{status?.download_progress_percent!=null&&<span>{Math.round(status.download_progress_percent)}%</span>}</div>{status?.download_progress_percent!=null&&<progress max="100" value={status.download_progress_percent}/>}<div className="setup-log" aria-live="polite">{status?.logs.slice(-40).map((line,index)=><div key={`${index}-${line}`}>{line}</div>)}</div></div>}
    {!running&&Boolean(status?.logs.length)&&<details className="setup-log-details"><summary>Latest setup output{status?.install_state==='failed'?' · needs attention':''}</summary><div className="setup-log">{status?.logs.slice(-60).map((line,index)=><div key={`${index}-${line}`}>{line}</div>)}</div></details>}
    <div className="setup-actions">{running?<button className="button secondary" disabled={busy} onClick={()=>void stop()}><Square size={13}/> Stop setup · retain partial cache</button>:<button className="button primary" disabled={loading||busy||!status||status.ready||!status.disk_sufficient_for_bootstrap} onClick={()=>void install()}>{busy?<LoaderCircle className="spin" size={14}/>:<Download size={14}/>} Install missing components{includeModel&&model?.status!=='installed'?' + model':''}</button>}
      <button className="button secondary" disabled={loading||running} onClick={()=>void reload()}><RefreshCw size={13}/> Recheck</button>
      <button className="button secondary" onClick={dismiss}>{status?.ready?'Continue to studio':'Continue without setup'}</button></div>
    {status?.install_state==='failed'&&<p className="settings-intro">The log shows the failure. Fix the listed prerequisite and rerun Install missing components; existing cache files let supported downloads resume.</p>}
    {status?.ready&&<div className="setup-complete"><Check size={15}/><span>All required local components are detected. You can continue to the studio.</span></div>}
  </section>
}
