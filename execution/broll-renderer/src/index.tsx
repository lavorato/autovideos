import { Composition, registerRoot } from "remotion";
import { BrollComposition, type BrollSegment } from "./BrollComposition";

/**
 * Props for the BrollComposition. The entire payload — including composition
 * dimensions/fps/duration — is passed via the Remotion CLI `--props` flag
 * from the Python side and applied by `calculateMetadata` below.
 *
 * We cannot read this config from disk in `index.tsx` because `registerRoot`
 * is evaluated both in Node (when Remotion scans compositions) AND inside the
 * Chrome headless renderer (where `require("fs")` does not exist). Using the
 * idiomatic `--props` flow guarantees the segments array survives both
 * environments and makes it to the React component.
 */
type BrollProps = {
  fps: number;
  width: number;
  height: number;
  durationInFrames: number;
  segments: BrollSegment[];
  mainVideoSrc: string;
};

const defaultProps: BrollProps = {
  fps: 30,
  width: 1080,
  height: 1920,
  durationInFrames: 90,
  segments: [],
  mainVideoSrc: "",
};

const RemotionRoot = () => {
  return (
    <Composition
      id="BrollComposition"
      component={BrollComposition}
      fps={defaultProps.fps}
      width={defaultProps.width}
      height={defaultProps.height}
      durationInFrames={defaultProps.durationInFrames}
      defaultProps={defaultProps}
      calculateMetadata={({ props }) => {
        return {
          fps: props.fps || defaultProps.fps,
          width: props.width || defaultProps.width,
          height: props.height || defaultProps.height,
          durationInFrames:
            props.durationInFrames || defaultProps.durationInFrames,
        };
      }}
    />
  );
};

registerRoot(RemotionRoot);
