#!/usr/bin/env node
/**
 * `npx voxa-code` — thin Node wrapper around the Python launcher.
 *
 * It finds a usable Python and runs `python -m server.cli`, which starts the
 * server + tunnel and prints a scannable QR.
 *
 * Python resolution order:
 *   1. in-repo .venv        (dev / cloned repo, fast path)
 *   2. existing ~/.voxa/venv (already bootstrapped, fast path)
 *   3. a system python3.11+  (bootstrap a venv with stdlib `venv` + pip)
 *   4. uv (Astral)          (no system Python: install uv, let it provision
 *                            Python 3.12 and create the venv)
 *
 * Dependency-free: Node built-ins only.
 */
"use strict";
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const HOME_VENV = path.join(os.homedir(), ".voxa", "venv");
const REQUIREMENTS = path.join(ROOT, "requirements.txt");
const IS_WIN = process.platform === "win32";

function venvPython(dir) {
  return IS_WIN
    ? path.join(dir, "Scripts", "python.exe")
    : path.join(dir, "bin", "python");
}

function findSystemPython() {
  for (const c of ["python3.13", "python3.12", "python3.11", "python3"]) {
    const r = spawnSync(c, ["--version"], { stdio: "ignore" });
    if (r.status === 0) return c;
  }
  return null;
}

// Bootstrap ~/.voxa/venv using a system Python (stdlib venv + pip).
function bootstrapWithSystemPython(py) {
  console.log("Setting up Voxa (one-time, ~a minute)…");
  fs.mkdirSync(path.dirname(HOME_VENV), { recursive: true });
  run(py, ["-m", "venv", HOME_VENV]);
  const vpy = venvPython(HOME_VENV);
  run(vpy, ["-m", "pip", "install", "-q", "--upgrade", "pip"]);
  run(vpy, ["-m", "pip", "install", "-q", "-r", REQUIREMENTS]);
  return vpy;
}

// Locate the `uv` binary: PATH first, then its default install dir.
function findUv() {
  const probe = spawnSync("uv", ["--version"], { stdio: "ignore" });
  if (probe.status === 0) return "uv";
  const localBin = IS_WIN
    ? path.join(os.homedir(), ".local", "bin", "uv.exe")
    : path.join(os.homedir(), ".local", "bin", "uv");
  if (fs.existsSync(localBin)) return localBin;
  return null;
}

function installUv() {
  console.log("Installing uv (Astral Python manager)…");
  if (IS_WIN) {
    run("powershell", [
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-Command",
      "irm https://astral.sh/uv/install.ps1 | iex",
    ]);
  } else {
    // curl | sh — run through a shell so the pipe works.
    run("sh", ["-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"]);
  }
  const uv = findUv();
  if (!uv) {
    console.error(
      "Could not find uv after install. Please install it manually from https://astral.sh/uv and re-run `npx voxa-code`."
    );
    process.exit(1);
  }
  return uv;
}

// Bootstrap ~/.voxa/venv using uv (provisions Python 3.12 if needed).
function bootstrapWithUv() {
  let uv = findUv();
  if (!uv) uv = installUv();
  console.log("Setting up Voxa with uv (one-time, ~a minute)…");
  fs.mkdirSync(path.dirname(HOME_VENV), { recursive: true });
  run(uv, ["venv", "--python", "3.12", HOME_VENV]);
  const vpy = venvPython(HOME_VENV);
  run(uv, ["pip", "install", "--python", vpy, "-r", REQUIREMENTS]);
  return vpy;
}

function bootstrapVenv() {
  const py = findSystemPython();
  if (py) return bootstrapWithSystemPython(py);
  console.log("No system Python found — provisioning one with uv…");
  return bootstrapWithUv();
}

// Run a command, inheriting stdio; abort setup on failure.
function run(cmd, args) {
  const r = spawnSync(cmd, args, { stdio: "inherit" });
  if (r.error || r.status !== 0) {
    console.error(
      `\nSetup step failed: ${cmd} ${args.join(" ")}` +
        (r.error ? `\n${r.error.message}` : "")
    );
    process.exit(r.status || 1);
  }
}

function main() {
  // By default Voxa uses the hosted relay (no tunnel/cloudflared, nothing to set
  // up). Self-hosters who set VOXA_RELAY_URL to their own box are likewise fine.
  const repoVenv = venvPython(path.join(ROOT, ".venv"));
  let py = fs.existsSync(repoVenv) ? repoVenv : null;
  if (!py) py = fs.existsSync(venvPython(HOME_VENV)) ? venvPython(HOME_VENV) : bootstrapVenv();

  const child = spawn(py, ["-u", "-m", "server.cli"], {
    cwd: ROOT,
    stdio: "inherit",
    env: process.env,
  });
  child.on("exit", (code) => process.exit(code == null ? 0 : code));
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => child.kill(sig));
  }
}

main();
