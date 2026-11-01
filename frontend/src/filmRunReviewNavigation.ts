export type FilmRunReviewTarget = { projectId: string; runId: string; shotId: string }

export function filmRunReviewTarget(projectId: string, runId: string, shotId: string): FilmRunReviewTarget {
  return { projectId, runId, shotId }
}
