import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowDown, ArrowUp, Check, Clapperboard, Film, LoaderCircle, Plus, RefreshCw, Save, Trash2, WandSparkles } from 'lucide-react'

type Asset = { path: string; relative_path: string; mime_type: string }
type Preset = { name: string; frames: number; fps: number; duration: number; status: string; visual_drift_warning?: boolean }
type ExistingJob = { id:string; kind?:string; status:string; created_at:string; result_path?:string|null; payload:{prompt?:string;preset?:string;seed?:number|null;reference_image?:string|null}; verification_metadata?:{verified_output?:{width:number;height:number;fps:number;frames:number}}|null }
type ShotJob = ExistingJob & { progress: number; error?: string | null; settings_match?:boolean; settings_snapshot?:{prompt:string;preset:string;seed?:number|null;reference_image?:string|null}; events?: {id:number;time:string;level:string;message:string}[] }
type Shot = { id?: string; position: number; scene_id?: string; scene_title: string; title: string; prompt: string; reference_image: string | null; preset: string; seed: number | null; caption: string; width:number|null;height:number|null;fps:number|null;output_location:string|null;framing:string;prompt_reviewed:boolean;reviewed_style_revision:number;style_context?:string;style_bible_revision?:number;status?: string; job?: ShotJob | null }
type ManifestShot = { shot_id: string; scene_title: string; title: string; frames: number; start_frame: number; transition_in_frames: number; transition_out_frames: number; caption: string }
type Manifest = { title: string; revision: number; width: number; height: number; fps: number; duration_in_frames: number; shots: ManifestShot[] }
type Storyboard = { id: string; project_id: string; title: string; revision: number; story_text:string; style_bible:Record<string,string>;style_bible_revision:number;shots: Shot[]; manifest_path?: string | null; manifest?: Manifest | null }
type ProductionProject={id:string;root:string;workspace_kind:string;aspect_ratio:string|null;frame_rate:number|null}
type ContinuityState={revision:number;approved_revision:number|null;document:{scenes:{title:string;anchor_reference_ids:string[]}[]}|null}

const dimensions=(ratio:string|null):[number,number]=>({'16:9':[768,432],'1:1':[640,640],'4:3':[640,480],'3:2':[672,448]} as Record<string,[number,number]>)[ratio??'']??[448,768]
const emptyShot = (project:ProductionProject|null): Shot => {const [width,height]=dimensions(project?.aspect_ratio??null);return {position:0,scene_title:'',title:'',prompt:'',reference_image:null,preset:'Short',seed:null,caption:'',width,height,fps:project?.frame_rate??24,output_location:'',framing:'',prompt_reviewed:false,reviewed_style_revision:0}}

