import { Film } from 'lucide-react'

type GenericShot = {id:string;scene_title?:string|null;title?:string|null;prompt?:string|null;framing?:string|null;characters?:unknown;status?:string;job?:{result_path?:string|null}|null}
type GenericPlan = {title?:string|null;story_text?:string|null;shots?:GenericShot[]}

export default function GenericRunPlanSummary({plan,onOpenStoryboard}:{plan:GenericPlan;onOpenStoryboard:()=>void}){
  const shots=Array.isArray(plan.shots)?plan.shots:[]
  return <section className="generic-run-plan-summary" aria-label="Generic Gemini story plan">
    <div className="section-kicker">GEMINI STORY PLAN · READ ONLY IN THIS VIEW</div>
    <h3>{plan.title||'Untitled story plan'}</h3>
    <p>{plan.story_text||'No source story text was saved.'}</p>
    <div className="generic-run-shot-list">{shots.map((shot,index)=>{
      const characters=Array.isArray(shot.characters)?shot.characters.filter((value):value is string=>typeof value==='string'):[]
      const video=shot.status==='verified'&&typeof shot.job?.result_path==='string'?shot.job.result_path:null
      return <article key={shot.id||`shot-${index}`}>
        <b>{index+1}. {shot.scene_title||'Scene'} · {shot.title||'Untitled shot'}</b>
        <p>{shot.prompt||'No shot prompt was saved.'}</p>
        <small>{shot.framing||'Framing not specified'} · {characters.length?characters.join(', '):'No character roster specified'} · {shot.status==='verified'?'Technically verified':shot.status||'Status unavailable'}</small>
        {video&&<video className="film-clip-preview" controls playsInline preload="metadata" src={`/api/media/file?path=${encodeURIComponent(video)}`}/>}
      </article>
    })}</div>
    {!shots.length&&<div className="model-caution">No shot details were returned by the saved plan. Review the run directly from Create Film.</div>}
    <button className="button secondary" onClick={onOpenStoryboard}><Film size={13}/> Open generic storyboard</button>
  </section>
}
