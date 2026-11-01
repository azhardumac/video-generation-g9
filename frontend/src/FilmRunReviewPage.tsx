import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, ArrowLeft, Check, Clapperboard, LoaderCircle, ShieldCheck, X } from 'lucide-react'
import type { FilmRunReviewTarget } from './filmRunReviewNavigation'

type ReviewShot = {
  id: string; title: string; scene_title: string; prompt: string; visual_description?: string; framing: string
  characters: string[]; expected_character_count: number; reference_ids: string[]
  references: {id:string;name:string;category:string;mime_type:string;available:boolean}[]
  verified: boolean; playable:boolean; settings_mismatches?:string[]; job_id:string|null; reviewability_reason:string|null; job: {id:string;status:string;exit_code:number|null;result_path:string;verification_metadata:{verified_output:{width:number;height:number;fps:number;frames:number}}}|null
  creative_review: {id:string;decision:'approved'|'rejected';notes:string;created_at:string}|null; creative_review_current:boolean
}
type ReviewData = {run_id:string;project_id:string;run_revision:number;status:string;storyboard_id:string;storyboard_revision:number;title:string;creative_review_status:'pending'|'approved'|'rejected';shots:ReviewShot[]}
type ReviewDecision = {expected_run_revision:number;expected_storyboard_revision:number;job_id:string;decision:'approved'|'rejected';notes:string}

async function requestJson<T>(url:string,init?:RequestInit):Promise<T>{
  const response=await fetch(url,init)
  const data=await response.json().catch(()=>({detail:'The local API returned an unreadable response'}))
  if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'The local API could not complete the review action')
  return data as T
}

