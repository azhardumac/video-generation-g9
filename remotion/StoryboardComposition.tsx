import React from 'react';
import {
  AbsoluteFill,
  Composition,
  OffthreadVideo,
  Sequence,
  staticFile,
  useCurrentFrame,
  interpolate,
} from 'remotion';

type Shot = {
  shot_id: string;
  title: string;
  src: string;
  frames: number;
  start_frame: number;
  transition_in_frames: number;
  transition_out_frames: number;
  caption: string;
};
type Manifest = {
  title: string;
  width: number;
  height: number;
  fps: number;
  duration_in_frames: number;
  shots: Shot[];
};

const ShotClip: React.FC<{ shot: Shot }> = ({shot}) => {
  const frame = useCurrentFrame();
  const fadeIn = shot.transition_in_frames
    ? interpolate(frame, [0, shot.transition_in_frames], [0, 1], {extrapolateRight: 'clamp'})
    : 1;
  const fadeOut = shot.transition_out_frames
    ? interpolate(frame, [shot.frames - shot.transition_out_frames, shot.frames], [1, 0], {extrapolateLeft: 'clamp'})
    : 1;
  return <AbsoluteFill style={{opacity: Math.min(fadeIn, fadeOut), backgroundColor: '#05070b'}}>
    <OffthreadVideo src={staticFile(shot.src)} muted style={{width: '100%', height: '100%', objectFit: 'cover'}} />
    {shot.caption.trim() && <div style={{position: 'absolute', left: '7%', right: '7%', bottom: '8%', padding: '18px 22px',
      color: 'white', backgroundColor: 'rgba(5,8,14,.68)', borderRadius: 12, fontFamily: 'Arial, sans-serif',
      fontSize: 34, lineHeight: 1.25, textAlign: 'center', textShadow: '0 2px 8px #000'}}>{shot.caption}</div>}
  </AbsoluteFill>;
};

const StoryboardTimeline: React.FC<{manifest: Manifest}> = ({manifest}) => <AbsoluteFill style={{backgroundColor: '#05070b'}}>
  {manifest.shots.map((shot) => <Sequence key={shot.shot_id} from={shot.start_frame} durationInFrames={shot.frames}>
    <ShotClip shot={shot}/>
  </Sequence>)}
</AbsoluteFill>;

export const StoryboardComposition: React.FC<{manifest: Manifest}> = ({manifest}) => <StoryboardTimeline manifest={manifest}/>;

export const StoryboardRoot: React.FC = () => <Composition
  id="StoryboardAssembly"
  component={StoryboardComposition}
  durationInFrames={1}
  fps={24}
  width={448}
  height={768}
  defaultProps={{manifest: {title: 'Storyboard', width: 448, height: 768, fps: 24, duration_in_frames: 1, shots: []}}}
  calculateMetadata={({props}) => ({
    durationInFrames: props.manifest.duration_in_frames,
    fps: props.manifest.fps,
    width: props.manifest.width,
    height: props.manifest.height,
  })}
/>;
