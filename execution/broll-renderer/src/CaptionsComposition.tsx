import {
  AbsoluteFill,
  OffthreadVideo,
  Sequence,
  continueRender,
  delayRender,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useEffect, useMemo, useState } from "react";

export type CaptionWord = {
  word: string;
  start: number;
  end: number;
};

export type Caption = {
  text: string;
  start: number;
  end: number;
  words: CaptionWord[];
};

export type CaptionsProps = {
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
  fps?: number;
  width?: number;
  height?: number;
  durationInFrames?: number;
};

const FONT_FAMILY = "OpenSansBold";

/**
 * Asynchronously register the bundled bold font via FontFace. We block the
 * first frame with delayRender() so the text never flashes in a fallback
 * system font, then release it once the font is in document.fonts.
 */
const useCaptionFont = (fontSrc: string) => {
  const [handle] = useState(() => delayRender("loading-caption-font"));

  useEffect(() => {
    let cancelled = false;
    const url = staticFile(fontSrc);
    const face = new FontFace(FONT_FAMILY, `url(${url})`);
    face
      .load()
      .then((loaded) => {
        if (cancelled) return;
        (document as Document).fonts.add(loaded);
        continueRender(handle);
      })
      .catch((err) => {
        console.error("Failed to load caption font", err);
        continueRender(handle);
      });
    return () => {
      cancelled = true;
    };
  }, [fontSrc, handle]);
};

type CaptionLineProps = {
  caption: Caption;
  fontSize: number;
  fontColor: string;
  highlightColor: string;
  shadowStrength: number;
  shadowBlurPx: number;
  yFromBottom: number;
  padding: number;
};

const CaptionLine: React.FC<CaptionLineProps> = ({
  caption,
  fontSize,
  fontColor,
  highlightColor,
  shadowStrength,
  shadowBlurPx,
  yFromBottom,
  padding,
}) => {
  const frame = useCurrentFrame();
  const { fps, height } = useVideoConfig();
  const tSeconds = frame / fps + caption.start;

  // Captacity's highlight rule: the active word runs from its start until
  // the start of the next word (or its own end for the final word).
  const activeIndex = useMemo(() => {
    const words = caption.words;
    if (!words.length) return -1;
    for (let i = 0; i < words.length; i++) {
      const w = words[i];
      const next = words[i + 1];
      const end = next ? next.start : w.end;
      if (tSeconds >= w.start && tSeconds < end) {
        return i;
      }
    }
    // Before first word starts (rare rounding), highlight first.
    if (tSeconds < words[0].start) return 0;
    return words.length - 1;
  }, [caption.words, tSeconds]);

  const containerStyle: React.CSSProperties = {
    position: "absolute",
    left: padding,
    right: padding,
    bottom: yFromBottom,
    display: "flex",
    justifyContent: "center",
    alignItems: "center",
    pointerEvents: "none",
  };

  const sharedTextStyle: React.CSSProperties = {
    fontFamily: `${FONT_FAMILY}, sans-serif`,
    fontSize,
    fontWeight: 700,
    lineHeight: 1,
    whiteSpace: "pre",
    textAlign: "center",
  };

  const renderWords = (mode: "shadow" | "foreground") => {
    return caption.words.map((w, i) => {
      const text = w.word.startsWith(" ") ? w.word : ` ${w.word}`;
      let color: string;
      if (mode === "shadow") {
        color = "#000";
      } else if (i === activeIndex) {
        color = highlightColor;
      } else {
        color = fontColor;
      }
      return (
        <span key={i} style={{ color }}>
          {text}
        </span>
      );
    });
  };

  // The shadow is a stacked blurred copy of the line underneath the
  // foreground text. Opacity encodes captacity's SHADOW_STRENGTH.
  return (
    <div style={containerStyle}>
      <div style={{ position: "relative" }}>
        {shadowStrength > 0 && shadowBlurPx > 0 ? (
          <div
            aria-hidden
            style={{
              ...sharedTextStyle,
              position: "absolute",
              inset: 0,
              opacity: shadowStrength,
              filter: `blur(${shadowBlurPx}px)`,
              color: "#000",
            }}
          >
            {renderWords("shadow")}
          </div>
        ) : null}
        <div style={{ ...sharedTextStyle, position: "relative" }}>
          {renderWords("foreground")}
        </div>
      </div>
    </div>
  );
};

export const CaptionsComposition: React.FC<CaptionsProps> = ({
  mainVideoSrc,
  fontSrc,
  captions,
  fontSize,
  fontColor,
  highlightColor,
  shadowStrength,
  shadowBlurPx,
  yFromBottom,
  padding,
}) => {
  useCaptionFont(fontSrc);
  const { fps } = useVideoConfig();

  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      <OffthreadVideo src={staticFile(mainVideoSrc)} />
      {captions.map((caption, i) => {
        const from = Math.max(0, Math.round(caption.start * fps));
        const duration = Math.max(
          1,
          Math.round((caption.end - caption.start) * fps),
        );
        return (
          <Sequence key={i} from={from} durationInFrames={duration}>
            <CaptionLine
              caption={caption}
              fontSize={fontSize}
              fontColor={fontColor}
              highlightColor={highlightColor}
              shadowStrength={shadowStrength}
              shadowBlurPx={shadowBlurPx}
              yFromBottom={yFromBottom}
              padding={padding}
            />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
