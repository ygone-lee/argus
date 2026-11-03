// Render demo/promo/promo.html to an MP4, frame by frame, with headless Chrome (DevTools protocol) and ffmpeg.
//
//   node demo/promo/render.mjs                       # → demo/.run/argus-promo.mp4 (1920×1080, 30 fps)
//   node demo/promo/render.mjs --out docs/promo.mp4 --fps 30
//   node demo/promo/render.mjs --still 12.5          # one PNG of the frame at 12.5 s (for checking a scene)
//
// Needs Node 22+ (built-in WebSocket), Google Chrome and ffmpeg with libx264.
import { spawn } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "../..");
const RUN = join(ROOT, "demo", ".run");
const arg = (name, def) => { const i = process.argv.indexOf("--" + name); return i > 0 ? process.argv[i + 1] : def; };
const FPS = +arg("fps", 30);
const OUT = resolve(arg("out", join(RUN, "argus-promo.mp4")));
const STILL = arg("still", null);
const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9333 + Math.floor(Math.random() * 500);
const W = 1920, H = 1080;

mkdirSync(RUN, { recursive: true });
const profile = join(RUN, "chrome-profile-" + PORT);
const chrome = spawn(CHROME, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, `--window-size=${W},${H}`,
  "--hide-scrollbars", "--force-device-scale-factor=1", "--no-first-run", "--no-default-browser-check", "--mute-audio",
  "--disable-extensions", "--disable-background-networking", "--disable-sync", "about:blank",
], { stdio: "ignore" });
const cleanup = () => { try { chrome.kill(); } catch {} setTimeout(() => rmSync(profile, { recursive: true, force: true }), 500); };
process.on("exit", cleanup);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function pageSocket() {
  for (let i = 0; i < 100; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((x) => x.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch {}
    await sleep(100);
  }
  throw new Error("Chrome did not start");
}

const ws = new WebSocket(await pageSocket());
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let seq = 0;
const waiting = new Map();
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.id && waiting.has(m.id)) { const { ok, ko } = waiting.get(m.id); waiting.delete(m.id); m.error ? ko(new Error(m.error.message)) : ok(m.result); }
});
const send = (method, params = {}) => new Promise((ok, ko) => { const id = ++seq; waiting.set(id, { ok, ko }); ws.send(JSON.stringify({ id, method, params })); });
const evaluate = async (expr) => (await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true })).result.value;

await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await send("Page.addScriptToEvaluateOnNewDocument", { source: "window.__capture = true" });
const loaded = new Promise((r) => ws.addEventListener("message", (e) => { if (JSON.parse(e.data).method === "Page.loadEventFired") r(); }));
await send("Page.navigate", { url: pathToFileURL(join(HERE, "promo.html")).href });
await loaded;
await evaluate("document.fonts.ready.then(() => true)");

const shot = async (t, format) => {
  await evaluate(`render(${t}); new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))`);
  const { data } = await send("Page.captureScreenshot", { format, ...(format === "jpeg" ? { quality: 95 } : {}), captureBeyondViewport: false });
  return Buffer.from(data, "base64");
};

if (STILL !== null) {
  const file = resolve(arg("out", join(RUN, `still-${STILL}.png`)));
  writeFileSync(file, await shot(+STILL, "png"));
  console.log(file);
  process.exit(0);
}

const duration = await evaluate("window.DURATION");
const frames = Math.round(duration * FPS);
mkdirSync(dirname(OUT), { recursive: true });
const ff = spawn("ffmpeg", ["-v", "error", "-y", "-f", "image2pipe", "-framerate", String(FPS), "-i", "-",
  "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", OUT], { stdio: ["pipe", "inherit", "inherit"] });
const started = Date.now();
for (let i = 0; i < frames; i++) {
  const buf = await shot(i / FPS, "jpeg");
  if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once("drain", r));
  if (i % FPS === 0) process.stdout.write(`\rframe ${i}/${frames}  (${((Date.now() - started) / 1000).toFixed(0)}s)`);
}
ff.stdin.end();
await new Promise((r) => ff.on("close", r));
console.log(`\n${OUT}`);
process.exit(0);
