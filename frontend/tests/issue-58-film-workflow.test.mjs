import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { createRequire, Module } from 'node:module'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const here=path.dirname(fileURLToPath(import.meta.url))
const frontendRoot=path.resolve(here,'..')
const require=createRequire(import.meta.url)
const ts=require('typescript')

function loadTs(sourcePath){
  const source=fs.readFileSync(sourcePath,'utf8')
  const compiled=ts.transpileModule(source,{fileName:sourcePath,compilerOptions:{
    jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true,
  }}).outputText
  const loaded=new Module(sourcePath);loaded.filename=sourcePath;loaded.paths=Module._nodeModulePaths(path.dirname(sourcePath));loaded._compile(compiled,sourcePath)
  return loaded.exports
}

const {resolveFilmRunNextAction}=loadTs(path.join(frontendRoot,'src/FilmRunExportPanel.tsx'))

// Mirrors the saved generic Gemini fox run: one current 49-frame job and a
// current human approval, but no legacy film-batch/reconciled_job_id linkage.
const approvedFoxRun={id:'run-gemini-fox',project_id:'project-fox',status:'needs_review',stage:'creative_review',revision:4,
  completed_stages:['understand_story','define_visual_bible','plan_scenes','generate_shots'],storyboard_id:'storyboard-fox',
  generation_progress:{total:1,verified:1,progress:100,shots:[{shot_id:'shot-fox',title:'Fox finds a lantern',verified:true}]}}
const approvedFoxExport={run_id:'run-gemini-fox',run_revision:4,storyboard_id:'storyboard-fox',revision:1,total_shots:1,
  verified_shots:1,all_verified:true,approved:true,creative_review_status:'approved',ready_to_prepare:true,
  preparation_blockers:[],manifest_ready:false,manifest:null,remotion_ready:true,latest_render:null,verified_final:false}

test('approved generic Gemini run chooses Prepare Film without legacy reconciliation',()=>{
  assert.equal(approvedFoxExport.reconciled,undefined)
  assert.equal(resolveFilmRunNextAction(approvedFoxRun,approvedFoxExport),'prepare')
})

test('Create Film next action progresses through render, durable progress, and final playback',()=>{
  const prepared={...approvedFoxExport,ready_to_prepare:false,reconciled:true,manifest_ready:true,
    manifest:{duration_in_frames:49,width:448,height:768,fps:24}}
  assert.equal(resolveFilmRunNextAction(approvedFoxRun,prepared),'render')
  assert.equal(resolveFilmRunNextAction(approvedFoxRun,{...prepared,latest_render:{id:'render-1',status:'running',progress:42}}),'rendering')
  assert.equal(resolveFilmRunNextAction(approvedFoxRun,{...prepared,latest_render:{id:'render-1',status:'completed',progress:100},verified_final:true}),'watch')
})

test('unapproved, stale, or unavailable exports do not offer Prepare or Render',()=>{
  assert.equal(resolveFilmRunNextAction(approvedFoxRun,{...approvedFoxExport,approved:false,ready_to_prepare:false,
    preparation_blockers:['Current creative approval is required for every verified shot.']}),'review')
  assert.equal(resolveFilmRunNextAction(approvedFoxRun,{...approvedFoxExport,ready_to_prepare:false,all_verified:false,
    preparation_blockers:['Fox finds a lantern']}),'review')
  const prepared={...approvedFoxExport,ready_to_prepare:false,reconciled:true,manifest_ready:true,manifest:{duration_in_frames:49,width:448,height:768,fps:24}}
  assert.equal(resolveFilmRunNextAction(approvedFoxRun,{...prepared,remotion_ready:false}),'blocked')
})
