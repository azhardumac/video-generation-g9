import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowDown, ArrowUp, Check, Clapperboard, Film, LoaderCircle, Plus, RefreshCw, Save, Trash2, WandSparkles } from 'lucide-react'

type Asset = { path: string; relative_path: string; mime_type: string }
type Preset = { name: string; frames: number; fps: number; duration: number; status: string; visual_drift_warning?: boolean }
type ShotJob = { id: string; status: string; progress: number; error?: string | null; result_path?: string | null; verification_metadata?: { verified_output?: {width:number;height:number;fps:number;frames:number} } | null; events?: {id:number;time:string;level:string;message:string}[] }
type Shot = { id?: string; position: number; scene_id?: string; scene_title: string; title: string; prompt: string; reference_image: string | null; preset: string; seed: number | null; caption: string; status?: string; job?: ShotJob | null }
type ManifestShot = { shot_id: string; scene_title: string; title: string; frames: number; start_frame: number; transition_in_frames: number; transition_out_frames: number; caption: string }
type Manifest = { title: string; revision: number; width: number; height: number; fps: number; duration_in_frames: number; shots: ManifestShot[] }
type Storyboard = { id: string; project_id: string; title: string; revision: number; shots: Shot[]; manifest_path?: string | null; manifest?: Manifest | null }

const emptyShot = (sceneTitle='New scene'): Shot => ({position:0,scene_title:sceneTitle,title:'New shot',prompt:'',reference_image:null,preset:'Short',seed:null,caption:''})

