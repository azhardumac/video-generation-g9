import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle, ArrowLeft } from 'lucide-react'

type Props = { children:ReactNode; onReset:()=>void }
type State = { failed:boolean; errorId:string|null }

export default class ErrorBoundary extends Component<Props,State>{
  state:State={failed:false,errorId:null}
  static getDerivedStateFromError():State{return {failed:true,errorId:null}}
  componentDidCatch(error:Error,info:ErrorInfo){
    const errorId=`VIEW-${Date.now().toString(36).toUpperCase()}-${Math.random().toString(36).slice(2,7).toUpperCase()}`
    const stackFrames=(error.stack??'').split('\n').slice(1).join('\n')
    // Log stack frames for local debugging without logging the exception message,
    // which could contain user-authored prompt or path data.
    console.error(`FRAMEWORK view error ${errorId}\n${stackFrames}\n${info.componentStack??''}`)
    this.setState({errorId})
  }
  render(){
    if(this.state.failed)return <section className="panel app-error-boundary" role="alert" aria-live="assertive"><AlertTriangle size={17}/><div><div className="section-kicker">THIS VIEW COULD NOT BE DISPLAYED</div><h2>FRAMEWORK kept the rest of the studio available</h2><p>The saved project response is incomplete or uses fields this view does not support. Your run history and media were not changed. Return to Create Film or open another section.</p>{this.state.errorId&&<small>Diagnostic ID: <code>{this.state.errorId}</code> · local stack details are in the developer console.</small>}<button className="button secondary" onClick={this.props.onReset}><ArrowLeft size={13}/> Return to Create Film</button></div></section>
    return this.props.children
  }
}
