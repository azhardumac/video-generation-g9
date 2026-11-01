import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { createRequire, Module } from 'node:module'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const here = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(here, '..')
const require = createRequire(import.meta.url)
const ts = require('typescript')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')

Module._extensions['.tsx'] = (module, sourcePath) => {
  const source = fs.readFileSync(sourcePath, 'utf8')
  const compiled = ts.transpileModule(source, { fileName: sourcePath, compilerOptions: {
    jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
    esModuleInterop: true,
  } }).outputText
  module._compile(compiled, sourcePath)
}

function loadTs(sourcePath) {
  const source = fs.readFileSync(sourcePath, 'utf8')
  const compiled = ts.transpileModule(source, { fileName: sourcePath, compilerOptions: {
    jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
    esModuleInterop: true,
  } }).outputText
  const loaded = new Module(sourcePath)
  loaded.filename = sourcePath
  loaded.paths = Module._nodeModulePaths(path.dirname(sourcePath))
  loaded._compile(compiled, sourcePath)
  return loaded.exports
}

const { default: GenericRunPlanSummary } = loadTs(path.join(frontendRoot, 'src/GenericRunPlanSummary.tsx'))
const { FilmRunReviewView } = loadTs(path.join(frontendRoot, 'src/FilmRunReviewPage.tsx'))
const { default: ErrorBoundary } = loadTs(path.join(frontendRoot, 'src/ErrorBoundary.tsx'))
const { filmRunReviewTarget, activateFilmRunReview } = loadTs(path.join(frontendRoot, 'src/filmRunReviewNavigation.ts'))
const { storyToFilmDisplayMode } = loadTs(path.join(frontendRoot, 'src/filmPlanCompatibility.ts'))
const { default: ReviewVerifiedClipButton } = loadTs(path.join(frontendRoot, 'src/ReviewVerifiedClipButton.tsx'))
const { default: FilmPlanSummary } = loadTs(path.join(frontendRoot, 'src/GenericRunPlanSummary.tsx'))
const { default: FilmRunReviewAction } = loadTs(path.join(frontendRoot, 'src/FilmRunReviewAction.tsx'))

// Mirrors GET /api/storyboards/{storyboard_id} after FilmRunEngine persistence:
// planner_mode belongs to film_plans; generated metadata contains the normalized
// story plan and does not carry the legacy fable scenes/preflight fields.
const geminiStoryboardApiResponse = {
  id: 'storyboard-fox-api', project_id: 'project-fox', title: 'Fox and the Lantern', revision: 1,
  story_text: 'A fox finds a lantern and carries it home.', style_bible_revision: 0, style_bible: {},
  scenes: [{ id: 'scene-api-1', title: 'Dawn by the cottage', position: 0 }], shot_count: 1,
  film_plan: { storyboard_id: 'storyboard-fox-api', project_id: 'project-fox',
    planner_mode: 'local-structured-v1', film_run_id: 'run-fox-api', approved_revision: 1,
    approved_style_revision: 0, approved: true,
    metadata: { story_plan: { title: 'Fox and the Lantern', characters: [{id:'fox',name:'Fox'}], scenes: [] },
      planner_disclosure: 'Structured plan validated by FRAMEWORK.', scene_count: 1, shot_count: 1,
      preset: 'Short', frames_per_shot: 49, fps: 24, width: 448, height: 768,
      approved_reference_ids: [], creative_quality_status: 'pending_manual_review' } },
  shots: [{ id: 'shot-fox-api', scene_title: 'Dawn by the cottage', title: 'Finds the lantern',
    prompt: 'A small red fox finds a brass lantern in the dawn grass.', framing: 'Medium close-up',
    characters: ['Fox'], reference_ids: [], status: 'verified',
    job: { id: 'job-fox-api', status: 'completed', exit_code: 0, result_path: '/outputs/fox.mp4',
      verification_metadata: {verified_output:{width:448,height:768,fps:24,frames:49}} } }],
}