export function FilmRunReviewView({data,shotId,notes,onNotesChange,busy,error,onBack,onSelectShot,onReview,onContinueExport,onRefreshVerification}:{
  data:ReviewData;shotId:string;notes:string;onNotesChange:(value:string)=>void;busy:boolean;error:string
  onBack:()=>void;onSelectShot:(shotId:string)=>void;onReview:(decision:'approved'|'rejected')=>void
  onContinueExport:(storyboardId:string)=>void;onRefreshVerification:(shotId:string)=>void
}){
  const shot=data.shots.find(item=>item.id===shotId)
  const verifiedShots=data.shots.filter(item=>item.playable&&item.job)
  const nextShot=verifiedShots.find(item=>item.id!==shotId&&(!item.creative_review_current||item.creative_review?.decision!=='approved'))
  const verified=Boolean(shot?.verified&&shot.job?.result_path)
  const playable=Boolean(shot?.playable&&shot.job?.result_path)
  if(!shot)return <section className="panel workspace-page run-review-page"><div className="model-caution"><AlertTriangle size={14}/> This shot is no longer available in the selected film run.</div><button className="button secondary" onClick={onBack}><ArrowLeft size={13}/> Return to Create Film</button></section>
  return <section className="panel workspace-page run-review-page" aria-labelledby="run-review-title">
    <div className="run-review-top"><div><div className="section-kicker">FILM RUN · HUMAN CREATIVE REVIEW</div><h2 id="run-review-title">{shot.scene_title} · {shot.title}</h2><p>{data.title}</p></div><button className="button secondary mini" onClick={onBack}><ArrowLeft size={13}/> Create Film</button></div>
    <div className={`run-review-technical${playable&&!verified?' preflight-warning':''}`}><ShieldCheck size={15}/><span>{playable?`Technically verified · ${shot.job!.verification_metadata.verified_output.width} × ${shot.job!.verification_metadata.verified_output.height} · ${shot.job!.verification_metadata.verified_output.fps} fps · ${shot.job!.verification_metadata.verified_output.frames} frames${verified?' · current for creative review':'. Playback is available, but approval is blocked until the saved shot settings match.'}`:shot.reviewability_reason||'This clip is no longer technically verified; review actions are disabled.'}{playable&&!verified&&(shot.settings_mismatches??[]).length>0&&<> Mismatched fields: <code>{shot.settings_mismatches!.join(', ')}</code>.</>}</span>{!verified&&<button className="button secondary mini" disabled={busy} onClick={()=>onRefreshVerification(shot.id)}><LoaderCircle size={12} className={busy?'spin':''}/> Refresh verification</button>}</div>
    {shot.reviewability_reason&&playable&&!verified&&<div className="model-caution"><AlertTriangle size={13}/><span>{shot.reviewability_reason}</span></div>}
    {playable&&<div className="run-review-player"><video aria-label={`Verified clip: ${shot.title}`} controls playsInline preload="metadata" src={`/api/media/file?path=${encodeURIComponent(shot.job!.result_path)}`}/></div>}
    <div className="run-review-shot-details">
      <article><span>SHOT PROMPT</span><p>{shot.prompt||'No prompt was saved for this shot.'}</p></article>
      <article><span>FRAMING</span><p>{shot.framing||'Not specified'}</p></article>
      <article><span>CHARACTERS · EXPECTED {shot.expected_character_count}</span><p>{shot.characters.length?shot.characters.join(', '):'No character roster was specified for this shot.'}</p></article>
      {shot.visual_description&&<article><span>SCENE BEAT</span><p>{shot.visual_description}</p></article>}
    </div>
    <div className="run-review-references"><div><div className="section-kicker">SHOT REFERENCES</div><p>Only references attached to this shot are shown.</p></div>{shot.references.length?shot.references.map(reference=><figure key={reference.id}><img src={`/api/projects/${data.project_id}/references/${encodeURIComponent(reference.id)}/file`} alt={`${reference.name} reference`}/><figcaption>{reference.name} · {reference.category}</figcaption></figure>):<div className="run-review-no-reference">No available reference images are attached to this shot.</div>}</div>
    {shot.creative_review&&<div className={`film-style-notice ${shot.creative_review_current?'':'preflight-warning'}`}><Check size={14}/><span>{shot.creative_review_current?'Latest decision':'A prior decision is stale'}: {shot.creative_review.decision}{shot.creative_review.notes?` · ${shot.creative_review.notes}`:''} · {new Date(shot.creative_review.created_at).toLocaleString()}</span></div>}
    <label className="setting-field wide run-review-notes"><span>REVIEW NOTES</span><textarea maxLength={2000} value={notes} onChange={event=>onNotesChange(event.target.value)} placeholder="Record what works or what should change. Rejection requires notes."/></label>
    {error&&<div className="model-caution"><AlertTriangle size={13}/><span>{error}</span></div>}
    <div className="film-action-row run-review-actions"><button className="button secondary" disabled={!verified||busy||!notes.trim()} onClick={()=>onReview('rejected')}><X size={13}/>{busy?'Saving…':'Reject clip'}</button><button className="button primary" disabled={!verified||busy} onClick={()=>onReview('approved')}><Check size={13}/>{busy?'Saving…':'Approve clip'}</button></div>
    {shot.creative_review_current&&shot.creative_review?.decision==='approved'&&<div className="run-review-next"><div><div className="section-kicker">NEXT FILMMAKING ACTION</div><b>{nextShot?'Continue creative review':'All technically verified clips have creative approval'}</b><p>{nextShot?'Choose the next verified clip to review. No new footage is generated.':data.creative_review_status==='approved'?'Continue in Create Film to prepare the existing clips. Nothing renders until you authorize it separately.':'Review status was refreshed; continue when ready.'}</p></div>{nextShot?<button className="button secondary" onClick={()=>onSelectShot(nextShot.id)}>Review next clip</button>:data.creative_review_status==='approved'?<button className="button primary" onClick={()=>onContinueExport(data.storyboard_id)}><Clapperboard size={13}/> Continue in Create Film</button>:null}</div>}
    {shot.creative_review_current&&shot.creative_review?.decision==='rejected'&&<div className="model-caution"><X size={13}/><span>Rejection saved. The verified clip, job, and history are preserved. No generation or render was started.</span></div>}
    {verifiedShots.length>1&&<nav className="run-review-shot-nav" aria-label="Verified clips">{verifiedShots.map((item,index)=><button key={item.id} className={item.id===shot.id?'selected':''} onClick={()=>onSelectShot(item.id)}>{index+1}. {item.title} · {item.creative_review_current?item.creative_review?.decision:'needs review'}</button>)}</nav>}
    <small className="run-review-disclosure">Creative review is a human decision separate from ffprobe verification. FRAMEWORK does not infer visual quality. Approving or rejecting this clip does not start LTX or Remotion.</small>
  </section>
}

