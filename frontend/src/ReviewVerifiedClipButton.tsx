import { ShieldCheck } from 'lucide-react'

export default function ReviewVerifiedClipButton({runId,shotId,onReview,label='Review verified clips'}:{runId:string;shotId:string;onReview:(runId:string,shotId:string)=>void;label?:string}){
  return <button type="button" className="button primary mini" data-review-run-id={runId} data-review-shot-id={shotId}
    onClick={()=>onReview(runId,shotId)}><ShieldCheck size={14}/>{label}</button>
}
