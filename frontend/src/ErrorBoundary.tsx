import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle, ArrowLeft } from 'lucide-react'

type Props = { children:ReactNode; onReset:()=>void }
type State = { failed:boolean }

export default class ErrorBoundary extends Component<Props,State>{
  state:State={failed:false}
  static getDerivedStateFromError():State{return {failed:true}}
  componentDidCatch(_error:Error,_info:ErrorInfo){
    // Keep runtime diagnostics in the visible fallback instead of letting an
    // unsupported project response unmount the complete studio shell.
  }
  render(){
    if(this.state.failed)return <section className="panel app-error-boundary" role="alert" aria-live="assertive"><AlertTriangle size={17}/><div><div className="section-kicker">THIS VIEW COULD NOT BE DISPLAYED</div><h2>FRAMEWORK kept the rest of the studio available</h2><p>The saved project response is incomplete or uses fields this view does not support. Your run history and media were not changed. Return to Create Film or open another section.</p><button className="button secondary" onClick={this.props.onReset}><ArrowLeft size={13}/> Return to Create Film</button></div></section>
    return this.props.children
  }
}