const verifiedGeminiPlan = {
  id: 'storyboard-gemini-1', project_id: 'project-fox', title: 'Sunrise on the Hill', revision: 1,
  story_text: 'A fox finds a lantern and carries it home.', style_bible_revision: 0, style_bible: {},
  film_plan: { approved: true, metadata: { planner_mode: 'gemini', planner_provenance: { provider: 'gemini' },
    story_plan: { title: 'Sunrise on the Hill' } } },
  shots: [{ id: 'shot-gemini-1', scene_title: 'Dawn by the cottage', title: 'Finds the lantern',
    prompt: 'A small red fox finds a brass lantern in the dawn grass.', framing: 'Medium close-up',
    characters: ['Fox'], status: 'verified', job: { result_path: '/tmp/fox first shot.mp4' } }],
}

test('generic Gemini storyboard renders without legacy fable metadata', () => {
  assert.equal(verifiedGeminiPlan.film_plan.metadata.scenes, undefined)
  assert.equal(verifiedGeminiPlan.film_plan.metadata.expected_workload, undefined)
  assert.throws(() => verifiedGeminiPlan.film_plan.metadata.scenes.length, TypeError)
  const html = renderToStaticMarkup(React.createElement(GenericRunPlanSummary, {
    plan: verifiedGeminiPlan, onOpenStoryboard() {},
  }))
  assert.match(html, /Sunrise on the Hill/)
  assert.match(html, /Finds the lantern/)
  assert.match(html, /A small red fox finds a brass lantern/)
  assert.match(html, /api\/media\/file\?path=%2Ftmp%2Ffox%20first%20shot.mp4/)
  assert.doesNotMatch(html, /frog|scorpion|river|expected_workload/)
})

test('generic clip review renders verified video, safe empty-reference state, and approval controls', () => {
  const data = { run_id: 'run-fox-1', project_id: 'project-fox', run_revision: 8, status: 'needs_review',
    storyboard_id: 'storyboard-gemini-1', storyboard_revision: 1, title: 'Sunrise on the Hill',
    creative_review_status: 'pending', shots: [{ id: 'shot-gemini-1', title: 'Finds the lantern',
      scene_title: 'Dawn by the cottage', prompt: 'A small red fox finds a brass lantern in the dawn grass.',
      framing: 'Medium close-up', characters: ['Fox'], expected_character_count: 1,
      reference_ids: [], references: [], verified: true,
      job: { id: 'job-fox-1', status: 'completed', exit_code: 0, result_path: '/tmp/fox first shot.mp4',
        verification_metadata: { verified_output: { width: 448, height: 768, fps: 24, frames: 49 } } },
      creative_review: null, creative_review_current: false }] }
  const html = renderToStaticMarkup(React.createElement(FilmRunReviewView, {
    data, shotId: 'shot-gemini-1', notes: '', onNotesChange() {}, busy: false, error: '',
    onBack() {}, onSelectShot() {}, onReview() {}, onContinueExport() {},
    onRefreshVerification() {},
  }))
  assert.match(html, /api\/media\/file\?path=%2Ftmp%2Ffox%20first%20shot.mp4/)
  assert.match(html, /No available reference images are attached/)
  assert.match(html, /SHOT PROMPT/)
  assert.match(html, /CHARACTERS · EXPECTED 1/)
  assert.match(html, /Approve clip/)
  assert.match(html, /Reject clip/)
  assert.match(html, /separate from ffprobe/i)
})

test('Create Film navigation preserves exact project, run, and shot identifiers', () => {
  assert.deepEqual(filmRunReviewTarget('project-fox', 'run-fox-1', 'shot-gemini-1'), {
    projectId: 'project-fox', runId: 'run-fox-1', shotId: 'shot-gemini-1',
  })
})