export default function FilmRunReviewPage({target,onBack,onContinueExport}:{target:FilmRunReviewTarget|null;onBack:()=>void;onContinueExport:(storyboardId:string)=>void}){
  const [data,setData]=useState<ReviewData|null>(null);const [shotId,setShotId]=useState(target?.shotId??'');const [notes,setNotes]=useState('');const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [loading,setLoading]=useState(true)
  const url=useMemo(()=>target?`/api/projects/${encodeURIComponent(target.projectId)}/film-runs/${encodeURIComponent(target.runId)}/review`:null,[target?.projectId,target?.runId])
  const load=useCallback(async()=>{if(!url){setData(null);setError('Select a project and film run to review.');setLoading(false);return}setLoading(true);setError('');try{const value=await requestJson<ReviewData>(url);setData(value);setShotId(current=>value.shots.some(item=>item.id===current)?current:(value.shots.find(item=>item.playable)?.id??value.shots[0]?.id??''))}catch(e){setError(e instanceof Error?e.message:'Could not load the verified clip for review')}finally{setLoading(false)}},[url])
  const refreshEligibility=async(selectedShotId:string)=>{if(!target||!url)return;setBusy(true);setError('');try{const checkUrl=`${url}/shots/${encodeURIComponent(selectedShotId)}/eligibility-check`;const value=await requestJson<ReviewData>(checkUrl,{method:'POST'});setData(value);setShotId(selectedShotId)}catch(e){setError(e instanceof Error?e.message:'Could not recheck this clip’s saved output and settings')}finally{setBusy(false)}}
  useEffect(()=>{setShotId(target?.shotId??'');void load()},[load,target?.shotId])
  const shot=data?.shots.find(item=>item.id===shotId)
  useEffect(()=>{setNotes(shot?.creative_review?.notes??'')},[shot?.id,shot?.creative_review?.id])
  const submit=async(decision:'approved'|'rejected')=>{if(!target||!data||!shot?.job||!shot.verified)return;if(decision==='rejected'&&!notes.trim()){setError('Add review notes before rejecting this clip.');return}setBusy(true);setError('');try{const payload:ReviewDecision={expected_run_revision:data.run_revision,expected_storyboard_revision:data.storyboard_revision,job_id:shot.job.id,decision,notes};await requestJson(`/api/projects/${encodeURIComponent(target.projectId)}/film-runs/${encodeURIComponent(target.runId)}/shots/${encodeURIComponent(shot.id)}/review`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});await load()}catch(e){setError(e instanceof Error?e.message:'Could not save the creative review')}finally{setBusy(false)}}
  if(loading)return <section className="panel workspace-page run-review-page" role="status"><LoaderCircle className="spin" size={16}/> Loading the selected run and verified shot…</section>
  if(error&&!data)return <section className="panel workspace-page run-review-page"><div className="model-caution"><AlertTriangle size={14}/>{error}</div><button className="button secondary" onClick={onBack}><ArrowLeft size={13}/> Return to Create Film</button></section>
  if(!data)return <section className="panel workspace-page run-review-page"><div className="empty-workspace">No film run was selected for review.</div><button className="button secondary" onClick={onBack}>Return to Create Film</button></section>
  return <FilmRunReviewView data={data} shotId={shotId} notes={notes} onNotesChange={setNotes} busy={busy} error={error} onBack={onBack} onSelectShot={setShotId} onReview={decision=>void submit(decision)} onContinueExport={onContinueExport} onRefreshVerification={shot=>void refreshEligibility(shot)}/>
}
