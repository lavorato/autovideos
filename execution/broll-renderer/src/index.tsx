import { Composition, registerRoot } from "remotion";
import { BrollComposition } from "./BrollComposition";

const configPath = process.env.BROLL_CONFIG || "broll_config.json";

let config: any = {
  fps: 30,
  width: 2160,
  height: 3840,
  durationInFrames: 90,
  segments: [],
};

try {
  const fs = require("fs");
  const raw = fs.readFileSync(configPath, "utf-8");
  config = JSON.parse(raw);
} catch {}

const RemotionRoot = () => {
  return (
    <Composition
      id="BrollComposition"
      component={BrollComposition}
      durationInFrames={config.durationInFrames}
      fps={config.fps}
      width={config.width}
      height={config.height}
      defaultProps={{
        segments: config.segments,
        mainVideoSrc: config.mainVideoSrc || "",
      }}
    />
  );
};

registerRoot(RemotionRoot);
