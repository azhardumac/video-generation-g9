import { useEffect, useState } from 'react'
import { Aperture, ArrowDownRight, ArrowUpRight, Bell, BookOpen, Check, ChevronDown, CircleHelp, Clapperboard, Command, Film, FolderOpen, Gauge, HardDrive, History, Image, LayoutDashboard, ListVideo, LoaderCircle, Plus, Search, Settings, SlidersHorizontal, Sparkles, WandSparkles, X } from 'lucide-react'

type Preset = { name: string; frames: number; fps: number; duration: number; status: string; visual_drift_warning?: boolean }
type Job = { id: string; status: string; progress: number; payload: { prompt: string; preset: string }; created_at: string }
const nav = [
  { group: 'STUDIO', items: [['Overview', LayoutDashboard], ['Projects', FolderOpen], ['Storyboard', ListVideo], ['References', Image]] },
  { group: 'PRODUCTION', items: [['Generate video', WandSparkles], ['Job queue', Gauge], ['History & logs', History], ['Remotion render', Clapperboard]] },
  { group: 'WORKSPACE', items: [['Settings', Settings]] },
] as const
const dateLabel = new Intl.DateTimeFormat('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }).format(new Date()).toUpperCase()

export default function App() {
  const [active, setActive] = useState('Overview')
  const [presets, setPresets] = useState<Preset[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [preset, setPreset] = useState('Standard')
  const [prompt, setPrompt] = useState('A tiny scorpion frog emerges from a rain-soaked fern, cinematic macro lens, soft amber moonlight')
  const [notice, setNotice] = useState('')
  const [connected, setConnected] = useState(false)
  const [loading, setLoading] = useState(false)

  const refresh = async () => {
    try {
      const [p, j, h] = await Promise.all([fetch('/api/presets'), fetch('/api/jobs'), fetch('/api/health')])
      if (p.ok) setPresets(await p.json())
      if (j.ok) setJobs(await j.json())
      setConnected(h.ok)
    } catch { setConnected(false) }
  }
  useEffect(() => { void refresh(); const timer = window.setInterval(refresh, 4000); return () => clearInterval(timer) }, [])
  const running = jobs.filter(j => j.status === 'running').length

  async function createJob() {
    setLoading(true); setNotice('')
    try {
      const saved = await fetch('/api/settings')
      const defaults = saved.ok ? await saved.json() : {}
      const response = await fetch('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ settings: { model: 'dgrauet/ltx-2.3-mlx-q8', width: 448, height: 768, distilled_two_stage: true, low_ram: true, audio: false, ...defaults, prompt, preset } }) })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? 'Could not queue generation')
      setNotice('Generation added to the local queue'); setActive('Job queue'); await refresh()
    } catch (error) { setNotice(error instanceof Error ? error.message : 'Local API is unavailable') }
    finally { setLoading(false) }
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Aperture size={20}/></div><div><strong>FRAMEWORK</strong><span>AI VIDEO STUDIO</span></div></div>
      <button className="project-switch"><span className="project-icon">SF</span><span className="project-copy"><b>Scorpion Frog</b><small>Current project</small></span><ChevronDown size={15}/></button>
      <div className="nav-wrap">{nav.map(section => <div className="nav-section" key={section.group}><div className="nav-label">{section.group}</div>{section.items.map(([label, Icon]) => <button key={label} onClick={() => setActive(label)} className={`nav-item ${active === label ? 'active' : ''}`}><Icon size={17}/><span>{label}</span>{label === 'Job queue' && jobs.some(j => j.status === 'queued' || j.status === 'running') && <i className="nav-dot"/>}</button>)}</div>)}</div>
      <div className="sidebar-bottom"><div className="local-card"><div className="local-top"><span className={`status-dot ${connected ? 'online' : ''}`}/><b>{connected ? 'Local engine ready' : 'Engine offline'}</b><span className="local-badge">LOCAL</span></div><p>{connected ? 'Your studio is running on this machine.' : 'Start the FastAPI service to connect.'}</p><div className="local-meta"><span><HardDrive size={13}/> SQLite</span><span><Aperture size={13}/> MLX</span></div></div><button className="help-link"><CircleHelp size={16}/> Help & documentation</button><div className="profile"><div className="avatar">A</div><div><b>Azhar</b><small>Personal workspace</small></div><button aria-label="More options">•••</button></div></div>
    </aside>
    <main className="main-content">
      <header className="topbar"><div className="breadcrumbs"><span>Scorpion Frog</span><span className="slash">/</span><b>{active}</b></div><div className="top-actions"><div className="local-indicator"><span className={`status-dot ${connected ? 'online' : ''}`}/>{connected ? 'Local mode' : 'API disconnected'}</div><button className="icon-button" aria-label="Search"><Search size={17}/></button><button className="icon-button" aria-label="Notifications"><Bell size={17}/><i/></button><span className="top-divider"/><button className="avatar small">A</button></div></header>
      <div className="page-scroll"><div className="page-container">
        <div className="welcome-row"><div><div className="eyebrow"><span className="eyebrow-line"/> {dateLabel}</div><h1>{active === 'Overview' ? 'Good evening, Azhar' : active}</h1><p className="subtitle">{active === 'Overview' ? 'Your next frame is waiting to be made.' : 'A local-first production workspace for your next frame.'}</p></div><button className="button primary" onClick={() => setActive('Generate video')}><Plus size={16}/> New generation</button></div>
        {notice && <div className="notice"><span>{notice}</span><button onClick={() => setNotice('')} aria-label="Dismiss"><X size={14}/></button></div>}
        {active === 'Settings' && <SettingsPage presets={presets} selected={preset} onSelect={setPreset} onPresetsChanged={refresh} onMessage={setNotice}/>}
        {active === 'Job queue' && <JobsPage title="Local job queue" jobs={jobs.filter(j=>j.status==='queued'||j.status==='running'||j.status==='failed')} onChanged={refresh} onMessage={setNotice}/>}
        {active === 'History & logs' && <JobsPage title="Generation history" jobs={jobs.filter(j=>j.status==='completed'||j.status==='failed'||j.status==='cancelled')} onChanged={refresh} onMessage={setNotice}/>}
        {['Projects','Storyboard','References','Remotion render'].includes(active) && <WorkspacePage active={active} onGenerate={()=>setActive('Generate video')} onMessage={setNotice} onJobsChanged={refresh}/>}
        <div hidden={active !== 'Overview' && active !== 'Generate video'}>
        <section className="stats-grid"><Stat icon={<Film size={16}/>} label="TOTAL GENERATIONS" value={String(jobs.length).padStart(2,'0')} delta="All time"/><Stat icon={<LoaderCircle size={16}/>} label="IN THE QUEUE" value={String(jobs.filter(j=>j.status==='queued'||j.status==='running').length).padStart(2,'0')} delta={running ? `${running} running now` : 'Ready when you are'} accent/><Stat icon={<Check size={16}/>} label="COMPLETED" value={String(jobs.filter(j=>j.status==='completed').length).padStart(2,'0')} delta="This workspace"/><Stat icon={<HardDrive size={16}/>} label="OUTPUT STORAGE" value="Local" delta="On this machine"/></section>
        <div className="content-grid">
          <section className="panel generation-panel"><div className="panel-heading"><div><div className="section-kicker">CREATE SOMETHING</div><h2>Quick generation</h2><p>Turn a thought into a moving image.</p></div><div className="sparkle"><Sparkles size={18}/></div></div>
            <label className="field-label" htmlFor="prompt">YOUR PROMPT <span>Be descriptive</span></label><div className="prompt-wrap"><textarea id="prompt" maxLength={4000} value={prompt} onChange={e=>setPrompt(e.target.value)} placeholder="Describe the scene you want to create…"/><div className="prompt-footer"><span><Command size={12}/> Use <kbd>⌘</kbd><kbd>↵</kbd> to generate</span><span>{prompt.length} / 4,000</span></div></div>
            <div className="generation-controls"><div className="control-block"><label className="field-label">DURATION PRESET</label><div className="preset-row">{presets.filter(p=>p.name!=='Experimental').map(p=><button key={p.name} className={`preset-chip ${preset===p.name?'chosen':''}`} onClick={()=>setPreset(p.name)}><span>{p.name}</span><small>{p.duration.toFixed(2)}s</small></button>)}</div></div><div className="control-block model-block"><label className="field-label">MODEL <span>MLX · Q8</span></label><button className="model-select"><span className="model-glyph"><Aperture size={16}/></span><span><b>LTX-2.3</b><small>dgrauet / q8</small></span><ChevronDown size={15}/></button></div></div>
            <div className="generation-bottom"><span className="settings-summary"><SlidersHorizontal size={14}/> 448 × 768 <i/> 24 fps <i/> Two-stage <i/> No audio</span><button className="button generate-button" disabled={loading || !connected || !prompt.trim()} onClick={createJob}>{loading ? <LoaderCircle className="spin" size={16}/> : <WandSparkles size={16}/>} Generate video <ArrowUpRight size={15}/></button></div>
          </section>
          <section className="panel presets-panel"><div className="panel-heading compact"><div><div className="section-kicker">TUNED TO YOUR MACHINE</div><h2>Generation presets</h2></div><button className="text-button" onClick={()=>setActive('Settings')}>All presets <ArrowUpRight size={13}/></button></div><div className="preset-list">{presets.map(p=><button key={p.name} onClick={()=>setPreset(p.name)} className={`preset-item ${preset===p.name?'selected':''}`}><div className="preset-mark"><span style={{height: `${Math.min(30,10+p.frames/8)}px`}}/></div><div className="preset-info"><b>{p.name}{p.name==='Standard'&&<span className="default-label">DEFAULT</span>}</b><small>{p.frames} frames <i/> {p.fps} fps <i/> {p.duration.toFixed(2)}s</small></div><span className={`preset-status ${p.visual_drift_warning?'warning':''}`}>{p.status}</span></button>)}</div><div className="benchmark-note"><Gauge size={14}/><span>Presets reflect verified local benchmarks.</span></div></section>
        </div>
        <div className="lower-grid"><section className="panel queue-panel"><div className="panel-heading compact"><div><div className="section-kicker">UP NEXT</div><h2>Recent activity</h2></div><button className="text-button" onClick={()=>setActive('Job queue')}>View queue <ArrowUpRight size={13}/></button></div>{jobs.length ? <div className="job-list">{jobs.slice(0,4).map(j=><div className="job-row" key={j.id}><div className={`job-icon ${j.status}`}><Film size={16}/></div><div className="job-copy"><b>{j.payload.prompt || 'Untitled generation'}</b><small>{j.payload.preset} · {new Date(j.created_at).toLocaleString()}</small></div><span className={`job-status ${j.status}`}>{j.status}</span>{j.status==='running'&&<div className="mini-progress"><span style={{width:`${j.progress}%`}}/></div>}</div>)}</div> : <div className="empty-state"><div className="empty-icon"><Film size={20}/></div><b>No generations yet</b><span>Your creations and renders will show up here.</span><button className="text-button" onClick={()=>setActive('Generate video')}>Create your first video <ArrowUpRight size={13}/></button></div>}</section>
          <section className="panel project-panel"><div className="panel-heading compact"><div><div className="section-kicker">YOUR WORKSPACE</div><h2>Recent projects</h2></div><button className="text-button" onClick={()=>setActive('Projects')}>All projects <ArrowUpRight size={13}/></button></div><div className="project-card"><div className="project-art"><div className="art-orb"/><div className="art-leaf one"/><div className="art-leaf two"/><span>FRAME 01</span></div><div className="project-card-copy"><div><b>Scorpion Frog</b><span>Created just now</span></div><button className="icon-button" aria-label="Open project"><ArrowDownRight size={15}/></button></div></div></section></div>
        <footer className="footer"><span><span className="status-dot online"/> Everything runs locally</span><span><BookOpen size={13}/> Studio v0.1.0 <i/> <span>Made for the frames in your head.</span></span></footer>
        </div>
      </div></div>
    </main>
  </div>
}

function Stat({ icon, label, value, delta, accent=false }: { icon: React.ReactNode; label: string; value: string; delta: string; accent?: boolean }) {
  return <div className="stat-card"><div className={`stat-icon ${accent?'accent':''}`}>{icon}</div><div className="stat-label">{label}</div><div className="stat-value">{value}</div><div className="stat-delta">{delta}</div></div>
}

function SettingsPage({ presets, selected, onSelect, onPresetsChanged, onMessage }: { presets: Preset[]; selected: string; onSelect: (name: string) => void; onPresetsChanged: () => Promise<void>; onMessage: (message: string) => void }) {
  const [values, setValues] = useState({ model: 'dgrauet/ltx-2.3-mlx-q8', width: 448, height: 768, distilled_two_stage: true, low_ram: true, audio: false, seed: null as number | null, reference_image: '', output_location: '', preset: 'Standard' })
  const [name, setName] = useState(''); const [frames, setFrames] = useState(97); const [fps, setFps] = useState(24)
  useEffect(() => { fetch('/api/settings').then(r=>r.ok?r.json():null).then(data=>{ if(data) setValues(v=>({...v,...data, reference_image:data.reference_image??'', output_location:data.output_location??''})) }).catch(()=>{}) }, [])
  const update = (key: string, value: string | number | boolean | null) => setValues(v=>({...v,[key]:value}))
  async function save() {
    const body = {...values, reference_image: values.reference_image || null, output_location: values.output_location || null, preset: selected}
    const response = await fetch('/api/settings', {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    const result = await response.json(); onMessage(response.ok ? 'Generation defaults saved locally' : result.detail ?? 'Could not save settings')
  }
  async function addPreset() {
    const response = await fetch('/api/presets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,frames,fps,status:'Custom'})})
    const result=await response.json()
    if (!response.ok) { onMessage(result.detail ?? 'Could not add preset'); return }
    setName(''); onMessage('Custom preset saved'); await onPresetsChanged()
  }
  return <section className="panel settings-page"><div className="section-kicker">LOCAL GENERATION</div><h2>Generation defaults</h2><p className="settings-intro">Settings stay on this machine and apply to new generations.</p>
    <div className="settings-form-grid"><label className="setting-field wide">Model<input value={values.model} onChange={e=>update('model',e.target.value)}/></label><label className="setting-field">Width<input type="number" step="8" min="64" value={values.width} onChange={e=>update('width',Number(e.target.value))}/></label><label className="setting-field">Height<input type="number" step="8" min="64" value={values.height} onChange={e=>update('height',Number(e.target.value))}/></label><label className="setting-field">Seed<input type="number" min="0" placeholder="Random" value={values.seed ?? ''} onChange={e=>update('seed',e.target.value ? Number(e.target.value) : null)}/></label><label className="setting-field">Output folder<input placeholder="~/AI-Video-Studio/outputs" value={values.output_location} onChange={e=>update('output_location',e.target.value)}/></label><label className="setting-field wide">Reference image path<input placeholder="Optional local image path" value={values.reference_image} onChange={e=>update('reference_image',e.target.value)}/></label></div>
    <div className="setting-toggles"><label><input type="checkbox" checked={values.distilled_two_stage} onChange={e=>update('distilled_two_stage',e.target.checked)}/> Distilled two-stage</label><label><input type="checkbox" checked={values.low_ram} onChange={e=>update('low_ram',e.target.checked)}/> Low RAM mode</label><label><input type="checkbox" checked={values.audio} onChange={e=>update('audio',e.target.checked)}/> Generate audio</label></div>
    <div className="setting-actions"><button className="button primary" onClick={save}><Check size={14}/> Save defaults</button></div>
    <div className="settings-divider"/><div className="settings-presets-heading"><div><div className="section-kicker">GENERATION PRESETS</div><h2>Choose a benchmark preset</h2></div></div>
    <div className="settings-preset-grid">{presets.map(p=><button key={p.name} className={`settings-preset ${selected===p.name?'chosen':''}`} onClick={()=>{onSelect(p.name);update('preset',p.name)}}><b>{p.name}</b><span>{p.frames} frames · {p.fps} fps</span><small>{p.duration.toFixed(2)}s · {p.status}</small></button>)}</div>
    <div className="custom-preset"><div><h3>Add custom preset</h3><p>Frame counts must follow the LTX 8n + 1 rule.</p></div><input aria-label="Preset name" placeholder="Name" value={name} onChange={e=>setName(e.target.value)}/><input aria-label="Frame count" type="number" min="9" max="193" step="8" value={frames} onChange={e=>setFrames(Number(e.target.value))}/><input aria-label="Frames per second" type="number" min="1" max="60" value={fps} onChange={e=>setFps(Number(e.target.value))}/><button className="button secondary" disabled={!name.trim()} onClick={addPreset}><Plus size={14}/> Add preset</button></div>
  </section>
}

function JobsPage({ title, jobs, onChanged, onMessage }: { title: string; jobs: Job[]; onChanged: () => Promise<void>; onMessage: (message: string) => void }) {
  async function action(id: string, endpoint: 'cancel'|'retry') {
    const response=await fetch(`/api/jobs/${id}/${endpoint}`,{method:'POST'}); const data=await response.json()
    onMessage(response.ok ? `Job ${endpoint==='cancel'?'cancelled':'queued for retry'}` : data.detail ?? `Could not ${endpoint} job`); await onChanged()
  }
  return <section className="panel workspace-page"><div className="section-kicker">LOCAL WORKER · ONE JOB AT A TIME</div><h2>{title}</h2>{jobs.length ? <div className="full-job-list">{jobs.map(job=><div className="full-job" key={job.id}><div className={`job-icon ${job.status}`}><Film size={16}/></div><div className="job-copy"><b>{job.payload.prompt || 'Untitled job'}</b><small>{job.payload.preset ?? job.status} · {new Date(job.created_at).toLocaleString()}</small><div className="full-progress"><span style={{width:`${job.progress}%`}}/></div></div><span className={`job-status ${job.status}`}>{job.status} · {job.progress}%</span>{(job.status==='queued'||job.status==='running')&&<button className="button secondary mini" onClick={()=>action(job.id,'cancel')}>Cancel</button>}{job.status==='failed'&&<button className="button secondary mini" onClick={()=>action(job.id,'retry')}>Retry</button>}</div>)}</div> : <div className="empty-workspace"><div className="empty-icon"><ListVideo size={19}/></div><b>Nothing here yet</b><span>Local jobs will appear here when you start a render or generation.</span></div>}</section>
}

function WorkspacePage({ active, onGenerate, onMessage, onJobsChanged }: { active: string; onGenerate: () => void; onMessage: (message: string) => void; onJobsChanged: () => Promise<void> }) {
  const [entrypoint,setEntrypoint]=useState('~/AI-Video-Studio/remotion-studio/src/index.ts')
  const [composition,setComposition]=useState('ScorpionFrogCinematicV3')
  const [output,setOutput]=useState('~/AI-Video-Studio/outputs/scorpion-frog-render.mp4')
  async function queueRender() {
    const response=await fetch('/api/renders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entrypoint,composition_id:composition,output_path:output})})
    const data=await response.json(); onMessage(response.ok?'Remotion render added to the local queue':data.detail??'Could not queue the Remotion render'); if(response.ok) await onJobsChanged()
  }
  const detail: Record<string,string> = { Projects:'Organize productions and keep their shots, references, and renders together.', Storyboard:'Plan scenes and shots before sending them to the local generation queue.', References:'Keep character and scene reference paths connected to your local project.', 'Remotion render':'Queue a render using your existing local Remotion studio.' }
  return <section className="panel workspace-page"><div className="section-kicker">SCORPION FROG · LOCAL PROJECT</div><h2>{active}</h2><p className="settings-intro">{detail[active]}</p>{active==='Remotion render'?<><div className="settings-form-grid"><label className="setting-field wide">Remotion entrypoint<input value={entrypoint} onChange={e=>setEntrypoint(e.target.value)}/></label><label className="setting-field">Composition ID<input value={composition} onChange={e=>setComposition(e.target.value)}/></label><label className="setting-field">Output video path<input value={output} onChange={e=>setOutput(e.target.value)}/></label></div><button className="button primary render-button" onClick={queueRender}><Clapperboard size={14}/> Queue render</button><p className="settings-intro render-note">The local Remotion CLI will render on request. The result is checked with ffprobe.</p></>:<div className="workspace-placeholder"><div className="empty-icon">{active==='References'?<Image size={19}/>:<FolderOpen size={19}/>}</div><b>{active==='Projects'?'One project is connected':active==='Storyboard'?'Shot planning is ready':'Reference library is ready'}</b><span>{`This Phase 1 workspace is connected to ${active==='References'?'local filesystem paths':'the local API and SQLite database'}.`}</span>{active==='Storyboard'&&<button className="button primary" onClick={onGenerate}><WandSparkles size={14}/> Generate a shot</button>}</div>}</section>
}
