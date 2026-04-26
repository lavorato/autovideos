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
  /**
   * Optional Instagram-style top banner: white rounded box, black bold text,
   * shown for the full composition duration (burned in during step 09).
   */
  overlayTitle?: string;
  /**
   * When true (default), the composition renders the source video as a
   * full-bleed background via <OffthreadVideo>. When false, the background
   * is transparent and only the caption text is painted — used by the
   * overlay-only render path that composites against the untouched source
   * via FFmpeg afterwards, skipping a costly video decode+re-encode.
   */
  renderMainVideo?: boolean;
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

const TopTitlePill: React.FC<{ text: string }> = ({ text }) => {
  const trimmed = text.trim();
  const { width, height } = useVideoConfig();
  if (!trimmed) {
    return null;
  }
  const fontSize = Math.round(Math.min(44, Math.max(24, width * 0.034)));
  const padV = Math.round(fontSize * 0.55);
  const padH = Math.round(fontSize * 0.72);
  const sideInset = Math.round(Math.max(28, width * 0.045));
  const top = Math.round(Math.max(36, height * 0.022));
  const radius = Math.round(Math.min(36, width * 0.026));

  return (
    <div
      style={{
        position: "absolute",
        left: sideInset,
        right: sideInset,
        top,
        display: "flex",
        justifyContent: "center",
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          backgroundColor: "#ffffff",
          color: "#000000",
          fontFamily: `${FONT_FAMILY}, system-ui, sans-serif`,
          fontWeight: 700,
          fontSize,
          lineHeight: 1.28,
          textAlign: "center",
          padding: `${padV}px ${padH}px`,
          borderRadius: radius,
          maxWidth: "100%",
          boxSizing: "border-box",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {trimmed}
      </div>
    </div>
  );
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

const MAX_WORDS_PER_LINE = 3;

/** How long the top title pill stays on screen (matches typical hook length). */
const OVERLAY_TITLE_DURATION_SEC = 5;

/**
 * Split a caption into sub-captions of at most MAX_WORDS_PER_LINE words.
 * Each chunk keeps the original word timings, but its `start`/`end` are
 * tightened to the chunk's own word range so the Sequence only plays while
 * those words are spoken — meaning at most 3 words are ever on screen.
 */
const chunkCaption = (caption: Caption): Caption[] => {
  const { words } = caption;
  if (words.length <= MAX_WORDS_PER_LINE) return [caption];
  const chunks: Caption[] = [];
  for (let i = 0; i < words.length; i += MAX_WORDS_PER_LINE) {
    const slice = words.slice(i, i + MAX_WORDS_PER_LINE);
    const nextSlice = words.slice(
      i + MAX_WORDS_PER_LINE,
      i + MAX_WORDS_PER_LINE + 1,
    );
    const start = slice[0].start;
    // Extend the chunk up to the next chunk's first word so the last word's
    // highlight-until-next-start rule keeps working inside the chunk.
    const end = nextSlice.length ? nextSlice[0].start : caption.end;
    chunks.push({
      text: slice.map((w) => w.word).join("").trim(),
      start,
      end,
      words: slice,
    });
  }
  return chunks;
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
  overlayTitle = "",
  renderMainVideo = true,
}) => {
  useCaptionFont(fontSrc);
  const { fps } = useVideoConfig();

  // Overlay-only mode: transparent background + no video decode. The Python
  // side composites the resulting alpha track over the untouched source via
  // a single FFmpeg overlay pass, which is ~10x faster than re-encoding the
  // whole video inside Chrome headless.
  const backgroundColor = renderMainVideo ? "#000" : "transparent";

  const chunkedCaptions = useMemo(
    () => captions.flatMap(chunkCaption),
    [captions],
  );

  return (
    <AbsoluteFill style={{ backgroundColor }}>
      {renderMainVideo && mainVideoSrc ? (
        <OffthreadVideo src={staticFile(mainVideoSrc)} />
      ) : null}
      {overlayTitle.trim() ? (
        <Sequence
          from={0}
          durationInFrames={Math.max(
            1,
            Math.round(OVERLAY_TITLE_DURATION_SEC * fps),
          )}
        >
          <TopTitlePill text={overlayTitle} />
        </Sequence>
      ) : null}
      {chunkedCaptions.map((caption, i) => {
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
