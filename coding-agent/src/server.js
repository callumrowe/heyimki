// Ki coding agent
import express from "express";
import { execSync, spawnSync, spawn } from "child_process";
import { readFileSync } from "fs";
import { join } from "path";

const app = express();
app.use(express.json({ limit: "4mb" }));

const PORT = 3002;
const REPO_ROOT = "/repo";

// Pushing commits is opt-in. By default the coding agent commits locally only,
// so a fresh clone of the repo never tries to push to someone else's remote.
const GIT_PUSH = ["1", "true", "yes"].includes((process.env.CODING_AGENT_GIT_PUSH || "").toLowerCase());

// The repo's path on the Docker host. `docker compose` resolves relative
// paths (build contexts, bind mounts) against it, so self-deploys build the
// same files the host sees. The entrypoint symlinks it to /repo.
const REPO_HOST_PATH = process.env.REPO_HOST_PATH || "";

// Every endpoint requires INTERNAL_SECRET. This service can rewrite the repo
// and drive the Docker socket, and it shares a network with the headless
// browser, so it must not trust anything that can merely reach it.
const INTERNAL_SECRET = process.env.INTERNAL_SECRET || "";
if (!INTERNAL_SECRET) {
  console.error("[coding-agent] INTERNAL_SECRET is required");
  process.exit(1);
}
app.use((req, res, next) => {
  if (req.path === "/health") return next();
  if (req.get("authorization") !== `Bearer ${INTERNAL_SECRET}`) {
    return res.status(401).json({ error: "Unauthorized" });
  }
  next();
});

function currentBranch() {
  const r = spawnSync("git", ["-C", REPO_ROOT, "rev-parse", "--abbrev-ref", "HEAD"], { encoding: "utf8" });
  return (r.stdout || "main").trim() || "main";
}

function composeArgs(...rest) {
  // --project-directory must be the host path so the Docker daemon gets host
  // paths for bind mounts; -f reads the compose file through the /repo mount.
  const dir = REPO_HOST_PATH || REPO_ROOT;
  return ["compose", "--project-directory", dir, "-f", `${dir}/docker-compose.yml`, ...rest];
}

let _runningChild = null;

// ── Claude Code runner ────────────────────────────────────────────────────────

function runClaude(task) {
  return new Promise((resolve, reject) => {
    console.log(`[coding-agent] spawning claude: ${task.slice(0, 120)}`);
    const child = spawn(
      "claude",
      ["--print", "--verbose", "--dangerously-skip-permissions", "--output-format", "stream-json", task],
      { cwd: REPO_ROOT, env: { ...process.env }, stdio: ["ignore", "pipe", "pipe"] }
    );

    let stderr = "";
    let summary = "";
    let inputTokens = 0;
    let outputTokens = 0;
    let cost_usd = 0;
    let buf = "";

    child.stdout.on("data", d => {
      buf += d;
      // stream-json emits one JSON object per line
      const lines = buf.split("\n");
      buf = lines.pop(); // keep any incomplete trailing line
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const event = JSON.parse(line);
          if (event.type === "assistant") {
            // Log tool calls and text as they arrive so progress is visible in logs
            for (const block of event.message?.content ?? []) {
              if (block.type === "tool_use") {
                const label = block.input?.path ?? block.input?.command ?? block.input?.description ?? block.input?.query ?? "";
                console.log(`[claude] tool: ${block.name}${label ? ` — ${String(label).slice(0, 80)}` : ""}`);
              } else if (block.type === "text" && block.text?.trim()) {
                console.log(`[claude] text: ${block.text.trim().slice(0, 120)}`);
              }
            }
          } else if (event.type === "result") {
            // The final "result" event carries the summary and usage
            summary = event.result ?? "";
            cost_usd = event.cost_usd ?? event.total_cost ?? 0;
            if (event.usage) {
              inputTokens = event.usage.input_tokens ?? 0;
              outputTokens = event.usage.output_tokens ?? 0;
            }
            console.log(`[claude] done — ${event.num_turns} turns, $${cost_usd.toFixed(4)}, ${event.is_error ? "ERROR" : "success"}`);
          }
        } catch { /* ignore non-JSON lines */ }
      }
    });

    _runningChild = child;
    child.stderr.on("data", d => { stderr += d; process.stderr.write(d); });

    child.on("close", (code, signal) => {
      _runningChild = null;
      if (signal === "SIGKILL" || signal === "SIGTERM") {
        return reject(new Error("killed"));
      }
      if (code !== 0) {
        return reject(new Error(stderr.trim() || `claude exited with code ${code}`));
      }
      resolve({ summary: summary || "(no output)", inputTokens, outputTokens, cost_usd });
    });
  });
}

// ── Git helpers ───────────────────────────────────────────────────────────────

