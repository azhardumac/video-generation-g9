export type StoryboardPlanShape = {
  film_plan?: {
    planner_mode?: string | null
    metadata?: Record<string, unknown> | null
  } | null
} | null

const LEGACY_FABLE_PLANNER = 'deterministic-approved-fable'

export function isCompleteLegacyFablePlan(plan: StoryboardPlanShape): boolean {
  if (plan?.film_plan?.planner_mode !== LEGACY_FABLE_PLANNER) return false
  const metadata = plan.film_plan.metadata
  if (!metadata || typeof metadata !== 'object') return false
  const scenes = metadata.scenes
  if (!Array.isArray(scenes) || !scenes.every((scene) => {
    if (!scene || typeof scene !== 'object') return false
    const item = scene as Record<string, unknown>
    return typeof item.title === 'string' && typeof item.visual_description === 'string' &&
      Number.isFinite(item.estimated_seconds) && typeof item.transition_after === 'string'
  })) return false
  return Number.isFinite(metadata.shot_count) && Number.isFinite(metadata.estimated_seconds) &&
    typeof metadata.expected_workload === 'string' &&
    (metadata.benchmark_estimate === null || Boolean(metadata.benchmark_estimate && typeof metadata.benchmark_estimate === 'object')) &&
    typeof metadata.experimental_drift_warning === 'boolean' && typeof metadata.reference_conditioning === 'string' &&
    typeof metadata.reference_limit_note === 'string' && typeof metadata.planner_disclosure === 'string' &&
    typeof metadata.style_bible_notice === 'string' && Array.isArray(metadata.preflight_warnings) &&
    typeof metadata.target_format === 'string' && typeof metadata.preset === 'string' &&
    Number.isFinite(metadata.frames_per_shot) && Number.isFinite(metadata.fps) &&
    Number.isFinite(metadata.width) && Number.isFinite(metadata.height) &&
    typeof metadata.ltx_ready_at_plan_time === 'boolean' && typeof metadata.audio_available === 'boolean' &&
    Array.isArray(metadata.approved_reference_ids)
}

export function storyToFilmDisplayMode(plan: StoryboardPlanShape, initialStoryboardId: string | null): 'loading' | 'builder' | 'legacy' | 'generic' {
  if (!plan) return initialStoryboardId ? 'loading' : 'builder'
  if (isCompleteLegacyFablePlan(plan)) return 'legacy'
  // Only the explicitly marked and structurally complete legacy plan is allowed
  // into the fable-specific renderer. Unknown and partial plans render generically.
  return 'generic'
}