export default function StoryboardPage({assets,imported,onImportProject,onMessage}:{assets:Asset[];imported:boolean;onImportProject:()=>Promise<void>;onMessage:(message:string)=>void}) {
  const [story,setStory]=useState<Storyboard|null>(null)
  const [draft,setDraft]=useState<Shot[]>([])
  const [title,setTitle]=useState('Scorpion & Frog')
  const [presets,setPresets]=useState<Preset[]>([])
  const [busy,setBusy]=useState('')
  const [dirty,setDirty]=useState(false)
  const [error,setError]=useState('')
  const [manifest,setManifest]=useState<Manifest|null>(null)
  const [timelineIndex,setTimelineIndex]=useState(0)
  const [timelinePlaying,setTimelinePlaying]=useState(false)
  const timelineVideo=useRef<HTMLVideoElement|null>(null)

  const loadStory=useCallback(async(id:string,loadDraft=false)=>{
    const response=await fetch(`/api/storyboards/${id}`)
    const data=await response.json()
    if(!response.ok)throw new Error(data.detail??'Could not load storyboard')
    setStory(data)
    if(loadDraft){setTitle(data.title);setDraft(data.shots.map((shot:Shot)=>({...shot})));setDirty(false);setManifest(data.manifest??null)}
    else setManifest(data.manifest??null)
    return data as Storyboard
  },[])

  useEffect(()=>{
    let active=true
    Promise.all([fetch('/api/storyboards'),fetch('/api/presets')]).then(async([s,p])=>{
      if(!active)return
      if(p.ok)setPresets(await p.json())
      if(s.ok){const list:Storyboard[]=await s.json();if(list[0])await loadStory(list[0].id,true)}
    }).catch(e=>setError(e instanceof Error?e.message:'Could not load local storyboards'))
    return()=>{active=false}
  },[loadStory])

  useEffect(()=>{
    if(!story)return
    const timer=window.setInterval(()=>{void loadStory(story.id,false).catch(()=>{})},2000)
    return()=>clearInterval(timer)
  },[story?.id,loadStory])

  useEffect(()=>{
    if(timelinePlaying&&timelineVideo.current)void timelineVideo.current.play().catch(()=>setTimelinePlaying(false))
  },[timelineIndex,timelinePlaying])

  async function importScorpionFrog(){
    setBusy('import');setError('')
    try{
      if(!imported)await onImportProject()
      const response=await fetch('/api/storyboards/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:'scorpion-frog'})})
      const data=await response.json()
      if(!response.ok)throw new Error(data.detail??'Could not create the storyboard')
      setStory(data);setTitle(data.title);setDraft(data.shots.map((shot:Shot)=>({...shot})));setDirty(false);setManifest(data.manifest??null)
      onMessage('Scorpion & Frog storyboard imported; local reference assets remain in place')
    }catch(e){setError(e instanceof Error?e.message:'Import failed')}
    finally{setBusy('')}
  }

  function changeShot(index:number,key:keyof Shot,value:string|number|null){
    setDraft(current=>current.map((shot,i)=>i===index?{...shot,[key]:value}:shot));setDirty(true);setManifest(null)
  }
  function reorder(index:number,direction:-1|1){
    const target=index+direction;if(target<0||target>=draft.length)return
    setDraft(current=>{const next=[...current];[next[index],next[target]]=[next[target],next[index]];return next.map((shot,i)=>({...shot,position:i}))})
    setDirty(true);setManifest(null)
  }
  function addShot(){setDraft(current=>[...current,{...emptyShot(current.at(-1)?.scene_title??'New scene'),position:current.length}]);setDirty(true);setManifest(null)}
  function removeShot(index:number){
    if(draft.length<2)return
    setDraft(current=>current.filter((_,i)=>i!==index).map((shot,position)=>({...shot,position})));setDirty(true);setManifest(null)
  }

  async function saveDraft(){
    if(!story)throw new Error('Import a project storyboard first')
    if(draft.some(shot=>!shot.title.trim()||!shot.prompt.trim()))throw new Error('Every shot needs a title and prompt before saving')
    setBusy('save');setError('')
    try{
      const response=await fetch(`/api/storyboards/${story.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:story.revision,title,shots:draft.map(({status,job,position,...shot})=>({...shot,reference_image:shot.reference_image||null}))})})
      const data=await response.json();if(!response.ok)throw new Error(data.detail??'Could not save storyboard')
      setStory(data);setTitle(data.title);setDraft(data.shots.map((shot:Shot)=>({...shot})));setDirty(false);setManifest(null)
      onMessage(`Storyboard saved as revision ${data.revision}`);return data as Storyboard
    }catch(e){setError(e instanceof Error?e.message:'Could not save storyboard');throw e}
    finally{setBusy('')}
  }

  async function queueShot(shot:Shot){
    if(shot.preset==='Experimental'&&!window.confirm('Experimental 193-frame generation may show visual drift and use substantial resources. Queue this shot?'))return
    setBusy(`queue:${shot.id??''}`);setError('')
    try{
      const saved=dirty?await saveDraft():story
      if(!saved)throw new Error('Storyboard is not loaded')
      const target=saved.shots.find(item=>item.id===shot.id)??saved.shots.find(item=>item.title===shot.title)
      if(!target?.id)throw new Error('Save the shot plan before queuing it')
      const response=await fetch(`/api/storyboards/${saved.id}/shots/${target.id}/generate`,{method:'POST'})
      const data=await response.json();if(!response.ok)throw new Error(data.detail??'Could not queue shot')
      await loadStory(saved.id,false);onMessage(`Shot “${target.title}” queued. LTX starts one job at a time.`)
    }catch(e){setError(e instanceof Error?e.message:'Could not queue shot')}
    finally{setBusy('')}
  }

  async function jobAction(jobId:string,action:'cancel'|'retry'){
    const response=await fetch(`/api/jobs/${jobId}/${action}`,{method:'POST'});const data=await response.json()
    if(!response.ok){setError(data.detail??`Could not ${action} job`);return}
    if(story)await loadStory(story.id,false)
  }

  async function buildManifest(){
    if(!story)return
    setBusy('manifest');setError('')
    try{
      if(dirty)await saveDraft()
      const response=await fetch(`/api/storyboards/${story.id}/manifest`,{method:'POST'})
      const data=await response.json();if(!response.ok)throw new Error(data.detail??'Could not assemble preview')
      setStory(data);setManifest(data.manifest);setTimelineIndex(0);setTimelinePlaying(false);onMessage('Verified clips assembled into a Remotion timeline preview')
    }catch(e){setError(e instanceof Error?e.message:'Could not build composition manifest')}
    finally{setBusy('')}
  }

  async function renderComposition(){
    if(!story?.manifest||dirty)return
    if(!window.confirm(`Queue the ${story.manifest.duration_in_frames}-frame Remotion composition for local rendering? This does not start LTX generation.`))return
    setBusy('render');setError('')
    try{
      const response=await fetch(`/api/storyboards/${story.id}/render`,{method:'POST'})
      const data=await response.json();if(!response.ok)throw new Error(data.detail??'Could not queue Remotion render')
      onMessage(`Remotion composition render queued as job ${data.id}`)
    }catch(e){setError(e instanceof Error?e.message:'Could not queue render')}
    finally{setBusy('')}
  }

  async function archiveStoryboard(){
    if(!story||!window.confirm('Archive this storyboard? Its revision and linked generation history will be retained.'))return
    setBusy('archive');setError('')
    try{
      const response=await fetch(`/api/storyboards/${story.id}`,{method:'DELETE'})
      if(!response.ok){const data=await response.json();throw new Error(data.detail??'Could not archive storyboard')}
      setStory(null);setDraft([]);setManifest(null);setDirty(false);onMessage('Storyboard archived; its history and generated files were retained')
    }catch(e){setError(e instanceof Error?e.message:'Could not archive storyboard')}
    finally{setBusy('')}
  }

  if(!story)return <section className="panel workspace-page storyboard-page"><div className="section-kicker">PHASE 3 · LOCAL STORYBOARD</div><h2>Storyboard & shots</h2><p className="settings-intro">Import your existing Scorpion & Frog project to create a persistent shot plan. References stay in their original folders.</p>{error&&<div className="model-caution">{error}</div>}<button className="button primary" disabled={busy==='import'} onClick={importScorpionFrog}>{busy==='import'?<LoaderCircle className="spin" size={14}/>:<Film size={14}/>} Import Scorpion & Frog</button></section>

  return <section className="panel workspace-page storyboard-page">
    <div className="section-kicker">PERSISTENT STORYBOARD · REVISION {story.revision}</div>
    <div className="storyboard-title-row"><div><h2>Storyboard & shots</h2><p className="settings-intro">Editable shot plans, queued one at a time on the local LTX worker.</p></div><div className="storyboard-actions"><button className="button secondary" disabled={!dirty||busy!==''} onClick={()=>void saveDraft()}><Save size={14}/> Save revision</button><button className="button secondary" onClick={()=>void loadStory(story.id,true)} disabled={busy!==''}><RefreshCw size={13}/> Reload</button><button className="button secondary" disabled={busy!==''} onClick={()=>void archiveStoryboard()}>Archive storyboard</button></div></div>
    <label className="setting-field wide storyboard-name">STORY TITLE<input value={title} maxLength={160} onChange={e=>{setTitle(e.target.value);setDirty(true);setManifest(null)}}/></label>
    {error&&<div className="model-caution storyboard-error">{error}</div>}
    <div className="shot-plan-list">{draft.map((shot,index)=>{
      const live=story.shots.find(item=>item.id===shot.id);const job=live?.job;const verified=job?.verification_metadata?.verified_output
      const presetInfo=presets.find(item=>item.name===shot.preset)
      return <article className="shot-plan-card" key={shot.id??`new-${index}`}>
        <div className="shot-plan-head"><div className="shot-index">{String(index+1).padStart(2,'0')}</div><div className="shot-plan-summary"><b>{shot.title||`Shot ${index+1}`}</b><span>{shot.scene_title} · {presetInfo?`${shot.preset} · ${presetInfo.frames} frames · ${presetInfo.duration.toFixed(2)} s at ${presetInfo.fps} fps`:shot.preset}</span></div><span className={`job-status ${job?.status??'planned'}`}>{job?.status??'planned'}{job?.status==='running'?` · ${job.progress}%`:''}</span><div className="shot-order-buttons"><button aria-label="Move shot up" disabled={index===0} onClick={()=>reorder(index,-1)}><ArrowUp size={14}/></button><button aria-label="Move shot down" disabled={index===draft.length-1} onClick={()=>reorder(index,1)}><ArrowDown size={14}/></button><button aria-label="Remove shot" disabled={draft.length<2} onClick={()=>removeShot(index)}><Trash2 size={14}/></button></div></div>
        <div className="settings-form-grid shot-fields"><label className="setting-field">SCENE<input value={shot.scene_title} maxLength={120} onChange={e=>changeShot(index,'scene_title',e.target.value)}/></label><label className="setting-field">SHOT TITLE<input value={shot.title} maxLength={120} onChange={e=>changeShot(index,'title',e.target.value)}/></label><label className="setting-field">GENERATION PRESET<select value={shot.preset} onChange={e=>changeShot(index,'preset',e.target.value)}>{presets.map(p=><option value={p.name} key={p.name}>{p.name} · {p.frames} frames · {p.fps} fps</option>)}</select></label><label className="setting-field wide">PROMPT<textarea value={shot.prompt} maxLength={4000} onChange={e=>changeShot(index,'prompt',e.target.value)} placeholder="Describe this shot"/></label><label className="setting-field">REFERENCE IMAGE<select value={shot.reference_image??''} onChange={e=>changeShot(index,'reference_image',e.target.value||null)}><option value="">No reference · text to video</option>{assets.filter(asset=>asset.mime_type.startsWith('image/')).map(asset=><option value={asset.path} key={asset.path}>{asset.relative_path}</option>)}</select></label><label className="setting-field">SEED<input type="number" min="0" value={shot.seed??''} placeholder="Random seed" onChange={e=>changeShot(index,'seed',e.target.value===''?null:Number(e.target.value))}/></label><label className="setting-field wide">OPTIONAL CAPTION<input value={shot.caption} maxLength={240} onChange={e=>changeShot(index,'caption',e.target.value)}/></label></div>
        <div className="shot-plan-footer"><span>{verified?`Verified · ${verified.width} × ${verified.height} · ${verified.fps} fps · ${verified.frames} frames`:job?.error??'No generation job queued'}</span><div className="shot-job-actions">{job&&(job.status==='queued'||job.status==='running')&&<button className="button secondary mini" onClick={()=>void jobAction(job.id,'cancel')}>Cancel job</button>}{job?.status==='failed'&&<button className="button secondary mini" onClick={()=>void jobAction(job.id,'retry')}>Retry</button>}<button className="button primary mini" disabled={!shot.id||dirty||busy!==''||job?.status==='queued'||job?.status==='running'} onClick={()=>void queueShot(shot)}><WandSparkles size={12}/> {job?.status==='completed'?'Generate again':'Queue shot'}</button></div></div>
        {job?.result_path&&<div className="shot-video-preview"><video controls preload="metadata" src={`/api/media/file?path=${encodeURIComponent(job.result_path)}`}/></div>}
        {Boolean(job?.events?.length)&&<details className="shot-log"><summary>Latest generation log</summary><div>{job?.events?.slice(-8).map(event=><p key={event.id}><time>{new Date(event.time).toLocaleTimeString()}</time>{event.message}</p>)}</div></details>}
      </article>
    })}</div>
    <div className="storyboard-bottom-actions"><button className="button secondary" onClick={addShot} disabled={draft.length>=40}><Plus size={14}/> Add shot</button><button className="button secondary" onClick={()=>void saveDraft()} disabled={!dirty||busy!==''}><Save size={14}/> Save storyboard</button><button className="button primary" onClick={()=>void buildManifest()} disabled={busy!==''||dirty||draft.length===0}><Clapperboard size={14}/> Preview verified timeline</button></div>
    {manifest&&<section className="storyboard-timeline"><div className="timeline-title"><div><div className="section-kicker">REMOTION ASSEMBLY PREVIEW · REVISION {manifest.revision}</div><h3>{manifest.title}</h3></div><div className="timeline-spec">{manifest.width} × {manifest.height} · {manifest.fps} fps · {(manifest.duration_in_frames/manifest.fps).toFixed(2)} s</div></div>{(()=>{const clip=manifest.shots[timelineIndex];const source=clip&&story.shots.find(s=>s.id===clip.shot_id)?.job?.result_path;return clip&&source?<div className="assembled-player"><div className="assembled-preview-frame"><video ref={timelineVideo} key={clip.shot_id} controls preload="metadata" src={`/api/media/file?path=${encodeURIComponent(source)}`} onPlay={()=>setTimelinePlaying(true)} onPause={()=>setTimelinePlaying(false)} onEnded={()=>{if(timelineIndex+1<manifest.shots.length){setTimelineIndex(timelineIndex+1);setTimelinePlaying(true)}else setTimelinePlaying(false)}}/>{clip.caption&&<div className="assembled-caption">{clip.caption}</div>}</div><div className="assembled-player-actions"><button className="button secondary mini" onClick={()=>{if(timelinePlaying){timelineVideo.current?.pause();setTimelinePlaying(false)}else{setTimelinePlaying(true);void timelineVideo.current?.play()}}}>{timelinePlaying?'Pause timeline':'Play timeline'}</button><span>Shot {timelineIndex+1} of {manifest.shots.length} · {clip.scene_title} · {clip.title}</span></div></div>:null})()}<div className="timeline-track">{manifest.shots.map((clip,index)=>{const source=story.shots.find(s=>s.id===clip.shot_id)?.job?.result_path;return <div className={`timeline-clip ${index===timelineIndex?'current':''}`} key={clip.shot_id}><button className="timeline-clip-label" onClick={()=>{setTimelineIndex(index);setTimelinePlaying(false)}}>{clip.scene_title} · {String(index+1).padStart(2,'0')} · {clip.title}</button><small>{clip.frames} frames{clip.transition_in_frames?` · ${clip.transition_in_frames}f crossfade`:''}</small>{clip.caption&&<small>Caption: {clip.caption}</small>}{!source&&<small>Video unavailable</small>}</div>})}</div><p className="settings-intro">Preview plays verified clips in order; transitions and captions are applied by the packaged Remotion composition. Rendering remains a separate confirmed action.</p><button className="button primary" onClick={()=>void renderComposition()} disabled={busy!==''||dirty}><Check size={14}/> Confirm & queue Remotion render</button></section>}
  </section>
}
