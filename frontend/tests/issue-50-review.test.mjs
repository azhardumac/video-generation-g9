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
const { filmRunReviewTarget } = loadTs(path.join(frontendRoot, 'src/filmRunReviewNavigation.ts'))

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

test('error boundary fallback gives a safe recovery action', () => {
  const boundary = new ErrorBoundary({ children: null, onReset() {} })
  boundary.state = ErrorBoundary.getDerivedStateFromError(new Error('unsupported metadata'))
  const html = renderToStaticMarkup(boundary.render())
  assert.match(html, /THIS VIEW COULD NOT BE DISPLAYED/)
  assert.match(html, /Return to Create Film/)
  assert.match(html, /run history and media were not changed/)
  assert.doesNotMatch(html, /unsupported metadata/)
})
