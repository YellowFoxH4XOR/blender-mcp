import {
  AbsoluteFill,
  CalculateMetadataFunction,
  Composition,
  Img,
  staticFile,
  useCurrentFrame,
} from "remotion";

export type BlenderSequenceProps = {
  framePaths: string[];
  fps: number;
  width: number;
  height: number;
};

const calculateMetadata: CalculateMetadataFunction<BlenderSequenceProps> = ({
  props,
}) => {
  if (props.framePaths.length === 0) {
    throw new Error("framePaths must contain at least one Blender frame");
  }
  if (props.fps <= 0 || props.width <= 0 || props.height <= 0) {
    throw new Error("fps, width, and height must be positive");
  }
  return {
    durationInFrames: props.framePaths.length,
    fps: props.fps,
    width: props.width,
    height: props.height,
  };
};

export const MyComposition = () => {
  return (
    <Composition
      id="BlenderSequence"
      component={BlenderFrameSequence}
      durationInFrames={1}
      fps={30}
      width={1920}
      height={1080}
      defaultProps={{
        framePaths: ["example/frame_0001.png"],
        fps: 30,
        width: 1920,
        height: 1080,
      }}
      calculateMetadata={calculateMetadata}
    />
  );
};

export const BlenderFrameSequence: React.FC<BlenderSequenceProps> = ({
  framePaths,
}) => {
  const frame = useCurrentFrame();
  const source = framePaths[Math.min(frame, framePaths.length - 1)];

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      <Img
        src={staticFile(source)}
        style={{ width: "100%", height: "100%", objectFit: "contain" }}
      />
    </AbsoluteFill>
  );
};
