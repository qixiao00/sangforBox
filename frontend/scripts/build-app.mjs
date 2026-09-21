import { spawnSync } from "node:child_process";
import { cp, mkdir, rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, "..");
const nextBin = resolve(frontend, "node_modules", "next", "dist", "bin", "next");
const { NODE_OPTIONS: _hostNodeOptions, ...cleanEnvironment } = process.env;
const build = spawnSync(process.execPath, [nextBin, "build"], {
  cwd: frontend,
  env: cleanEnvironment,
  stdio: "inherit",
});

if (build.status !== 0) process.exit(build.status ?? 1);

const source = resolve(frontend, "out");
const target = resolve(frontend, "..", "af_app", "static");
await rm(target, { recursive: true, force: true });
await mkdir(target, { recursive: true });
await cp(source, target, { recursive: true });
console.log(`Synced Next.js export to ${target}`);
