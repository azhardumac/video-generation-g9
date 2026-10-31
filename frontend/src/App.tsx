import { useEffect, useState } from 'react'
import { Aperture, ArrowUpRight, BookOpen, Check, ChevronDown, Command, Film, FolderOpen, Gauge, HardDrive, History, Image, LayoutDashboard, ListVideo, LoaderCircle, Plus, Settings, SlidersHorizontal, Sparkles, WandSparkles, X, AlertTriangle } from 'lucide-react'
import StoryboardPage from './StoryboardPage'
import ProjectsPage, { type Project } from './ProjectsPage'
import ProjectReferencesPage from './ProjectReferencesPage'
import StyleBiblePage from './StyleBiblePage'
import FilmPlanPage from './FilmPlanPage'

type Preset = { name: string; frames: number; fps: number; duration: number; status: string; visual_drift_warning?: boolean }
type Asset = { id: string; category: string; relative_path: string; path: string; name: string; size_bytes: number; mime_type: string }
type JobEvent = { id: number; time: string; level: string; message: string }
type Job = { id: string; kind?:string; status: string; progress: number; payload: { prompt?: string; preset?: string; frames?:number; fps?:number; seed?:number|null; reference_image?:string|null; composition_id?:string; job_label?:string; preflight_warnings?:string[] }; created_at: string; result_path?: string | null; error?:string|null; exit_code?:number|null; elapsed_seconds?:number|null; verification_metadata?:{verified_output?:{width:number;height:number;fps:number;frames:number}}|null; partial_output_path?:string|null; events?: JobEvent[] }
type Integration = { ltx: { root: string; installed: boolean; runner: string; runner_available: boolean; model: string; hf_home: string; cache_available: boolean; weights_cached: boolean; ready: boolean }; remotion: { root: string; installed: boolean; cli: string; cli_available: boolean; version: string | null; ready: boolean }; story_project: { root: string; available: boolean }; ffprobe: { available: boolean; path: string | null } }
type Resources = { disk_free_bytes: number; memory: { total_bytes: number | null; available_bytes: number | null }; swap: { total_bytes: number | null; used_bytes: number | null }; memory_pressure: { free_percent: number; level: string } | null; warnings:string[] }
const nav = [
  { group: 'STUDIO', items: [['Overview', LayoutDashboard], ['Projects', FolderOpen], ['Storyboard', ListVideo], ['Story to Film', Film], ['Export / Remotion', Film], ['References', Image], ['Style bible', BookOpen]] },
  { group: 'PRODUCTION', items: [['Generate video', WandSparkles], ['Job queue', Gauge], ['History & logs', History]] },
  { group: 'WORKSPACE', items: [['Settings', Settings]] },
] as const
const dateLabel = new Intl.DateTimeFormat('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }).format(new Date()).toUpperCase()
const jobTitle = (job:Job) => job.payload.job_label || (job.kind === 'render' ? `Remotion render · ${job.payload.composition_id || 'storyboard composition'}` : job.payload.prompt || 'Untitled generation')
const jobTypeLabel = (job:Job) => job.kind === 'render' ? `Remotion · ${job.payload.composition_id || 'StoryboardAssembly'}` : job.payload.preset || 'Generation'

export default function App() {
  const [active, setActive] = useState('Overview')
  const [presets, setPresets] = useState<Preset[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [assets, setAssets] = useState<Asset[]>([])
  const [integrations, setIntegrations] = useState<Integration | null>(null)
  const [resources, setResources] = useState<Resources | null>(null)
  const [projects, setProjects] = useState<Project[]>([])
  const [activeProject, setActiveProject] = useState<Project | null>(null)
  const [preset, setPreset] = useState('Standard')
  const [prompt, setPrompt] = useState('')
  const [referenceImage, setReferenceImage] = useState('')
  const [seed, setSeed] = useState('')
  const [outputPath, setOutputPath] = useState('')
  const [generationProfile, setGenerationProfile] = useState({model:'dgrauet/ltx-2.3-mlx-q8',width:448,height:768,fps:24,distilled_two_stage:true,low_ram:true,audio:false})
  const [notice, setNotice] = useState('')
  const [connected, setConnected] = useState(false)
  const [loading, setLoading] = useState(false)
  const [unsavedStoryboard, setUnsavedStoryboard] = useState(false)

  const refresh = async () => {
    try {
      const [p, h, i, projectResponse, activeResponse] = await Promise.all([fetch('/api/presets'), fetch('/api/health'), fetch('/api/integrations'), fetch('/api/projects'), fetch('/api/projects/active')])
      if (p.ok) setPresets(await p.json())
      if (i.ok) setIntegrations(await i.json())
      if (projectResponse.ok) {
        const projectList: Project[] = await projectResponse.json()
        setProjects(projectList)
        const activeData = activeResponse.ok ? await activeResponse.json() : {project:null}
        const selected = activeData.project as Project | null
        setActiveProject(selected)
        if (selected) {
          const [assetResponse, referenceResponse, jobResponse] = await Promise.all([fetch(`/api/projects/${selected.id}/assets`), fetch(`/api/projects/${selected.id}/references`), fetch(`/api/jobs?project_id=${encodeURIComponent(selected.id)}`)])
          if (jobResponse.ok) setJobs(await jobResponse.json())
          else setJobs([])
          const indexedAssets:Asset[]=assetResponse.ok?await assetResponse.json():[]
          const references:Asset[]=referenceResponse.ok?(await referenceResponse.json()).map((ref:{id:string;path:string;name:string;category:string;size_bytes:number;mime_type:string})=>({id:`reference-${ref.id}`,category:`reference_${ref.category}`,relative_path:ref.name,path:ref.path,name:ref.name,size_bytes:ref.size_bytes,mime_type:ref.mime_type})):[]
          setAssets([...references,...indexedAssets])
        } else { setAssets([]); setJobs([]) }
      }
      setConnected(h.ok)
    } catch { setConnected(false) }
  }
  useEffect(() => {
    const refreshResources=async()=>{try{const response=await fetch('/api/resources');if(response.ok)setResources(await response.json())}catch{}}
    void refresh();void refreshResources()
    const timer=window.setInterval(refresh,2500);const resourceTimer=window.setInterval(refreshResources,10000)
    return()=>{clearInterval(timer);clearInterval(resourceTimer)}
  }, [])
  async function loadProjectSettings(projectId:string) {
    const response=await fetch(`/api/settings?project_id=${encodeURIComponent(projectId)}`)
    if(!response.ok)return
    const data=await response.json()
    setPrompt(typeof data.prompt==='string'?data.prompt:'');setReferenceImage(data.reference_image??'');setSeed(data.seed==null?'':String(data.seed));setOutputPath(data.output_location??'')
    setPreset(typeof data.preset==='string'?data.preset:'Standard')
    setGenerationProfile({model:data.model??'dgrauet/ltx-2.3-mlx-q8',width:data.width??448,height:data.height??768,fps:data.fps??24,distilled_two_stage:data.distilled_two_stage??true,low_ram:data.low_ram??true,audio:data.audio??false})
  }
  async function recheckLocalStatus() {
    const [healthResponse, integrationResponse, resourceResponse] = await Promise.all([
      fetch('/api/health'), fetch('/api/integrations'), fetch('/api/resources'),
    ])
    if(!healthResponse.ok||!integrationResponse.ok||!resourceResponse.ok)throw new Error('The local API could not complete all checks')
    setConnected(true);setIntegrations(await integrationResponse.json());setResources(await resourceResponse.json())
  }
  useEffect(() => {
    setPrompt(''); setReferenceImage(''); setSeed('');setOutputPath('')
    if(activeProject) void loadProjectSettings(activeProject.id).catch(()=>{})
  }, [activeProject?.id])
  const running = jobs.filter(j => j.status === 'running').length

  async function createJob() {
    if (!activeProject) { setNotice('Select a project before queueing a generation'); return }
    setLoading(true); setNotice('')
    try {
      const saved = await fetch(`/api/settings?project_id=${encodeURIComponent(activeProject.id)}`)
      const defaults = saved.ok ? await saved.json() : {}
      const response = await fetch('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id:activeProject.id, settings: { model: 'dgrauet/ltx-2.3-mlx-q8', width: 448, height: 768, distilled_two_stage: true, low_ram: true, audio: false, ...defaults, prompt, preset, reference_image: referenceImage || defaults.reference_image || null, seed: seed ? Number(seed) : defaults.seed ?? null, output_location: outputPath || defaults.output_location || null } }) })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? 'Could not queue generation')
      setNotice('Generation added to the local queue'); setActive('Job queue'); await refresh()
    } catch (error) { setNotice(error instanceof Error ? error.message : 'Local API is unavailable') }
    finally { setLoading(false) }
  }

  async function refreshProjects() {
    const response=await fetch('/api/projects');if(response.ok)setProjects(await response.json())
    await refresh()
  }
  async function selectProject(projectId:string) {
    if(unsavedStoryboard&&!window.confirm('Discard unsaved storyboard changes and switch projects?'))return
    const response=await fetch('/api/projects/active',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:projectId||null})})
    const data=await response.json();if(!response.ok)throw new Error(data.detail??'Could not switch project')
    setActiveProject(data.project);setActive('Overview');await refresh()
  }

  function selectPreset(name: string, warning?: boolean) {
    if (warning && !window.confirm('Experimental preset: 193 frames may show visual drift. Continue with this preset?')) return
    setPreset(name)
  }

  function navigate(label:string){
    if(active==='Storyboard'&&unsavedStoryboard&&!window.confirm('Discard unsaved storyboard changes and leave this page?'))return
    setActive(label)
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Aperture size={20}/></div><div><strong>FRAMEWORK</strong><span>AI VIDEO STUDIO</span></div></div>
      <label className="project-switch-label" aria-label="Active project"><span className="project-icon">{activeProject?.name.slice(0,2).toUpperCase()??'—'}</span><select className="project-switch" aria-label="Active project" value={activeProject?.id??''} onChange={e=>void selectProject(e.target.value).catch(error=>setNotice(error.message))}><option value="">No active project</option>{projects.map(project=><option key={project.id} value={project.id}>{project.name}</option>)}</select><ChevronDown size={15}/></label>
      <div className="nav-wrap">{nav.map(section => <div className="nav-section" key={section.group}><div className="nav-label">{section.group}</div>{section.items.map(([label, Icon]) => <button key={label} aria-label={label} title={label} onClick={() => navigate(label)} className={`nav-item ${active === label ? 'active' : ''}`}><Icon size={17}/><span>{label}</span>{label === 'Job queue' && jobs.some(j => j.status === 'queued' || j.status === 'running') && <i className="nav-dot"/>}</button>)}</div>)}</div>
      <div className="sidebar-bottom"><div className="local-card"><div className="local-top"><span className={`status-dot ${connected ? 'online' : ''}`}/><b>{connected ? 'Local API connected' : 'API offline'}</b><span className="local-badge">LOCAL</span></div><p>{connected ? 'Project data is stored in the local SQLite database.' : 'Start the FastAPI service to connect.'}</p><div className="local-meta"><span><HardDrive size={13}/> SQLite</span><span><Aperture size={13}/> MLX</span></div></div></div>
    </aside>
    <main className="main-content">
      <header className="topbar"><div className="breadcrumbs"><span>{activeProject?.name??'No project selected'}</span><span className="slash">/</span><b>{active}</b></div><div className="top-actions"><div className="local-indicator"><span className={`status-dot ${connected ? 'online' : ''}`}/>{connected ? 'Local mode' : 'API disconnected'}</div><span className="top-divider"/><span>{activeProject?'Project data stays local':'Select a project to begin'}</span></div></header>
      <div className="page-scroll"><div className="page-container">
        <div className="welcome-row"><div><div className="eyebrow"><span className="eyebrow-line"/> {dateLabel}</div><h1>{active === 'Overview' ? activeProject?.name??'Local video studio' : active}</h1><p className="subtitle">{active === 'Overview' ? activeProject?.description||'Create or select a project to start planning.' : 'A local-first production workspace.'}</p></div>{activeProject&&<button className="button primary" onClick={() => navigate('Generate video')}><Plus size={16}/> New generation</button>}</div>
        {notice && <div className="notice"><span>{notice}</span><button onClick={() => setNotice('')} aria-label="Dismiss"><X size={14}/></button></div>}
        {active === 'Settings' && <SettingsPage presets={presets} selected={preset} onSelect={selectPreset} onPresetsChanged={refresh} onSaved={()=>activeProject?loadProjectSettings(activeProject.id):Promise.resolve()} onRecheck={recheckLocalStatus} onMessage={setNotice} integrations={integrations} resources={resources} projectId={activeProject?.id??null}/>}
        {active === 'Job queue' && <JobsPage title="Local job queue" jobs={jobs.filter(j=>j.status==='queued'||j.status==='running'||j.status==='failed')} onChanged={refresh} onMessage={setNotice}/>}
        {active === 'History & logs' && <JobsPage title="Generation history" jobs={jobs.filter(j=>j.status==='completed'||j.status==='failed'||j.status==='cancelled')} onChanged={refresh} onMessage={setNotice}/>}
        {active === 'Projects' && <ProjectsPage projects={projects} activeId={activeProject?.id??null} onChanged={refreshProjects} onSelect={selectProject} onMessage={setNotice}/>}
        {active === 'References' && <ProjectReferencesPage projectId={activeProject?.id??null} onMessage={setNotice} onUse={path=>{setReferenceImage(path);setActive('Generate video')}}/>}
        {active === 'Style bible' && <StyleBiblePage projectId={activeProject?.id??null} onMessage={setNotice}/>}
        {active === 'Storyboard' && <StoryboardPage projectId={activeProject?.id??null} project={activeProject} assets={assets} jobs={jobs} ltxReady={Boolean(integrations?.ltx.ready)} onMessage={setNotice} onDirtyChange={setUnsavedStoryboard}/>}
        {(active === 'Story to Film' || active === 'Export / Remotion') && <FilmPlanPage projectId={activeProject?.id??null} projectName={activeProject?.name??''} onMessage={setNotice} onNavigate={navigate} focusExport={active === 'Export / Remotion'}/>}
        <div hidden={active !== 'Overview' && active !== 'Generate video'}>
        <section className="stats-grid"><Stat icon={<Film size={16}/>} label="TOTAL GENERATIONS" value={String(jobs.filter(j=>j.kind==='generation').length).padStart(2,'0')} delta="Selected project"/><Stat icon={<LoaderCircle size={16}/>} label="IN THE QUEUE" value={String(jobs.filter(j=>j.status==='queued'||j.status==='running').length).padStart(2,'0')} delta={running ? `${running} running now` : 'No active jobs'} accent/><Stat icon={<Check size={16}/>} label="COMPLETED GENERATIONS" value={String(jobs.filter(j=>j.kind==='generation'&&j.status==='completed').length).padStart(2,'0')} delta="Selected project"/><Stat icon={<HardDrive size={16}/>} label="OUTPUT DESTINATION" value={outputPath||'Configured default'} delta="Local path"/></section>
        <div className="content-grid">
          <section className="panel generation-panel"><div className="panel-heading"><div><div className="section-kicker">CREATE SOMETHING</div><h2>Quick generation</h2><p>Turn a thought into a moving image.</p></div><div className="sparkle"><Sparkles size={18}/></div></div>
            <label className="field-label" htmlFor="prompt">YOUR PROMPT <span>Be descriptive</span></label><div className="prompt-wrap"><textarea id="prompt" maxLength={4000} value={prompt} onChange={e=>setPrompt(e.target.value)} onKeyDown={e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();void createJob()}}} placeholder="Write a prompt for this project…"/><div className="prompt-footer"><span><Command size={12}/> Use <kbd>⌘</kbd><kbd>↵</kbd> to generate</span><span>{prompt.length} / 4,000</span></div></div>
            <div className="generation-controls"><div className="control-block"><label className="field-label">DURATION PRESET</label><div className="preset-row">{presets.filter(p=>p.name!=='Experimental').map(p=><button key={p.name} className={`preset-chip ${preset===p.name?'chosen':''}`} onClick={()=>selectPreset(p.name)}><span>{p.name}</span><small>{p.frames}f · {p.duration.toFixed(2)}s</small></button>)}</div></div><div className="control-block model-block"><label className="field-label">VALIDATED MODEL PROFILE <span>LOCAL · MLX · Q8</span></label><div className="model-select"><span className="model-glyph"><Aperture size={16}/></span><span><b>{integrations?.ltx.model||'LTX-2.3 Q8'}</b><small>{integrations?.ltx.ready?'Detected local profile':'Local profile not detected'}</small></span></div></div></div>
            <div className="generation-fields"><label className="setting-field">Reference image<select value={referenceImage} onChange={e=>setReferenceImage(e.target.value)}><option value="">No reference · text to video</option>{assets.filter(asset=>asset.mime_type.startsWith('image/')).map(asset=><option key={asset.id} value={asset.path}>{asset.relative_path}</option>)}</select></label><label className="setting-field">Seed<input type="number" min="0" placeholder="Random seed" value={seed} onChange={e=>setSeed(e.target.value)}/></label><label className="setting-field">Output path<input value={outputPath} onChange={e=>setOutputPath(e.target.value)} placeholder="Leave blank for configured output root"/></label></div>
            {!integrations?.ltx.ready&&<div className="model-caution"><AlertTriangle size={13}/><span>Q8 model files are not detected. Generation stays disabled; no model download will be attempted.</span></div>}
            {Boolean(resources?.warnings?.length)&&<div className="model-caution preflight-warning"><AlertTriangle size={13}/><span>Preflight safeguard: {resources?.warnings.join(' ')}</span></div>}
            <div className="generation-bottom"><span className="settings-summary"><SlidersHorizontal size={14}/> {generationProfile.model} · {generationProfile.width} × {generationProfile.height} <i/> {presets.find(p=>p.name===preset)?.frames??'—'} frames at {presets.find(p=>p.name===preset)?.fps??generationProfile.fps} fps <i/> {generationProfile.distilled_two_stage?'Distilled two-stage':'Single-stage'} <i/> {generationProfile.audio?'Audio on':'No audio'}</span><button className="button generate-button" title={!activeProject?'Select a project first':!integrations?.ltx.ready?'LTX runner and cached model weights are required':''} disabled={loading || !connected || !activeProject || !prompt.trim() || !integrations?.ltx.ready} onClick={createJob}>{loading ? <LoaderCircle className="spin" size={16}/> : <WandSparkles size={16}/>} Generate video <ArrowUpRight size={15}/></button></div>
          </section>
          <section className="panel presets-panel"><div className="panel-heading compact"><div><div className="section-kicker">TUNED TO YOUR MACHINE</div><h2>Generation presets</h2></div><button className="text-button" onClick={()=>setActive('Settings')}>All presets <ArrowUpRight size={13}/></button></div><div className="preset-list">{presets.map(p=><button key={p.name} onClick={()=>selectPreset(p.name,p.visual_drift_warning)} className={`preset-item ${preset===p.name?'selected':''}`}><div className="preset-mark"><span style={{height: `${Math.min(30,10+p.frames/8)}px`}}/></div><div className="preset-info"><b>{p.name}{p.name==='Standard'&&<span className="default-label">DEFAULT</span>}</b><small>{p.frames} frames <i/> {p.fps} fps <i/> {p.duration.toFixed(2)}s</small></div><span className={`preset-status ${p.visual_drift_warning?'warning':''}`}>{p.status}</span></button>)}</div><div className="benchmark-note"><Gauge size={14}/><span>Experimental 193f requires confirmation; it may show visual drift.</span></div></section>
        </div>
        <div className="lower-grid"><section className="panel queue-panel"><div className="panel-heading compact"><div><div className="section-kicker">UP NEXT</div><h2>Recent activity</h2></div><button className="text-button" onClick={()=>setActive('Job queue')}>View queue <ArrowUpRight size={13}/></button></div>{jobs.length ? <div className="job-list">{jobs.slice(0,4).map(j=><div className="job-row" key={j.id}><div className={`job-icon ${j.status}`}><Film size={16}/></div><div className="job-copy"><b>{jobTitle(j)}</b><small>{jobTypeLabel(j)} · {new Date(j.created_at).toLocaleString()}</small></div><span className={`job-status ${j.status}`}>{j.status}</span>{j.status==='running'&&<div className="mini-progress"><span style={{width:`${j.progress}%`}}/></div>}</div>)}</div> : <div className="empty-state"><div className="empty-icon"><Film size={20}/></div><b>No jobs yet</b><span>Your creations and renders will show up here.</span><button className="text-button" onClick={()=>setActive('Generate video')}>Create your first video <ArrowUpRight size={13}/></button></div>}</section>
            <section className="panel project-panel"><div className="panel-heading compact"><div><div className="section-kicker">YOUR WORKSPACE</div><h2>Projects</h2></div><button className="text-button" onClick={()=>setActive('Projects')}>Manage projects <ArrowUpRight size={13}/></button></div>{activeProject?<div className="empty-state"><div className="empty-icon"><FolderOpen size={20}/></div><b>{activeProject.name}</b><span>{activeProject.reference_count} references · {activeProject.shot_count} shots · {activeProject.job_count} jobs</span><button className="text-button" onClick={()=>setActive('Projects')}>Project settings <ArrowUpRight size={13}/></button></div>:<div className="empty-state"><b>No project selected</b><span>Create or open a project to see its production data.</span><button className="text-button" onClick={()=>setActive('Projects')}>Create or import a project <ArrowUpRight size={13}/></button></div>}</section></div>
        <footer className="footer"><span><span className="status-dot online"/> Everything runs locally</span><span><BookOpen size={13}/> Studio v0.1.0 <i/> <span>Made for the frames in your head.</span></span></footer>
        </div>
      </div></div>
    </main>
  </div>
}
function Stat({ icon, label, value, delta, accent=false }: { icon: React.ReactNode; label: string; value: string; delta: string; accent?: boolean }) {
  return <div className="stat-card"><div className={`stat-icon ${accent?'accent':''}`}>{icon}</div><div className="stat-label">{label}</div><div className="stat-value">{value}</div><div className="stat-delta">{delta}</div></div>
}

