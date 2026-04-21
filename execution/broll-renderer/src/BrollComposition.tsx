import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

type BrollSegment = {
  assetPath: string;
  assetType: "video" | "image";
  startFrame: number;
  durationFrames: number;
  animation: "slide-left" | "slide-right" | "slide-up" | "scale-in";
  splitRatio: number;
  position: "left" | "right" | "top" | "bottom";
};

type Props = {
  segments: BrollSegment[];
  mainVideoSrc: string;
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

  // Slide/scale animation within the clip's own frame
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
    case "scale-in": {
      const scale = interpolate(progress, [0, 1], [0.3, 1]);
      transform = `scale(${scale})`;
      break;
    }
  }

  const mediaStyle: React.CSSProperties = {
    width: "100%",
    height: "100%",
    objectFit: "cover",
  };

  // Subtle Ken Burns on images
  const kenBurns =
    segment.assetType === "image"
      ? `scale(${interpolate(frame, [0, segment.durationFrames], [1, 1.08])})`
      : "";

  return (
    <AbsoluteFill
      style={{
        backgroundColor: "#111",
        overflow: "hidden",
        borderRadius: 24,
        transform,
      }}
    >
      {/* Media content */}
      <div style={{ width: "100%", height: "100%", overflow: "hidden", borderRadius: 24 }}>
        {segment.assetType === "video" ? (
          <OffthreadVideo src={segment.assetPath} style={mediaStyle} />
        ) : (
          <Img
            src={segment.assetPath}
            style={{ ...mediaStyle, transform: kenBurns }}
          />
        )}
      </div>
      {/* Glassmorphism border */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          borderRadius: 24,
          border: "2px solid rgba(255,255,255,0.25)",
          boxShadow: "inset 0 1px 0 rgba(255,255,255,0.1), 0 8px 32px rgba(0,0,0,0.5)",
          pointerEvents: "none",
        }}
      />
    </AbsoluteFill>
  );
};

export const BrollComposition: React.FC<Props> = ({ segments }) => {
  return (
    <AbsoluteFill style={{ backgroundColor: "#111" }}>
      {segments.map((seg, i) => (
        <Sequence key={i} from={seg.startFrame} durationInFrames={seg.durationFrames}>
          <AnimatedBroll segment={seg} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