function extractRequiredEnvVars(filePaths) {
  const envVars = [];
  for (const relPath of filePaths) {
    if (!relPath.endsWith(".py")) continue;
    try {
      const abs = join(REPO_ROOT, relPath);
      const content = readFileSync(abs, "utf8");
      const match = content.match(/REQUIRED_ENV_VARS\s*=\s*\[([\s\S]*?)\]/);
      if (match) {
        const items = [...match[1].matchAll(/["']([^"']+)["']/g)].map(m => m[1]);
        envVars.push(...items);
      }
    } catch { /* ignore */ }
  }
  return [...new Set(envVars)];
}

function gitCommitAndPush(filePaths, taskSummary) {
  // Stage ALL changes in the repo (new files, edits, deletions) — not just the
  // files explicitly tracked by the agent loop; this catches anything the LLM
  // wrote via shell commands or indirect edits.
  try {
    execSync(`git -C ${JSON.stringify(REPO_ROOT)} add -A`, { encoding: "utf8", stdio: "pipe" });
  } catch (e) {
    console.warn(`[coding-agent] git add -A failed: ${e.message}${e.stderr ? `\n${e.stderr}` : ""}`);
    return { reason: e.stderr || e.message };
  }

  // Check whether there is actually anything to commit.
  let hasChanges = false;
  try {
    const status = execSync(`git -C ${JSON.stringify(REPO_ROOT)} status --porcelain`, { encoding: "utf8", stdio: "pipe" });
    hasChanges = status.trim().length > 0;
  } catch (e) {
    console.warn(`[coding-agent] git status failed: ${e.message}${e.stderr ? `\n${e.stderr}` : ""}`);
  }

  if (!hasChanges) {
    console.log(`[coding-agent] nothing to commit — working tree clean`);
    return null;
  }

  // Build a short conventional-commit message (max 60 chars).
  const lower = (taskSummary || "").toLowerCase();
  let type = "feat";
  if (/\b(fix|bug|broken)\b/.test(lower)) type = "fix";
  else if (/\brefactor\b/.test(lower)) type = "refactor";
  else if (/\b(chore|remove|delete|cleanup)\b/.test(lower)) type = "chore";
  const desc = (taskSummary || "update repo")
    .replace(/\n[\s\S]*/g, "")
    .replace(/[*_`#[\]]/g, "")
    .trim()
    .toLowerCase();
  const msg = (`${type}: ${desc}`).slice(0, 60);

  {
    // Use spawnSync so the commit message is passed as a literal argument and
    // never interpreted by the shell — backticks, asterisks, parentheses, and
    // other special characters in the message are therefore safe.
    const commitResult = spawnSync(
      "git", ["-C", REPO_ROOT, "commit", "-m", msg],
      { encoding: "utf8", timeout: 30000 }
    );
    if (commitResult.status !== 0) {
      const out = (commitResult.stderr || commitResult.stdout || "").trim();
      // "nothing to commit" is not a real error — treat it as clean.
      if (out.includes("nothing to commit") || out.includes("nothing added")) {
        console.log(`[coding-agent] nothing to commit — working tree clean`);
        return null;
      }
      console.warn(`[coding-agent] git commit failed: ${out}`);
      return { reason: out || "git commit failed" };
    }
    console.log(`[coding-agent] committed: ${msg}`);
  }

  if (!GIT_PUSH) {
    console.log(`[coding-agent] committed locally (CODING_AGENT_GIT_PUSH is off)`);
    return null;
  }

  const branch = currentBranch();

  // Pull --rebase before pushing so divergent histories don't block us.
  {
    const pullResult = spawnSync(
      "git", ["-C", REPO_ROOT, "pull", "--rebase", "origin", branch],
      { encoding: "utf8", timeout: 60000 }
    );
    if (pullResult.status !== 0) {
      const out = (pullResult.stderr || pullResult.stdout || "").trim();
      console.warn(`[coding-agent] git pull --rebase failed (push will be attempted anyway): ${out}`);
    } else {
      console.log(`[coding-agent] git pull --rebase ok`);
    }
  }

  {
    const pushResult = spawnSync(
      "git", ["-C", REPO_ROOT, "push", "origin", branch],
      { encoding: "utf8", timeout: 60000 }
    );
    if (pushResult.status !== 0) {
      const out = (pushResult.stderr || pushResult.stdout || "").trim();
      console.error(`[coding-agent] git push FAILED (commit is local only): ${out}`);
      return { reason: out || "git push failed" };
    }
    console.log(`[coding-agent] pushed to origin`);
    return null;
  }
}

// ── HTTP server ───────────────────────────────────────────────────────────────

app.post("/code", async (req, res) => {
  const { task, conversation_context } = req.body ?? {};
  if (!task) return res.status(400).json({ error: "task required" });
  console.log(`[coding-agent] task: ${task.slice(0, 120)}`);

  // Snapshot HEAD before running so we can diff afterwards
  let headBefore = null;
  try {
    headBefore = execSync(`git -C ${JSON.stringify(REPO_ROOT)} rev-parse HEAD`, { encoding: "utf8" }).trim();
  } catch { /* repo may have no commits yet */ }

  try {
    const contextBlock = Array.isArray(conversation_context) && conversation_context.length > 0
      ? `RECENT CONVERSATION (for context — read this to understand the full intent before looking at the code):\n${conversation_context.join("\n")}\n\n`
      : "";
    const constrainedTask = `IMPORTANT CONSTRAINTS — you must follow these exactly:\n- NEVER run docker, docker compose, or any container/deployment commands\n- NEVER attempt to deploy, restart, or rebuild services\n- Your ONLY job is: read code, write/edit files, then stop\n- Do NOT run the code you write\n- Do NOT verify your changes by running services\n- If you think a deploy is needed, just note it in your summary — do not do it\n\n${contextBlock}TASK:\n${task}`;
    const { summary, inputTokens, outputTokens, cost_usd } = await runClaude(constrainedTask);

    // Safety-net commit in case Claude Code didn't commit (shouldn't happen, but belt+braces)
    const gitWarning = gitCommitAndPush([], summary);

    // Derive files_written from git diff against pre-run HEAD
    let filesWritten = [];
    if (headBefore) {
      try {
        const diff = execSync(
          `git -C ${JSON.stringify(REPO_ROOT)} diff --name-only ${headBefore} HEAD`,
          { encoding: "utf8" }
        );
        filesWritten = diff.trim().split("\n").filter(Boolean);
      } catch { /* ignore */ }
    }

    const requiredEnvVars = extractRequiredEnvVars(filesWritten);
    const committed = !gitWarning?.reason;
    return res.json({
      summary,
      files_written: filesWritten,
      required_env_vars: requiredEnvVars,
      input_tokens: inputTokens,
      output_tokens: outputTokens,
      cost_usd,
      committed,
      ...(gitWarning?.reason ? { git_push_failed: true, git_push_error: gitWarning.reason } : {}),
    });
  } catch (e) {
    console.error(`[coding-agent] unhandled error: ${e}`);
    return res.status(500).json({ error: e.message });
  }
});

app.get("/health", (_req, res) => res.json({ status: "ok" }));

app.post("/kill", (req, res) => {
  if (_runningChild && !_runningChild.killed) {
    _runningChild.kill("SIGKILL");
    console.log("[kill] sent SIGKILL to running claude process");
    return res.json({ ok: true, killed: true });
  }
  return res.json({ ok: true, killed: false });
});

// ── Service logs endpoint ─────────────────────────────────────────────────────
// Fetch recent Docker logs for a named Ki service.
// POST /logs  { "service": "api", "lines": 100 }
const _LOG_SERVICE_CONTAINERS = {
  "api":          "ki-api-1",
  "browser":      "ki-api-1",   // browser tool runs inside the api container
  "baileys":      "ki-baileys-1",
  "coding-agent": "ki-coding-agent-1",
  "postgres":     "ki-postgres-1",
  "qdrant":       "ki-qdrant-1",
  "phoenix":      "ki-phoenix-1",
};

app.post("/logs", (req, res) => {
  const { service, lines = 100 } = req.body ?? {};
  if (!service) return res.status(400).json({ error: "service required" });

  const container = _LOG_SERVICE_CONTAINERS[service];
  if (!container) {
    return res.status(400).json({
      error: `Unknown service '${service}'. Allowed: ${Object.keys(_LOG_SERVICE_CONTAINERS).join(", ")}`,
    });
  }

  const tailLines = Math.max(1, Math.min(parseInt(lines) || 100, 2000));

  const result = spawnSync("docker", ["logs", "--tail", tailLines.toString(), container], {
    encoding: "utf8",
    timeout: 15000,
  });

  if (result.error) {
    return res.json({ output: `[docker error: ${result.error.message}]`, service, container });
  }

  // docker logs writes to stderr by default; combine both streams
  const output = (result.stdout || "") + (result.stderr || "");
  return res.json({ output: output || "(no output)", service, container });
});

// ── Restart endpoint ──────────────────────────────────────────────────────────
// Fast path for bind-mounted code changes (api/src/**, coding-agent/src/**).
// No rebuild needed — just restart the containers to pick up new Python/JS.
// POST /restart  {}
app.post("/restart", (req, res) => {
  // Return immediately — the api container will drop this connection on restart
  res.status(202).json({ status: "restarting", message: "Restarting api and coding-agent. Ready in ~10 seconds." });

  setImmediate(() => {
    console.log("[restart] Restarting api and coding-agent containers...");
    // Use 'docker restart' directly — avoids needing a compose file path that resolves
    // differently inside the container (/repo) vs on the host.
    const result = spawnSync(
      "docker",
      ["restart", "ki-api-1", "ki-coding-agent-1"],
      { encoding: "utf8", timeout: 60000, stdio: "pipe" }
    );
    if (result.error || result.status !== 0) {
      console.error(`[restart] FAILED (exit ${result.status}): ${result.error?.message || ""}`);
      if (result.stderr) console.error(`[restart] stderr: ${result.stderr.trim()}`);
      if (result.stdout) console.error(`[restart] stdout: ${result.stdout.trim()}`);
    } else {
      console.log("[restart] Done.");
      if (result.stdout?.trim()) console.log(`[restart] stdout: ${result.stdout.trim()}`);
    }
  });
});

// ── Deploy endpoint ───────────────────────────────────────────────────────────
// Full rebuild: git pull, rebuilds api+baileys+coding-agent, health-checks, auto-rolls back.
// Fire-and-forget: returns 202 immediately, rebuild runs in background.
// POST /deploy  {}
app.post("/deploy", (req, res) => {
  // Return immediately — api will be restarted, so this response must arrive first
  res.status(202).json({ status: "deploying", message: "Full rebuild started. Ki will be back in ~60-90 seconds." });

  setImmediate(async () => {
    let output = "";
    const log = (msg) => {
      console.log(`[deploy] ${msg}`);
      output += `[deploy] ${msg}\n`;
    };

    try {
      // 1. Pull latest code (only when pushing is on — otherwise the local
      //    working tree is the source of truth)
      if (GIT_PUSH) {
        log("Pulling latest code...");
        const pullResult = spawnSync(
          "git", ["-C", REPO_ROOT, "pull", "--rebase", "origin", currentBranch()],
          { encoding: "utf8", timeout: 60000 }
        );
        if (pullResult.stdout) output += pullResult.stdout;
        if (pullResult.stderr) output += pullResult.stderr;

        if (pullResult.status !== 0) {
          log(`git pull --rebase failed (exit ${pullResult.status})`);
          return;
        }
      }

      const newHead = execSync(
        `git -C ${JSON.stringify(REPO_ROOT)} rev-parse HEAD`,
        { encoding: "utf8" }
      ).trim();
      log(`HEAD is now ${newHead.slice(0, 7)}`);

      // 2. Rebuild. No service list: `up` starts whatever COMPOSE_PROFILES in
      //    .env enables, so a disabled service (e.g. WhatsApp) stays off.
      log("Rebuilding containers (this may take a few minutes)...");
      const upResult = spawnSync(
        "docker",
        composeArgs("up", "--build", "-d"),
        { encoding: "utf8", timeout: 360000, env: { ...process.env, CACHEBUST: Date.now().toString() } }
      );
      if (upResult.stdout) output += upResult.stdout;
      if (upResult.stderr) output += upResult.stderr;

      if (upResult.error || upResult.status !== 0) {
        log(`docker compose up failed${upResult.error ? `: ${upResult.error.message}` : ` (exit ${upResult.status})`}`);
        return;
      }

      // 3. Health check — 30 attempts × 3s = 90s total
      log("Health checking api...");
      let healthy = false;
      for (let i = 0; i < 30; i++) {
        await new Promise(r => setTimeout(r, 3000));
        try {
          const healthResp = await fetch("http://api:8000/health", { signal: AbortSignal.timeout(3000) });
          if (healthResp.ok) { healthy = true; break; }
        } catch { /* keep retrying */ }
        if ((i + 1) % 5 === 0) log(`  attempt ${i + 1}/30...`);
      }

      if (healthy) {
        log(`Deploy successful! (${newHead.slice(0, 7)})`);
        return;
      }

      // 4. Auto-rollback
      log("Health check failed — reverting commit and rolling back...");
      const revertResult = spawnSync(
        "git", ["-C", REPO_ROOT, "revert", "--no-edit", "HEAD"],
        { encoding: "utf8", timeout: 30000 }
      );
      if (revertResult.stdout) output += revertResult.stdout;
      if (revertResult.stderr) output += revertResult.stderr;

      if (GIT_PUSH) {
        spawnSync("git", ["-C", REPO_ROOT, "push", "origin", currentBranch()], { encoding: "utf8", timeout: 60000 });
      }
      spawnSync(
        "docker",
        composeArgs("up", "--build", "-d"),
        { encoding: "utf8", timeout: 360000, env: { ...process.env, CACHEBUST: Date.now().toString() } }
      );
      log("Rolled back to previous version.");

    } catch (e) {
      console.error(`[deploy] error: ${e.message}`);
    }
  });
});

app.listen(PORT, () => console.log(`[coding-agent] listening on :${PORT}`));