test('Story to Film selects real generic Gemini storyboard API data without entering the legacy renderer', () => {
  assert.equal(geminiStoryboardApiResponse.film_plan.metadata.scenes, undefined)
  assert.equal(geminiStoryboardApiResponse.film_plan.metadata.preflight_warnings, undefined)
  function legacyFilmPlanView({plan}) {
    return React.createElement('div', null, `Scenes: ${plan.film_plan.metadata.scenes.length}`)
  }
  assert.throws(() => renderToStaticMarkup(React.createElement(legacyFilmPlanView, {plan:geminiStoryboardApiResponse})), TypeError)
  assert.equal(storyToFilmDisplayMode(geminiStoryboardApiResponse, null), 'generic')
  const html = renderToStaticMarkup(React.createElement(FilmPlanSummary, {
    plan: geminiStoryboardApiResponse, onOpenStoryboard() {}, onReview() {},
  }))
  assert.match(html, /Fox and the Lantern/)
  assert.match(html, /Review verified clips/)
  assert.match(html, /data-review-run-id="run-fox-api"/)
  assert.match(html, /data-review-shot-id="shot-fox-api"/)
  assert.doesNotMatch(html, /Frog character reference|Scorpion character reference|expected_workload/)
})

test('Create Film Review verified clips CTA opens the dedicated route with its exact run and shot IDs', () => {
  const opened = []
  let route = ''
  const element = ReviewVerifiedClipButton({runId:'run-create-film-82',shotId:'shot-create-film-19',
    onReview:(runId,shotId)=>activateFilmRunReview('project-create-film',runId,shotId,target=>opened.push(target),page=>{route=page})})
  assert.equal(element.props.children.at(-1), 'Review verified clips')
  element.props.onClick()
  assert.equal(route,'Clip review')
  assert.deepEqual(opened, [{projectId:'project-create-film',runId:'run-create-film-82',shotId:'shot-create-film-19'}])
})

test('Story to Film review CTA routes its verified Gemini shot to Clip Review directly', () => {
  const opened = []
  let route = ''
  const html=renderToStaticMarkup(React.createElement(FilmPlanSummary,{plan:geminiStoryboardApiResponse,
    onOpenStoryboard(){},onReview:(runId,shotId)=>activateFilmRunReview(geminiStoryboardApiResponse.project_id,runId,shotId,target=>opened.push(target),page=>{route=page})}))
  assert.match(html,/data-review-run-id="run-fox-api"/)
  assert.match(html,/data-review-shot-id="shot-fox-api"/)
  const button=ReviewVerifiedClipButton({runId:geminiStoryboardApiResponse.film_plan.film_run_id,
    shotId:geminiStoryboardApiResponse.shots[0].id,
    onReview:(runId,shotId)=>activateFilmRunReview(geminiStoryboardApiResponse.project_id,runId,shotId,target=>opened.push(target),page=>{route=page})})
  button.props.onClick()
  assert.equal(route,'Clip review')
  assert.deepEqual(opened, [{projectId:'project-fox',runId:'run-fox-api',shotId:'shot-fox-api'}])
})

test('Film Runs keep Open clip review visible and explain an unreviewable clip instead of hiding the action', () => {
  const opened=[]
  let route=''
  const shots=[{shot_id:'shot-missing-job',status:'completed',verified:false,job_id:null,
    reviewability_reason:'The job has no recorded ffprobe-verified video metadata.'}]
  const markup=renderToStaticMarkup(React.createElement(FilmRunReviewAction,{runId:'run-missing-job',shots,
    onReview:(runId,shotId)=>activateFilmRunReview('project-fox',runId,shotId,target=>opened.push(target),page=>{route=page})}))
  assert.match(markup,/Open clip review/)
  assert.match(markup,/The job has no recorded ffprobe-verified video metadata/)
  assert.match(markup,/data-review-run-id="run-missing-job"/)
  assert.match(markup,/data-review-shot-id="shot-missing-job"/)
  const element=ReviewVerifiedClipButton({runId:'run-missing-job',shotId:shots[0].shot_id,
    onReview:(runId,shotId)=>activateFilmRunReview('project-fox',runId,shotId,target=>opened.push(target),page=>{route=page}),label:'Open clip review'})
  element.props.onClick()
  assert.equal(route,'Clip review')
  assert.deepEqual(opened,[{projectId:'project-fox',runId:'run-missing-job',shotId:'shot-missing-job'}])
})

