import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

export type BrollSegment = {
  /**
   * Filename (basename only) of the asset inside the configured public dir.
   * The Python side passes `--public-dir` pointing at `input/<base>/`, so
   * `staticFile(assetName)` resolves to a URL that Chrome can actually fetch.
   */
  assetName: string;
  assetType: "video" | "image";
  startFrame: number;
  durationFrames: number;
  animation: "slide-left" | "slide-right" | "slide-up" | "scale-in" | "none";
  splitRatio: number;
  position: "left" | "right" | "top" | "bottom";
};

type Props = {
  segments: BrollSegment[];
  mainVideoSrc: string;
  // Metadata fields (also consumed by calculateMetadata in index.tsx).
  // They're unused inside the component but must exist on the props type
  // so Remotion accepts them in inputProps without prop-type errors.
  fps?: number;
  width?: number;
  height?: number;
  durationInFrames?: number;
};

/**
 * Renders a single B-roll clip that fills the entire composition frame.
 * The composition size is set to the B-roll region dimensions (not the full video).
 * Animations slide/scale the content within this frame.
 */
const AnimatedBroll: React.FC<{ segment: BrollSegment }> = ({ segment }) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();

  const enterDuration = Math.min(15, segment.durationFrames / 3);
  const exitStart = segment.durationFrames - enterDuration;

  const enterProgress = spring({
    frame,
    fps,
    config: { damping: 15, stiffness: 120 },
    durationInFrames: enterDuration,
  });

  const exitProgress =
    frame >= exitStart
      ? interpolate(frame, [exitStart, segment.durationFrames], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        })
      : 0;

  const progress = enterProgress * (1 - exitProgress);

  let transform = "";
  switch (segment.animation) {
    case "slide-left":
      transform = `translateX(${interpolate(progress, [0, 1], [width, 0])}px)`;
      break;
    case "slide-right":
      transform = `translateX(${interpolate(progress, [0, 1], [-width, 0])}px)`;
      break;
    case "slide-up":
      transform = `translateY(${interpolate(progress, [0, 1], [height, 0])}px)`;
      break;
    case "scale-in":
      const scale = interpolate(progress, [0, 1], [0.3, 1]);
      transform = `scale(${scale})`;
      break;
    case "none":
      transform = "";
      break;
    default:
      transform = "";
      break;
  }

  const mediaStyle: React.CSSProperties = {
    width: "100%",
    height: "100%",
    objectFit: "cover",
  };

  const kenBurns =
    segment.assetType === "image"
      ? `scale(${interpolate(frame, [0, segment.durationFrames], [1, 1.08])})`
      : "";

  const src = staticFile(segment.assetName);

  return (
    <AbsoluteFill
      style={{
        backgroundColor: "transparent",
        overflow: "hidden",
        borderRadius: 0,
        transform,
      }}
    >
      <div
        style={{
          width: "100%",
          height: "100%",
          overflow: "hidden",
          borderRadius: 0,
        }}
      >
        {segment.assetType === "video" ? (
          <OffthreadVideo src={src} style={mediaStyle} muted />
        ) : (
          <Img src={src} style={{ ...mediaStyle, transform: kenBurns }} />
        )}
      </div>
      <div
        style={{
          position: "absolute",
          inset: 0,
          borderRadius: 0,
          border: "2px solid rgba(255,255,255,0.25)",
          boxShadow:
            "inset 0 1px 0 rgba(255,255,255,0.1), 0 8px 32px rgba(0,0,0,0.5)",
          pointerEvents: "none",
        }}
      />
    </AbsoluteFill>
  );
};

export const BrollComposition: React.FC<Props> = ({ segments }) => {
  return (
    <AbsoluteFill style={{ backgroundColor: "transparent" }}>
      {segments.map((seg, i) => (
        <Sequence
          key={i}
          from={seg.startFrame}
          durationInFrames={seg.durationFrames}
        >
          <AnimatedBroll segment={seg} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
