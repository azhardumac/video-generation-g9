export type FilmRunReviewTarget = { projectId: string; runId: string; shotId: string }

export function filmRunReviewTarget(projectId: string, runId: string, shotId: string): FilmRunReviewTarget {
  return { projectId, runId, shotId }
}

export function activateFilmRunReview(projectId: string, runId: string, shotId: string,
  setTarget: (target: FilmRunReviewTarget) => void, navigate: (page: 'Clip review') => void) {
  setTarget(filmRunReviewTarget(projectId, runId, shotId))
  navigate('Clip review')
}
