<!DOCTYPE html>
<html lang="pt-BR">
  <head>
    <meta charset="UTF-8" />
    <title>percentage-ring</title>
    <style>
      html,
      body {
        margin: 0;
        padding: 0;
        background: transparent;
        font-family: "Plus Jakarta Sans", "Inter", system-ui, -apple-system,
          sans-serif;
      }
    </style>
  </head>
  <body>
    <div
      id="el-root"
      data-composition-id="percentage-ring"
      data-start="0"
      data-duration="__DURATION__"
      data-track-index="0"
      data-width="__WIDTH__"
      data-height="__HEIGHT__"
    >
      <div class="scene-content">
        <div class="backdrop"></div>
        <div class="glow" aria-hidden="true"></div>

        <div class="ring-wrap">
          <svg
            class="ring"
            viewBox="0 0 200 200"
            xmlns="http://www.w3.org/2000/svg"
          >
            <circle
              class="ring-track"
              cx="100"
              cy="100"
              r="88"
              fill="none"
              stroke="rgba(255,255,255,0.12)"
              stroke-width="10"
            />
            <circle
              class="ring-fill"
              cx="100"
              cy="100"
              r="88"
              fill="none"
              stroke="__COLOR_ACCENT__"
              stroke-width="10"
              stroke-linecap="round"
              transform="rotate(-90 100 100)"
              pathLength="100"
              stroke-dasharray="100 100"
              stroke-dashoffset="100"
            />
          </svg>

          <div class="center">
            <div class="number-row">
              <span id="num" class="number">0</span>
              <span class="percent">%</span>
            </div>
          </div>
        </div>

        <div class="label" id="label">__LABEL__</div>
      </div>

      <style>
        [data-composition-id="percentage-ring"] {
          position: relative;
          width: 100%;
          height: 100%;
          background: __COLOR_BG_BASE__;
          color: #f2f3f5;
          overflow: hidden;
        }

        [data-composition-id="percentage-ring"] .scene-content {
          position: relative;
          width: 100%;
          height: 100%;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: min(4vw, 4vh);
          padding: min(8vw, 8vh);
          box-sizing: border-box;
          z-index: 2;
        }

        [data-composition-id="percentage-ring"] .backdrop {
          position: absolute;
          inset: 0;
          background: radial-gradient(
            ellipse at 50% 40%,
            __COLOR_BG_GRADIENT__ 0%,
            rgba(10, 12, 20, 0.92) 55%,
            rgba(5, 7, 14, 1) 100%
          );
          z-index: 0;
        }

        [data-composition-id="percentage-ring"] .glow {
          position: absolute;
          left: 50%;
          top: 50%;
          width: min(70vw, 70vh);
          height: min(70vw, 70vh);
          border-radius: 50%;
          background: radial-gradient(
            circle,
            __COLOR_GLOW__ 0%,
            rgba(0, 0, 0, 0) 70%
          );
          transform: translate(-50%, -50%) scale(1);
          opacity: 0.85;
          filter: blur(40px);
          z-index: 1;
        }

        [data-composition-id="percentage-ring"] .ring-wrap {
          position: relative;
          width: min(56vw, 56vh);
          height: min(56vw, 56vh);
          display: flex;
          align-items: center;
          justify-content: center;
        }

        [data-composition-id="percentage-ring"] .ring {
          width: 100%;
          height: 100%;
          filter: drop-shadow(0 0 24px __COLOR_GLOW__);
        }

        [data-composition-id="percentage-ring"] .center {
          position: absolute;
          inset: 0;
          display: flex;
          align-items: center;
          justify-content: center;
          color: #ffffff;
        }

        [data-composition-id="percentage-ring"] .number-row {
          display: flex;
          align-items: baseline;
          gap: min(0.8vw, 0.8vh);
          font-variant-numeric: tabular-nums;
        }

        [data-composition-id="percentage-ring"] .number {
          font-size: min(18vw, 18vh);
          font-weight: 800;
          letter-spacing: -0.04em;
          line-height: 1;
          color: #ffffff;
        }

        [data-composition-id="percentage-ring"] .percent {
          font-size: min(9vw, 9vh);
          font-weight: 700;
          color: __COLOR_ACCENT__;
          line-height: 1;
        }

        [data-composition-id="percentage-ring"] .label {
          font-size: min(4vw, 4vh);
          font-weight: 600;
          letter-spacing: 0.04em;
          text-transform: uppercase;
          color: rgba(255, 255, 255, 0.88);
          text-align: center;
          max-width: 80%;
          line-height: 1.2;
        }
      </style>

      <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
      <script>
        window.__timelines = window.__timelines || {};

        (function () {
          const VALUE = __VALUE__;
          const DUR = __DURATION__;

          const tl = gsap.timeline({ paused: true });

          // Enter: scene content pops in.
          tl.from(
            '[data-composition-id="percentage-ring"] .scene-content',
            { opacity: 0, duration: 0.25, ease: "power2.out" },
            0
          );

          // Glow: slow breathing throughout the clip, then fade at end.
          tl.from(
            '[data-composition-id="percentage-ring"] .glow',
            { scale: 0.8, opacity: 0, duration: 0.45, ease: "power3.out" },
            0.05
          );

          // Ring fill: animate stroke-dashoffset from 100 -> (100 - VALUE).
          // pathLength is normalized to 100 in the SVG, so VALUE directly
          // represents the percentage filled.
          const ringFillTarget = Math.max(0, 100 - VALUE);
          tl.fromTo(
            '[data-composition-id="percentage-ring"] .ring-fill',
            { attr: { "stroke-dashoffset": 100 } },
            {
              attr: { "stroke-dashoffset": ringFillTarget },
              duration: Math.min(1.1, DUR * 0.55),
              ease: "power2.out",
            },
            0.2
          );

          // Counter: tween an object's scalar and write its rounded value
          // into the DOM on each frame.
          const counter = { v: 0 };
          tl.to(
            counter,
            {
              v: VALUE,
              duration: Math.min(1.1, DUR * 0.55),
              ease: "power2.out",
              onUpdate: function () {
                const node = document.getElementById("num");
                if (node) node.textContent = Math.round(counter.v).toString();
              },
            },
            0.2
          );

          // Number pop: tiny scale accent when the counter lands.
          tl.fromTo(
            '[data-composition-id="percentage-ring"] .number',
            { scale: 0.96 },
            { scale: 1, duration: 0.35, ease: "back.out(2.2)" },
            Math.min(1.1, DUR * 0.55) + 0.05
          );

          // Label: slide up in.
          tl.from(
            '[data-composition-id="percentage-ring"] .label',
            { y: 20, opacity: 0, duration: 0.4, ease: "power3.out" },
            0.45
          );

          // Hold the full frame until the last 0.3s, then fade scene out
          // so the composite hand-off back to the main video is clean.
          const fadeStart = Math.max(0, DUR - 0.3);
          tl.to(
            '[data-composition-id="percentage-ring"] .scene-content',
            { opacity: 0, duration: 0.3, ease: "power2.in" },
            fadeStart
          );

          window.__timelines["percentage-ring"] = tl;
        })();
      </script>
    </div>
  </body>
</html>