function formatBytes(value?: number | null) {
  if (value == null) return '—'
  const units=['B','KB','MB','GB','TB']; let amount=value; let unit=0
  while(amount>=1024&&unit<units.length-1){amount/=1024;unit++}
  return `${amount.toFixed(unit>1?1:0)} ${units[unit]}`
}

function IntegrationCard({ title, ready, detail, path, secondaryPath, warning=false, action }: { title:string; ready:boolean; detail:string; path:string; secondaryPath?:string; warning?:boolean; action?:React.ReactNode }) {
  return <div className="integration-card"><div className="integration-head"><span className={`status-dot ${ready?'online':''}`}/><b>{title}</b><small className={warning?'warn-text':''}>{ready?'Connected':warning?'Needs attention':'Unavailable'}</small></div><div className="integration-detail">{detail}</div><code title={path}>{path}</code>{secondaryPath&&<code title={secondaryPath}>HF_HOME · {secondaryPath}</code>}{action&&<div className="integration-action">{action}</div>}</div>
}

function SettingsPage({ presets, selected, onSelect, onPresetsChanged, onSaved, onRecheck, onMessage, integrations, resources, projectId }: { presets: Preset[]; selected: string; onSelect: (name: string, warning?: boolean) => void; onPresetsChanged: () => Promise<void>; onSaved:()=>Promise<void>; onRecheck:()=>Promise<void>; onMessage: (message: string) => void; integrations: Integration | null; resources: Resources | null; projectId:string|null }) {
  const [values, setValues] = useState({ model: 'dgrauet/ltx-2.3-mlx-q8', width: 448, height: 768, distilled_two_stage: true, low_ram: true, audio: false, seed: null as number | null, reference_image: '', output_location: '', preset: 'Standard', prompt: '' })
  const [name, setName] = useState(''); const [frames, setFrames] = useState(97); const [fps, setFps] = useState(24)
  const [rechecking,setRechecking]=useState(false)
  useEffect(() => { setValues(v=>({...v,prompt:'',reference_image:'',seed:null}));if(projectId)fetch(`/api/settings?project_id=${encodeURIComponent(projectId)}`).then(r=>r.ok?r.json():null).then(data=>{if(data)setValues(v=>({...v,...data,prompt:data.prompt??'',reference_image:data.reference_image??'',output_location:data.output_location??''}))}).catch(()=>{}) }, [projectId])
  const update = (key: string, value: string | number | boolean | null) => setValues(v=>({...v,[key]:value}))
  async function save() {
    if(!projectId){onMessage('Select a project before saving generation defaults');return}
    try {
      const body = {...values, reference_image: values.reference_image || null, output_location: values.output_location || null, preset: selected}
      const response = await fetch(`/api/settings?project_id=${encodeURIComponent(projectId)}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
      const result = await response.json(); onMessage(response.ok ? 'Generation defaults saved locally' : result.detail ?? 'Could not save settings');if(response.ok)await onSaved()
    } catch { onMessage('Could not save settings: the local API is unavailable') }
  }
  async function addPreset() {
    try {
      const response = await fetch('/api/presets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,frames,fps,status:'Custom'})})
      const result=await response.json()
      if (!response.ok) { onMessage(result.detail ?? 'Could not add preset'); return }
      setName(''); onMessage('Custom preset saved'); await onPresetsChanged()
    } catch { onMessage('Could not add preset: the local API is unavailable') }
  }
  async function recheck(){setRechecking(true);try{await onRecheck();onMessage('Local integrations and resource safeguards rechecked')}catch(error){onMessage(error instanceof Error?error.message:'Could not recheck local integrations')}finally{setRechecking(false)}}
  return <section className="panel settings-page"><div className="settings-presets-heading"><div><div className="section-kicker">LOCAL CONNECTIONS</div><h2>Integration status</h2></div><button className="button secondary" onClick={()=>void recheck()} disabled={rechecking}>{rechecking?'Checking…':'Recheck local status'}</button></div><p className="settings-intro">Checks local installations, cache presence, ffprobe, and resource safeguards. Checks are read-only; no model is downloaded and no generation starts.</p>
    <div className="integration-grid">
      <IntegrationCard title="LTX-2.3 MLX · Q8" ready={Boolean(integrations?.ltx.ready)} detail={integrations?.ltx.weights_cached ? 'Runner and cached Q8 weights available' : integrations?.ltx.cache_available ? 'Hugging Face cache exists; expected Q8 weights missing. Check LTX_EXECUTABLE and HF_CACHE in .env.' : 'Hugging Face cache missing. Check HF_CACHE in .env; the app will not download weights.'} path={integrations?.ltx.root ?? 'Checking…'} secondaryPath={integrations?.ltx.hf_home} warning={Boolean(integrations && !integrations.ltx.weights_cached)}/>
      <IntegrationCard title="Remotion Studio" ready={Boolean(integrations?.remotion.ready)} detail={integrations?.remotion.version ? `CLI ${integrations.remotion.version}` : integrations?.remotion.cli_available ? 'CLI available' : 'CLI missing. Check REMOTION_STUDIO and REMOTION_EXECUTABLE in .env.'} path={integrations?.remotion.root ?? 'Checking…'}/>
      <IntegrationCard title="Active project" ready={Boolean(projectId)} detail={projectId?'Project selected':'Select or create a project'} path={projectId??'No active project'}/>
      <IntegrationCard title="ffprobe" ready={Boolean(integrations?.ffprobe.available)} detail={integrations?.ffprobe.available ? 'Video verification available' : 'ffprobe not found. Install FFmpeg locally or set FFPROBE_EXECUTABLE in .env.'} path={integrations?.ffprobe.path ?? 'Not detected'}/>
    </div>
    <div className="metric-strip"><div><span>MEMORY AVAILABLE</span><b>{formatBytes(resources?.memory.available_bytes)}</b></div><div><span>SWAP IN USE</span><b>{formatBytes(resources?.swap.used_bytes)}</b></div><div><span>DISK FREE</span><b>{formatBytes(resources?.disk_free_bytes)}</b></div><div><span>MEMORY PRESSURE</span><b className={resources?.memory_pressure?.level==='high'?'warn-text':''}>{resources?.memory_pressure ? `${resources.memory_pressure.level} · ${resources.memory_pressure.free_percent}% free` : 'Unavailable'}</b></div></div>
    {Boolean(resources?.warnings?.length)&&<div className="model-caution preflight-warning"><AlertTriangle size={13}/><span>{resources?.warnings.join(' ')}</span></div>}
    <div className="settings-divider"/><div className="section-kicker">LOCAL GENERATION</div><h2>Generation defaults</h2><p className="settings-intro">Settings stay on this machine and apply only to the selected project.</p>
    <div className="settings-form-grid"><label className="setting-field wide">Project prompt<textarea value={values.prompt} onChange={e=>update('prompt',e.target.value)}/></label><label className="setting-field wide">Model ID<input value={values.model} onChange={e=>update('model',e.target.value)} aria-describedby="model-profile-help"/></label><small id="model-profile-help" className="settings-intro">The selected model must already be installed and cached locally. This app does not download weights.</small><label className="setting-field">Width (px) · custom profile<input type="number" step="8" min="64" value={values.width} onChange={e=>update('width',Number(e.target.value))}/></label><label className="setting-field">Height (px) · custom profile<input type="number" step="8" min="64" value={values.height} onChange={e=>update('height',Number(e.target.value))}/></label><label className="setting-field">Seed<input type="number" min="0" placeholder="Random" value={values.seed ?? ''} onChange={e=>update('seed',e.target.value ? Number(e.target.value) : null)}/></label><label className="setting-field">Output folder<input placeholder="Resolved to this project's output folder" value={values.output_location} onChange={e=>update('output_location',e.target.value)}/></label><label className="setting-field wide">Reference image path<input placeholder="Optional local image path" value={values.reference_image} onChange={e=>update('reference_image',e.target.value)}/></label></div>
    <div className="setting-toggles"><label><input type="checkbox" checked={values.distilled_two_stage} onChange={e=>update('distilled_two_stage',e.target.checked)}/> Distilled two-stage</label><label><input type="checkbox" checked={values.low_ram} onChange={e=>update('low_ram',e.target.checked)}/> Low RAM mode</label><label><input type="checkbox" checked={values.audio} onChange={e=>update('audio',e.target.checked)}/> Generate audio</label></div>
    <div className="setting-actions"><button className="button primary" onClick={save}><Check size={14}/> Save defaults</button></div>
    <div className="settings-divider"/><div className="settings-presets-heading"><div><div className="section-kicker">GENERATION PRESETS</div><h2>Choose a benchmark preset</h2></div></div>
    <div className="settings-preset-grid">{presets.map(p=><button key={p.name} aria-pressed={selected===p.name} className={`settings-preset ${selected===p.name?'chosen':''}`} onClick={()=>{onSelect(p.name,p.visual_drift_warning);update('preset',p.name)}}><b>{p.name}{p.visual_drift_warning&&<AlertTriangle size={12}/>}</b><span>{p.frames} frames · {p.fps} fps</span><small>{p.duration.toFixed(2)}s · {p.status}</small>{p.visual_drift_warning&&<em>Confirm visual drift warning before using</em>}</button>)}</div>
    <div className="custom-preset"><div><h3>Add custom preset</h3><p>Frame counts must follow the LTX 8n + 1 rule.</p></div><input aria-label="Preset name" placeholder="Name" value={name} onChange={e=>setName(e.target.value)}/><input aria-label="Frame count" type="number" min="9" max="193" step="8" value={frames} onChange={e=>setFrames(Number(e.target.value))}/><input aria-label="Frames per second" type="number" min="1" max="60" value={fps} onChange={e=>setFps(Number(e.target.value))}/><button className="button secondary" disabled={!name.trim()} onClick={addPreset}><Plus size={14}/> Add preset</button></div>
  </section>
}

function JobsPage({ title, jobs, onChanged, onMessage }: { title: string; jobs: Job[]; onChanged: () => Promise<void>; onMessage: (message: string) => void }) {
  const [selectedId,setSelectedId]=useState('')
  const [detail,setDetail]=useState<Job|null>(null)
  const selected=jobs.find(job=>job.id===selectedId)??jobs[0]
  useEffect(()=>{if(selected&&!selectedId)setSelectedId(selected.id)},[selected,selectedId])
  useEffect(()=>{
    if(!selected)return
    let active=true
    const load=async()=>{try{const response=await fetch(`/api/jobs/${selected.id}`);if(response.ok&&active)setDetail(await response.json())}catch{}}
    void load();const timer=window.setInterval(load,1500)
    return()=>{active=false;clearInterval(timer)}
  },[selected?.id])
  async function action(id: string, endpoint: 'cancel'|'retry') {
    const response=await fetch(`/api/jobs/${id}/${endpoint}`,{method:'POST'}); const data=await response.json()
    onMessage(response.ok ? `Job ${endpoint==='cancel'?'cancelled':'queued for retry'}` : data.detail ?? `Could not ${endpoint} job`); await onChanged()
  }
  return <section className="panel workspace-page"><div className="section-kicker">LOCAL WORKER · ONE JOB AT A TIME</div><h2>{title}</h2>{jobs.length ? <><div className="full-job-list">{jobs.map(job=><div className={`full-job ${selected?.id===job.id?'picked':''}`} key={job.id}><button className="job-pick" onClick={()=>setSelectedId(job.id)}><div className={`job-icon ${job.status}`}><Film size={16}/></div><div className="job-copy"><b>{jobTitle(job)}</b><small>{jobTypeLabel(job)} · {new Date(job.created_at).toLocaleString()}{job.status==='completed'&&job.verification_metadata?.verified_output?` · VERIFIED · ${job.elapsed_seconds?.toFixed(1)??'—'} s · ${job.verification_metadata.verified_output.width} × ${job.verification_metadata.verified_output.height} · ${job.verification_metadata.verified_output.fps} fps · ${job.verification_metadata.verified_output.frames} frames`:''}</small><div className="full-progress"><span style={{width:`${job.progress}%`}}/></div></div></button><span className={`job-status ${job.status}`}>{job.status} · {job.progress}%</span>{(job.status==='queued'||job.status==='running')&&<button className="button secondary mini" onClick={()=>action(job.id,'cancel')}>Cancel</button>}{job.status==='failed'&&<button className="button secondary mini" onClick={()=>action(job.id,'retry')}>Retry</button>}</div>)}</div>{detail&&<div className="job-telemetry"><div><span>EXIT STATUS</span><b>{detail.exit_code==null?(detail.status==='cancelled'?'Cancelled':'—'):detail.exit_code===0?'0 · success':`${detail.exit_code} · failed`}</b></div><div><span>{detail.kind==='render'?'REMOTION RENDER DURATION':'GENERATION DURATION'}</span><b>{detail.elapsed_seconds==null?'—':`${detail.elapsed_seconds.toFixed(1)} s`}</b></div>{detail.verification_metadata?.verified_output&&<div><span>VERIFIED OUTPUT · FFPROBE</span><b>{detail.verification_metadata.verified_output.width} × {detail.verification_metadata.verified_output.height} · {detail.verification_metadata.verified_output.fps} fps · {detail.verification_metadata.verified_output.frames} frames</b></div>}{detail.partial_output_path&&<div className="telemetry-wide"><span>PARTIAL OUTPUT · NOT A COMPLETED VIDEO</span><code>{detail.partial_output_path}</code></div>}{detail.error&&<div className="telemetry-wide"><span>ERROR</span><code>{detail.error}</code></div>}{detail.payload.preflight_warnings?.map((warning,index)=><div className="telemetry-wide warning-text" key={index}><span>PREFLIGHT WARNING</span><code>{warning}</code></div>)}</div>}{detail?.result_path&&<VideoResult path={detail.result_path} onMessage={onMessage}/>}<div className="log-panel"><div className="section-kicker">LIVE JOB LOG · AUTO REFRESH</div>{detail?.events?.length?<div className="job-logs">{detail.events.slice(-30).map(event=><div className={`job-log ${event.level}`} key={event.id}><time>{new Date(event.time).toLocaleTimeString()}</time><span>{event.message}</span></div>)}</div>:<div className="log-empty">Waiting for worker output…</div>}</div></> : <div className="empty-workspace"><div className="empty-icon"><ListVideo size={19}/></div><b>Nothing here yet</b><span>Local jobs will appear here when you start a render or generation.</span></div>}</section>
}

function VideoResult({path,onMessage}:{path:string;onMessage:(message:string)=>void}) {
  const [result,setResult]=useState<{verified:boolean;format?:{duration?:string;format_name?:string};streams?:{codec_type:string;codec_name:string}[];error?:string}|null>(null)
  async function verify(){try{const response=await fetch(`/api/verify-output?path=${encodeURIComponent(path)}`,{method:'POST'});const data=await response.json();setResult(response.ok?data:{verified:false,error:data.detail??'ffprobe verification failed'})}catch{setResult({verified:false,error:'Could not verify output: the local API is unavailable'})}}
  async function reveal(){try{const response=await fetch('/api/outputs/reveal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});const data=await response.json();onMessage(response.ok?`Revealed output in Finder: ${path}`:data.detail??'Could not reveal output')}catch{onMessage('Could not reveal output: the local API is unavailable')}}
  return <div className="video-result"><div className="section-kicker">PROJECT OUTPUT · PLAYBACK</div><div className="video-preview-frame"><video controls preload="metadata" src={`/api/media/file?path=${encodeURIComponent(path)}`}/></div><div className="video-result-footer"><code title={path}>{path}</code><button className="button secondary mini" onClick={verify}><Check size={12}/> ffprobe</button><button className="button secondary mini" onClick={()=>void reveal()}>Reveal in Finder</button></div>{result&&<div className={`verify-result ${result.verified?'ok':'error'}`}>{result.verified?`${result.format?.format_name??'Video'} · ${Number(result.format?.duration??0).toFixed(2)}s · ${result.streams?.map(stream=>stream.codec_name).join(', ')}`:result.error}</div>}</div>
}
