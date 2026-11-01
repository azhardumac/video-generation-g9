import { ShieldCheck } from 'lucide-react'
import ReviewVerifiedClipButton from './ReviewVerifiedClipButton'

export type ReviewReadinessShot = {
  shot_id: string
  verified?: boolean
  job_id?: string | null
  reviewability_reason?: string | null
}

export default function FilmRunReviewAction({runId,shots,onReview}:{
  runId:string;shots:ReviewReadinessShot[];onReview:(runId:string,shotId:string)=>void
}){
  const reviewable=shots.find(shot=>shot.verified&&shot.job_id)
  const fallback=shots.find(shot=>shot.shot_id)
  const shotId=reviewable?.shot_id??fallback?.shot_id??''
  return <div className="create-film-limitation"><ShieldCheck size={14}/><div>
    <span>Technically verified clips are awaiting human creative review. Reviews open directly for this run and shot; no automatic visual approval or Remotion render occurs.</span>
    <ReviewVerifiedClipButton runId={runId} shotId={shotId} onReview={onReview} label={reviewable?'Review verified clips':'Open clip review'}/>
    {!reviewable&&<small>{fallback?.reviewability_reason??'No shot details are available yet; open clip review to inspect the current blocker.'}</small>}
  </div></div>
}