export default function StoryboardPage({projectId,project,assets,jobs,ltxReady,onMessage,onDirtyChange}:{projectId:string|null;project:ProductionProject|null;assets:Asset[];jobs:ExistingJob[];ltxReady:boolean;onMessage:(message:string)=>void;onDirtyChange:(dirty:boolean)=>void}) {
  const [story,setStory]=useState<Storyboard|null>(null)
  const [draft,setDraft]=useState<Shot[]>([])
  const [title,setTitle]=useState('')
  const [newTitle,setNewTitle]=useState('')
  const [newStoryText,setNewStoryText]=useState('')
  const [storyText,setStoryText]=useState('')
  const [presets,setPresets]=useState<Preset[]>([])
  const [busy,setBusy]=useState('')
  const [dirty,setDirty]=useState(false)
  const [error,setError]=useState('')
  const [manifest,setManifest]=useState<Manifest|null>(null)
  const [timelineIndex,setTimelineIndex]=useState(0)
  const [timelinePlaying,setTimelinePlaying]=useState(false)
  const [selectedExisting,setSelectedExisting]=useState<Record<string,string>>({})
  const [continuity,setContinuity]=useState<ContinuityState|null>(null)
  const [experimentalAnchors,setExperimentalAnchors]=useState<Record<string,boolean>>({})
  const timelineVideo=useRef<HTMLVideoElement|null>(null)

  useEffect(()=>{onDirtyChange(dirty);return()=>onDirtyChange(false)},[dirty,onDirtyChange])

  const loadStory=useCallback(async(id:string,loadDraft=false)=>{
    const response=await fetch(`/api/storyboards/${id}`)
    const data=await response.json()
    if(!response.ok)throw new Error(data.detail??'Could not load storyboard')
    setStory(data)
    if(loadDraft){setTitle(data.title);setStoryText(data.story_text??'');setDraft(data.shots.map((shot:Shot)=>({...shot})));setDirty(false);setManifest(data.manifest??null)}
    else setManifest(data.manifest??null)
    return data as Storyboard
  },[])

  useEffect(()=>{
    let active=true
    if(!projectId){setStory(null);setDraft([]);setTitle('');setStoryText('');return}
    setStory(null);setDraft([]);setTitle('');setStoryText('');setManifest(null);setDirty(false)
    Promise.all([fetch(`/api/projects/${encodeURIComponent(projectId)}/storyboards`),fetch('/api/presets'),fetch(`/api/projects/${encodeURIComponent(projectId)}/scene-continuity`)]).then(async([s,p,c])=>{
      if(!active)return
      if(p.ok)setPresets(await p.json())
      if(c.ok)setContinuity(await c.json())
      if(s.ok){const list:Storyboard[]=await s.json();if(list[0])await loadStory(list[0].id,true)}
    }).catch(e=>setError(e instanceof Error?e.message:'Could not load local storyboards'))
    return()=>{active=false}
  },[loadStory,projectId])

  useEffect(()=>{
    if(!story)return
    const timer=window.setInterval(()=>{void loadStory(story.id,false).catch(()=>{})},2000)
    return()=>clearInterval(timer)
  },[story?.id,loadStory])

  useEffect(()=>{
    if(!dirty)return
    const guard=(event:BeforeUnloadEvent)=>{event.preventDefault();event.returnValue=''}
    window.addEventListener('beforeunload',guard)
    return()=>window.removeEventListener('beforeunload',guard)
  },[dirty])

  useEffect(()=>{
    if(timelinePlaying&&timelineVideo.current)void timelineVideo.current.play().catch(()=>setTimelinePlaying(false))
  },[timelineIndex,timelinePlaying])

  async function createBlankStoryboard(){
    if(!projectId)return
    setBusy('create');setError('')
    try{
      const response=await fetch(`/api/projects/${encodeURIComponent(projectId)}/storyboards`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:newTitle,story_text:newStoryText})})
      const data=await response.json()
      if(!response.ok)throw new Error(data.detail??'Could not create the blank storyboard')
      setStory(data);setTitle(data.title);setStoryText(data.story_text??'');setDraft(data.shots.map((shot:Shot)=>({...shot})));setDirty(false);setManifest(data.manifest??null)
      setNewTitle('');setNewStoryText('');onMessage('Storyboard created with no generated scenes or shots')
    }catch(e){setError(e instanceof Error?e.message:'Could not create storyboard')}
    finally{setBusy('')}
  }

  function changeShot(index:number,key:keyof Shot,value:string|number|boolean|null){
    setDraft(current=>current.map((shot,i)=>i===index?{...shot,[key]:value,...(key==='prompt'?{prompt_reviewed:false}:{})}:shot));setDirty(true);setManifest(null)
  }
  function reorder(index:number,direction:-1|1){
    const target=index+direction;if(target<0||target>=draft.length)return
    setDraft(current=>{const next=[...current];[next[index],next[target]]=[next[target],next[index]];return next.map((shot,i)=>({...shot,position:i}))})
    setDirty(true);setManifest(null)
  }
  function addShot(){setDraft(current=>[...current,{...emptyShot(project),position:current.length}]);setDirty(true);setManifest(null)}

  function reviewShot(index:number,approved:boolean){setDraft(current=>current.map((shot,i)=>i===index?{...shot,prompt_reviewed:approved,reviewed_style_revision:story?.style_bible_revision??0}:shot));setDirty(true)}
  function removeShot(index:number){
    if(draft.length<2)return
    setDraft(current=>current.filter((_,i)=>i!==index).map((shot,position)=>({...shot,position})));setDirty(true);setManifest(null)
  }

  async function saveDraft(){
    if(!story)throw new Error('Create a storyboard first')
    if(draft.some(shot=>!shot.scene_title.trim()||!shot.title.trim()||!shot.prompt.trim()))throw new Error('Every shot needs a scene, title, and prompt before saving')
    setBusy('save');setError('')
    try{
      const response=await fetch(`/api/storyboards/${story.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:story.revision,title,story_text:storyText,shots:draft.map(({status,job,position,style_context,style_bible_revision,...shot})=>({...shot,reference_image:shot.reference_image||null,prompt_reviewed:shot.prompt_reviewed&&shot.reviewed_style_revision===(story.style_bible_revision??0)}))})})
      const data=await response.json();if(!response.ok)throw new Error(data.detail??'Could not save storyboard')
      setStory(data);setTitle(data.title);setStoryText(data.story_text??'');setDraft(data.shots.map((shot:Shot)=>({...shot})));setDirty(false);setManifest(null)
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
      const useAnchors=Boolean(experimentalAnchors[target.id])
      if(!window.confirm(`Queue only “${target.title}” (${target.preset}) for local LTX generation? This starts one GPU-intensive job, not a film batch.${useAnchors?' Experimental temporal image anchors are enabled and do not lock character identity.':''}`))return
      const response=await fetch(`/api/storyboards/${saved.id}/shots/${target.id}/generate`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(useAnchors?{continuity_revision:continuity?.revision,use_experimental_multi_anchor:true}:{})})
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

  async function attachExisting(shot:Shot){
    if(!story||!shot.id)return
    const jobId=selectedExisting[shot.id]
    if(!jobId)return
    setBusy(`attach:${shot.id}`);setError('')
    try{
      const response=await fetch(`/api/storyboards/${story.id}/shots/${shot.id}/link-existing/${jobId}`,{method:'POST'})
      const data=await response.json();if(!response.ok)throw new Error(data.detail??'Could not attach existing verified clip')
      setStory(data);setStoryText(data.story_text??'');setDraft(data.shots.map((item:Shot)=>({...item})));setDirty(false);setManifest(null)
      onMessage('Existing verified clip attached; no generation was started')
    }catch(e){setError(e instanceof Error?e.message:'Could not attach existing clip')}
    finally{setBusy('')}
  }

  function useExistingSettings(index:number,job:ExistingJob){
    changeShot(index,'prompt',job.payload.prompt??'')
    changeShot(index,'reference_image',job.payload.reference_image??null)
    changeShot(index,'seed',job.payload.seed??null)
    changeShot(index,'preset',job.payload.preset??'Short')
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
    if(!story||!window.confirm(`${dirty?'Discard unsaved changes and archive':'Archive'} this storyboard? Its revision, linked generation history, and media files will be retained.`))return
    setBusy('archive');setError('')
    try{
      const response=await fetch(`/api/storyboards/${story.id}`,{method:'DELETE'})
      if(!response.ok){const data=await response.json();throw new Error(data.detail??'Could not archive storyboard')}
      setStory(null);setDraft([]);setStoryText('');setManifest(null);setDirty(false);onMessage('Storyboard archived; its history and generated files were retained')
    }catch(e){setError(e instanceof Error?e.message:'Could not archive storyboard')}
    finally{setBusy('')}
  }

  if(!story)return <section className="panel workspace-page storyboard-page"><div className="section-kicker">PROJECT STORYBOARD</div><h2>Storyboard & shots</h2>{!projectId?<div className="empty-workspace"><Film size={18}/><b>Select a project first</b><span>Storyboards are saved within a project.</span></div>:<><p className="settings-intro">Create a blank persistent storyboard or add story text you wrote yourself. No scenes, shots, prompts, or references are generated automatically.</p>{error&&<div className="model-caution">{error}</div>}<label className="setting-field">Storyboard title<input value={newTitle} maxLength={160} onChange={e=>setNewTitle(e.target.value)} placeholder="Enter a title"/></label><label className="setting-field wide">Story text<textarea value={newStoryText} maxLength={20000} onChange={e=>setNewStoryText(e.target.value)} placeholder="Optional story written by you"/></label><button className="button primary" disabled={busy==='create'||!newTitle.trim()} onClick={()=>void createBlankStoryboard()}>{busy==='create'?<LoaderCircle className="spin" size={14}/>:<Plus size={14}/>} Create storyboard</button></>}</section>

  return <section className="panel workspace-page storyboard-page">
    <div className="section-kicker">PERSISTENT STORYBOARD · REVISION {story.revision} · STYLE BIBLE REVISION {story.style_bible_revision}</div>
    <div className="storyboard-title-row"><div><h2>Storyboard & shots</h2><p className="settings-intro">Editable shot plans, queued one at a time on the local LTX worker.</p></div><div className="storyboard-actions"><button className="button secondary" disabled={!dirty||busy!==''} onClick={()=>void saveDraft()}><Save size={14}/> Save revision</button><button className="button secondary" onClick={()=>{if(!dirty||window.confirm('Discard unsaved storyboard changes and reload the saved revision?'))void loadStory(story.id,true)}} disabled={busy!==''}><RefreshCw size={13}/> Reload</button><button className="button secondary" disabled={busy!==''} onClick={()=>void archiveStoryboard()}>Archive storyboard</button></div></div>
    <label className="setting-field wide storyboard-name">STORY TITLE<input value={title} maxLength={160} onChange={e=>{setTitle(e.target.value);setDirty(true);setManifest(null)}}/></label>
    <label className="setting-field wide">USER-WRITTEN STORY TEXT<textarea value={storyText} maxLength={20000} onChange={e=>{setStoryText(e.target.value);setDirty(true);setManifest(null)}} placeholder="Optional story text written for this storyboard"/></label>
    {error&&<div className="model-caution storyboard-error">{error}</div>}
    <div className="shot-plan-list">{draft.map((shot,index)=>{
      const live=story.shots.find(item=>item.id===shot.id);const job=live?.job;const verified=job?.verification_metadata?.verified_output
      const presetInfo=presets.find(item=>item.name===shot.preset)
      const savedJobSettings=job?.settings_snapshot
      const outputIsStale=Boolean(job?.result_path&&(shot.status==='stale'||job.settings_match===false||(savedJobSettings&&(savedJobSettings.prompt!==shot.prompt||savedJobSettings.reference_image!==(shot.reference_image||null)||savedJobSettings.seed!==(shot.seed??null)||savedJobSettings.preset!==shot.preset))))
      const reusable=jobs.filter(item=>item.kind==='generation'&&item.status==='completed'&&typeof item.payload.prompt==='string'&&Boolean(item.result_path)&&Boolean(item.verification_metadata?.verified_output))
      const selectedJob=reusable.find(item=>item.id===selectedExisting[shot.id??''])
      const matchesSelected=selectedJob&&selectedJob.payload.prompt===shot.prompt&&selectedJob.payload.reference_image===shot.reference_image&&selectedJob.payload.seed===(shot.seed??null)&&selectedJob.payload.preset===shot.preset
      return <article className="shot-plan-card" key={shot.id??`new-${index}`}>
        <div className="shot-plan-head"><div className="shot-index">{String(index+1).padStart(2,'0')}</div><div className="shot-plan-summary"><b>{shot.title||`Shot ${index+1}`}</b><span>{shot.scene_title||'Scene not named'} · {presetInfo?`${shot.preset} · ${presetInfo.frames} frames · ${(presetInfo.frames/(shot.fps??presetInfo.fps)).toFixed(2)} s at ${shot.fps??presetInfo.fps} fps`:shot.preset}</span></div><span className={`job-status ${shot.status??'missing'}`}>{shot.status??'missing'}{job?.status==='running'?` · ${job.progress}%`:''}</span><div className="shot-order-buttons"><button aria-label="Move shot up" disabled={index===0} onClick={()=>reorder(index,-1)}><ArrowUp size={14}/></button><button aria-label="Move shot down" disabled={index===draft.length-1} onClick={()=>reorder(index,1)}><ArrowDown size={14}/></button><button aria-label="Remove shot" disabled={draft.length<2} onClick={()=>removeShot(index)}><Trash2 size={14}/></button></div></div>
        <div className="settings-form-grid shot-fields"><label className="setting-field">SCENE<input value={shot.scene_title} maxLength={120} onChange={e=>changeShot(index,'scene_title',e.target.value)}/></label><label className="setting-field">SHOT TITLE<input value={shot.title} maxLength={120} onChange={e=>changeShot(index,'title',e.target.value)}/></label><label className="setting-field">GENERATION PRESET<select value={shot.preset} onChange={e=>changeShot(index,'preset',e.target.value)}>{presets.map(p=><option value={p.name} key={p.name}>{p.name} · {p.frames} frames · {p.duration.toFixed(2)}s</option>)}</select></label><label className="setting-field wide">SHOT PROMPT<textarea value={shot.prompt} maxLength={4000} onChange={e=>changeShot(index,'prompt',e.target.value)} placeholder="Write the prompt for this shot"/></label><label className="setting-field">REFERENCE IMAGE<select value={shot.reference_image??''} onChange={e=>changeShot(index,'reference_image',e.target.value||null)}><option value="">No reference · text to video</option>{assets.filter(asset=>asset.mime_type.startsWith('image/')).map(asset=><option value={asset.path} key={asset.path}>{asset.relative_path}</option>)}</select></label><label className="setting-field">SEED<input type="number" min="0" value={shot.seed??''} placeholder="Random seed" onChange={e=>changeShot(index,'seed',e.target.value===''?null:Number(e.target.value))}/></label><label className="setting-field">WIDTH<input type="number" step="8" min="64" max="2048" value={shot.width??''} onChange={e=>changeShot(index,'width',Number(e.target.value))}/></label><label className="setting-field">HEIGHT<input type="number" step="8" min="64" max="2048" value={shot.height??''} onChange={e=>changeShot(index,'height',Number(e.target.value))}/></label><label className="setting-field">FPS<input type="number" min="1" max="60" value={shot.fps??''} onChange={e=>changeShot(index,'fps',Number(e.target.value))}/></label><label className="setting-field">INTENDED FRAMING<input value={shot.framing} maxLength={120} onChange={e=>changeShot(index,'framing',e.target.value)} placeholder="User-defined shot framing"/></label><label className="setting-field wide">OUTPUT DIRECTORY<input value={shot.output_location??''} onChange={e=>changeShot(index,'output_location',e.target.value)} placeholder="Project output directory"/></label><label className="setting-field wide">OPTIONAL CAPTION<input value={shot.caption} maxLength={240} onChange={e=>changeShot(index,'caption',e.target.value)}/></label></div>
        <section className="prompt-review"><div className="section-kicker">EXACT PROMPT SENT TO LTX</div><pre>{shot.style_context?`${shot.style_context}\n\n${shot.prompt}`:shot.prompt||'Write a shot prompt before review.'}</pre><label><input type="checkbox" checked={Boolean(shot.prompt_reviewed&&shot.reviewed_style_revision===(story.style_bible_revision??0))} disabled={!shot.prompt.trim()} onChange={e=>reviewShot(index,e.target.checked)}/> I reviewed this exact prompt and the current style bible context</label>{shot.reviewed_style_revision!==(story.style_bible_revision??0)&&shot.prompt_reviewed&&<div className="model-caution">The style bible changed after review. Review this prompt again.</div>}<details><summary>Input settings and readiness</summary><div className="settings-intro">{shot.width} × {shot.height} · {shot.fps} fps · {presetInfo?.frames??'—'} frames · {presetInfo?(presetInfo.frames/(shot.fps??presetInfo.fps)).toFixed(2):'—'} s · {shot.reference_image?'one selected image reference':'text-to-video, no image reference'} · output {shot.output_location||'project default'} · {shot.framing||'framing unspecified'}</div>{!ltxReady&&<div className="model-caution">Local LTX or cached weights are unavailable. Recheck Settings; no model download will be attempted.</div>}{shot.reference_image&&!assets.some(asset=>asset.path===shot.reference_image)&&<div className="model-caution">Selected reference is not available in this project. Relink it in References.</div>}<div className="model-caution">Independent image-to-video uses one selected image. Explicitly approved temporal scene anchors add timeline guides only; they do not lock character identity.</div></details></section>
        <div className="existing-clip-tools"><label className="setting-field">REUSE VERIFIED GENERATION<select value={selectedExisting[shot.id??'']??''} onChange={e=>setSelectedExisting(current=>({...current,[shot.id??'']:e.target.value}))}><option value="">Select a completed verified clip</option>{reusable.map(item=><option value={item.id} key={item.id}>{new Date(item.created_at).toLocaleString()} · {item.payload.preset} · {item.verification_metadata?.verified_output?.frames} frames · {item.id.slice(0,8)}</option>)}</select></label><button className="button secondary mini" disabled={!selectedJob||busy!==''} onClick={()=>selectedJob&&useExistingSettings(index,selectedJob)}>Use selected job settings</button><button className="button secondary mini" disabled={!shot.id||!matchesSelected||dirty||busy!==''} onClick={()=>void attachExisting(shot)}>Attach existing clip</button><span>Attaching adds a storyboard link only; it does not copy or modify the video.</span></div>
        <div className="shot-plan-footer"><span>{outputIsStale?'STALE OUTPUT · retained as history; settings differ':shot.status==='verified'&&verified?`VERIFIED · ${verified.width} × ${verified.height} · ${verified.fps} fps · ${verified.frames} frames`:job?.error??(shot.status==='missing'?'Missing output · no shot job has completed':'No verified output')}</span><div className="shot-job-actions">{job&&(job.status==='queued'||job.status==='running')&&<button className="button secondary mini" onClick={()=>void jobAction(job.id,'cancel')}>Cancel job</button>}{job?.status==='failed'&&<button className="button secondary mini" onClick={()=>void jobAction(job.id,'retry')}>Retry</button>}<button className="button primary mini" disabled={!shot.id||shot.status==='verified'||dirty||busy!==''||!ltxReady||!shot.prompt_reviewed||shot.reviewed_style_revision!==(story.style_bible_revision??0)||job?.status==='queued'||job?.status==='running'} onClick={()=>void queueShot(shot)}><WandSparkles size={12}/>{shot.status==='verified'?' Verified clip preserved':job?.status==='completed'?' Generate stale shot':' Queue shot'}</button></div></div>
        {job?.result_path&&<div className="shot-video-preview"><video controls preload="metadata" src={`/api/media/file?path=${encodeURIComponent(job.result_path)}`}/></div>}
        {Boolean(job?.events?.length)&&<details className="shot-log"><summary>Latest generation log</summary><div>{job?.events?.slice(-8).map(event=><p key={event.id}><time>{new Date(event.time).toLocaleTimeString()}</time>{event.message}</p>)}</div></details>}
      </article>
    })}</div>
    <section className="single-scene-validation-list"><div><div className="section-kicker">CONTROLLED SINGLE-SCENE VALIDATION</div><p className="settings-intro">Use the Queue shot button on exactly one reviewed shot. This queues one job only; it does not create or resume a film batch.</p></div>{draft.filter(shot=>shot.id).map(shot=>{const scene=continuity?.document?.scenes.find(item=>item.title.toLocaleLowerCase()===shot.scene_title.toLocaleLowerCase());const ready=Boolean(scene?.anchor_reference_ids.length&&continuity?.revision===continuity?.approved_revision);return <label className="film-approval-check" key={shot.id}><input type="checkbox" checked={Boolean(experimentalAnchors[shot.id!])} disabled={!ready||shot.status==='verified'||dirty} onChange={event=>setExperimentalAnchors(current=>({...current,[shot.id!]:event.target.checked}))}/>{shot.scene_title||'Unnamed scene'} / {shot.title}: experimental temporal image anchors {ready?`(${scene?.anchor_reference_ids.length} approved guides)`:'(requires approved scene anchors)'}<small>Optional. Multiple --image inputs guide timeline frames, not simultaneous character identities or a persistent identity lock.</small></label>})}</section>
    <div className="storyboard-bottom-actions"><button className="button secondary" onClick={addShot} disabled={draft.length>=40}><Plus size={14}/> Add shot</button><button className="button secondary" onClick={()=>void saveDraft()} disabled={!dirty||busy!==''}><Save size={14}/> Save storyboard</button><button className="button primary" onClick={()=>void buildManifest()} disabled={busy!==''||dirty||draft.length===0}><Clapperboard size={14}/> Preview verified timeline</button></div>
    {manifest&&<section className="storyboard-timeline"><div className="timeline-title"><div><div className="section-kicker">REMOTION ASSEMBLY PREVIEW · REVISION {manifest.revision}</div><h3>{manifest.title}</h3></div><div className="timeline-spec">{manifest.width} × {manifest.height} · {manifest.fps} fps · {(manifest.duration_in_frames/manifest.fps).toFixed(2)} s</div></div>{(()=>{const clip=manifest.shots[timelineIndex];const source=clip&&story.shots.find(s=>s.id===clip.shot_id)?.job?.result_path;return clip&&source?<div className="assembled-player"><div className="assembled-preview-frame"><video ref={timelineVideo} key={clip.shot_id} controls preload="metadata" src={`/api/media/file?path=${encodeURIComponent(source)}`} onPlay={()=>setTimelinePlaying(true)} onPause={()=>setTimelinePlaying(false)} onEnded={()=>{if(timelineIndex+1<manifest.shots.length){setTimelineIndex(timelineIndex+1);setTimelinePlaying(true)}else setTimelinePlaying(false)}}/>{clip.caption&&<div className="assembled-caption">{clip.caption}</div>}</div><div className="assembled-player-actions"><button className="button secondary mini" onClick={()=>{if(timelinePlaying){timelineVideo.current?.pause();setTimelinePlaying(false)}else{setTimelinePlaying(true);void timelineVideo.current?.play()}}}>{timelinePlaying?'Pause timeline':'Play timeline'}</button><span>Shot {timelineIndex+1} of {manifest.shots.length} · {clip.scene_title} · {clip.title}</span></div></div>:null})()}<div className="timeline-track">{manifest.shots.map((clip,index)=>{const source=story.shots.find(s=>s.id===clip.shot_id)?.job?.result_path;return <div className={`timeline-clip ${index===timelineIndex?'current':''}`} key={clip.shot_id}><button className="timeline-clip-label" onClick={()=>{setTimelineIndex(index);setTimelinePlaying(false)}}>{clip.scene_title} · {String(index+1).padStart(2,'0')} · {clip.title}</button><small>{clip.frames} frames{clip.transition_in_frames?` · ${clip.transition_in_frames}f crossfade`:''}</small>{clip.caption&&<small>Caption: {clip.caption}</small>}{!source&&<small>Video unavailable</small>}</div>})}</div><p className="settings-intro">Preview plays the verified clips sequentially with caption overlays. It does not preview rendered crossfades; those are applied by Remotion. Audio is not included in this storyboard composition. Rendering remains a separate confirmed action.</p><button className="button primary" onClick={()=>void renderComposition()} disabled={busy!==''||dirty}><Check size={14}/> Confirm & queue Remotion render</button></section>}
  </section>
}
