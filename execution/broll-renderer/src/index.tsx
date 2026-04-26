import { Composition, registerRoot } from "remotion";
import { BrollComposition, type BrollSegment } from "./BrollComposition";
import {
  CaptionsComposition,
  type Caption,
  type FaceBandNorm,
} from "./CaptionsComposition";

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

type CaptionsProps = {
  fps: number;
  width: number;
  height: number;
  durationInFrames: number;
  mainVideoSrc: string;
  fontSrc: string;
  captions: Caption[];
  fontSize: number;
  fontColor: string;
  highlightColor: string;
  shadowStrength: number;
  shadowBlurPx: number;
  yFromBottom: number;
  padding: number;
  renderMainVideo: boolean;
  overlayTitle: string;
  titlePillEdge: "top" | "bottom";
  faceBandNormIntro: FaceBandNorm | null;
  faceCxNormIntro: number | null;
};

const defaultCaptionsProps: CaptionsProps = {
  fps: 30,
  width: 1080,
  height: 1920,
  durationInFrames: 90,
  mainVideoSrc: "",
  fontSrc: "OpenSans-Bold.ttf",
  captions: [],
  fontSize: 40,
  fontColor: "#ffffff",
  highlightColor: "#FBBF23",
  shadowStrength: 0.4,
  shadowBlurPx: 3.2,
  yFromBottom: 1020,
  padding: 250,
  renderMainVideo: true,
  overlayTitle: "",
  titlePillEdge: "top",
  faceBandNormIntro: null,
  faceCxNormIntro: null,
};

const RemotionRoot = () => {
  return (
    <>
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
      <Composition
        id="CaptionsComposition"
        component={CaptionsComposition}
        fps={defaultCaptionsProps.fps}
        width={defaultCaptionsProps.width}
        height={defaultCaptionsProps.height}
        durationInFrames={defaultCaptionsProps.durationInFrames}
        defaultProps={defaultCaptionsProps}
        calculateMetadata={({ props }) => {
          return {
            fps: props.fps || defaultCaptionsProps.fps,
            width: props.width || defaultCaptionsProps.width,
            height: props.height || defaultCaptionsProps.height,
            durationInFrames:
              props.durationInFrames || defaultCaptionsProps.durationInFrames,
          };
        }}
      />
    </>
  );
};

registerRoot(RemotionRoot);