test('1/1 completed verified run renders Review verified clips with the authoritative shot ID', () => {
  const opened=[]
  const shots=[{shot_id:'shot-fox-verified',status:'verified',verified:true,job_id:'job-fox-verified',reviewability_reason:null}]
  const element=FilmRunReviewAction({runId:'run-fox-completed',shots,
    onReview:(runId,shotId)=>opened.push(filmRunReviewTarget('project-fox',runId,shotId))})
  const markup=renderToStaticMarkup(element)
  assert.match(markup,/Review verified clips/)
  assert.match(markup,/data-review-run-id="run-fox-completed"/)
  assert.match(markup,/data-review-shot-id="shot-fox-verified"/)
  const button=ReviewVerifiedClipButton({runId:'run-fox-completed',shotId:shots[0].shot_id,
    onReview:(runId,shotId)=>opened.push(filmRunReviewTarget('project-fox',runId,shotId))})
  button.props.onClick()
  assert.deepEqual(opened,[{projectId:'project-fox',runId:'run-fox-completed',shotId:'shot-fox-verified'}])
})

test('Clip Review shows the backend blocker and a refresh-verification action for missing media', () => {
  const data={run_id:'run-missing-media',project_id:'project-fox',run_revision:3,status:'needs_review',
    storyboard_id:'storyboard-missing-media',storyboard_revision:1,title:'Fox film',creative_review_status:'pending',
    shots:[{id:'shot-missing-media',title:'Lantern',scene_title:'Dawn',prompt:'Fox finds a lantern.',framing:'Close-up',
      characters:['Fox'],expected_character_count:1,reference_ids:[],references:[],verified:false,job_id:'job-missing-media',
      reviewability_reason:'The saved video is missing or outside the configured local output roots.',job:null,
      creative_review:null,creative_review_current:false}]}
  const html=renderToStaticMarkup(React.createElement(FilmRunReviewView,{data,shotId:'shot-missing-media',notes:'',
    onNotesChange(){},busy:false,error:'',onBack(){},onSelectShot(){},onReview(){},onContinueExport(){},onRefreshVerification(){}}))
  assert.match(html,/The saved video is missing or outside the configured local output roots/)
  assert.match(html,/Refresh verification/)
  assert.match(html,/button class="button primary" disabled=""[\s\S]*?Approve clip/)
})

test('error boundary fallback gives a safe recovery action', () => {
  const boundary = new ErrorBoundary({ children: null, onReset() {} })
  boundary.state = ErrorBoundary.getDerivedStateFromError(new Error('unsupported metadata'))
  const originalError = console.error
  let diagnostic = ''
  console.error = (...values)=>{diagnostic=values.join(' ')}
  boundary.setState = update=>{boundary.state={...boundary.state,...(typeof update==='function'?update(boundary.state):update)}}
  boundary.componentDidCatch(new Error('private prompt text'), {componentStack:'\n    at FilmPlanPage'})
  console.error = originalError
  const html = renderToStaticMarkup(boundary.render())
  assert.match(html, /THIS VIEW COULD NOT BE DISPLAYED/)
  assert.match(html, /Return to Create Film/)
  assert.match(html, /run history and media were not changed/)
  assert.match(html, /Diagnostic ID: <code>VIEW-/)
  assert.match(diagnostic, /at FilmPlanPage/)
  assert.doesNotMatch(diagnostic, /private prompt text/)
  assert.doesNotMatch(html, /unsupported metadata|private prompt text/)
})
