// Ki coding agent — runs Claude Code in its own clone of the repo.
//
// Isolation: this container has no Docker socket, no .env, no secrets/ and no
// vault, and its network reaches only the api. It sees the repo as a read-only
// .git directory (/src.git), clones that into its own volume (/workspace) and
// works there on the `ki/work` branch. It never writes the live checkout:
// the api fetches its work as a git bundle and applies it only after the user
// confirms a deploy (api/src/self_deploy.py).
import express from "express";
import { spawnSync, spawn } from "child_process";
import { existsSync, readFileSync, rmSync } from "fs";
import { join } from "path";

const PORT = 3002;
const SRC = "/src.git";          // the live repo's .git, read-only
const WORK = "/workspace";       // our own clone
const BRANCH = "ki/work";

// Only the api holds this secret. It is deliberately not INTERNAL_SECRET, which
// would let code running here talk to the api as the user.
const SECRET = process.env.CODING_AGENT_SECRET || "";
if (!SECRET) {
  console.error("[coding-agent] CODING_AGENT_SECRET is not set. Add it to .env (openssl rand -hex 32) and restart.");
  process.exit(1);
}

// ── Recent output, for the api's get_service_logs tool ───────────────────────
const LOG_LINES = [];
const MAX_LOG_LINES = 2000;
for (const level of ["log", "warn", "error"]) {
  const original = console[level].bind(console);
  console[level] = (...args) => {
    const text = args.map(a => (typeof a === "string" ? a : String(a))).join(" ");
    for (const line of text.split("\n")) {
      LOG_LINES.push(line);
      if (LOG_LINES.length > MAX_LOG_LINES) LOG_LINES.shift();
    }
    original(...args);
  };
}

const app = express();
app.use(express.json({ limit: "4mb" }));
app.use((req, res, next) => {
  if (req.path === "/health") return next();
  if (req.get("authorization") !== `Bearer ${SECRET}`) {
    return res.status(401).json({ error: "Unauthorized" });
  }
  next();
});

// ── Git ───────────────────────────────────────────────────────────────────────

function git(args, opts = {}) {
  const r = spawnSync("git", ["-C", WORK, ...args], { encoding: "utf8", timeout: 120000, ...opts });
  return {
    ok: r.status === 0,
    out: (r.stdout || "").trim(),
    err: (r.stderr || r.error?.message || "").trim(),
  };
}

function gitOrThrow(args) {
  const r = git(args);
  if (!r.ok) throw new Error(`git ${args[0]} failed: ${r.err || r.out}`);
  return r.out;
}

function ensureWorkspace() {
  if (existsSync(join(WORK, ".git"))) return;
  if (!existsSync(join(SRC, "HEAD"))) {
    throw new Error("This copy of Ki isn't a git checkout, so the coding agent has no repo to work on.");
  }
  console.log("[coding-agent] cloning the repo into the workspace...");
  const r = spawnSync("git", ["clone", "--quiet", "--no-hardlinks", SRC, WORK], { encoding: "utf8", timeout: 300000 });
  if (r.status !== 0) throw new Error(`git clone failed: ${(r.stderr || "").trim()}`);
  gitOrThrow(["checkout", "-q", "-B", BRANCH]);
}

// The live checkout's current commit.
function liveHead() {
  gitOrThrow(["fetch", "--quiet", SRC, "HEAD"]);
  return gitOrThrow(["rev-parse", "FETCH_HEAD"]);
}

// Put ki/work on top of the live checkout: fast-forward when everything has
// been deployed, rebase undeployed work when the live checkout moved on.
// Returns the live commit.
function sync() {
  ensureWorkspace();
  git(["rebase", "--abort"]);
  gitOrThrow(["checkout", "-q", "-B", BRANCH]);
  git(["reset", "-q", "--hard"]);     // leftovers from an interrupted run
  git(["clean", "-fdq"]);
  const live = liveHead();
  if (git(["merge-base", "--is-ancestor", "HEAD", live]).ok) {
    gitOrThrow(["reset", "-q", "--hard", live]);
  } else if (!git(["merge-base", "--is-ancestor", live, "HEAD"]).ok) {
    const r = git(["rebase", "-q", live]);
    if (!r.ok) {
      git(["rebase", "--abort"]);
      throw new Error(
        "Undeployed changes from an earlier task conflict with the current code. " +
        "Deploy them or discard them (discard_coding_changes), then try again."
      );
    }
  }
  return live;
}

function pendingFiles(live) {
  const out = git(["diff", "--name-only", live, "HEAD"]).out;
  return out ? out.split("\n") : [];
}

function commitAll(taskSummary) {
  gitOrThrow(["add", "-A"]);
  if (!git(["status", "--porcelain"]).out) return;
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
  gitOrThrow(["commit", "-q", "-m", `${type}: ${desc}`.slice(0, 60)]);
}

function extractRequiredEnvVars(filePaths) {
  const envVars = [];
  for (const relPath of filePaths) {
    if (!relPath.endsWith(".py")) continue;
    try {
      const content = readFileSync(join(WORK, relPath), "utf8");
      const match = content.match(/REQUIRED_ENV_VARS\s*=\s*\[([\s\S]*?)\]/);
      if (match) envVars.push(...[...match[1].matchAll(/["']([^"']+)["']/g)].map(m => m[1]));
    } catch { /* deleted or unreadable */ }
  }
  return [...new Set(envVars)];
}

// ── Claude Code ───────────────────────────────────────────────────────────────

let runningChild = null;

function runClaude(task) {
  return new Promise((resolve, reject) => {
    console.log(`[coding-agent] spawning claude: ${task.slice(0, 120)}`);
    // Claude Code needs the Anthropic key, not our own secret.
    const env = { ...process.env };
    delete env.CODING_AGENT_SECRET;
    const child = spawn(
      "claude",
      ["--print", "--verbose", "--dangerously-skip-permissions", "--output-format", "stream-json", task],
      { cwd: WORK, env, stdio: ["ignore", "pipe", "pipe"] }
    );

    let stderr = "";
    let summary = "";
    let inputTokens = 0;
    let outputTokens = 0;
    let costUsd = 0;
    let buf = "";

    child.stdout.on("data", d => {
      buf += d;
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const event = JSON.parse(line);
          if (event.type === "assistant") {
            for (const block of event.message?.content ?? []) {
              if (block.type === "tool_use") {
                const label = block.input?.path ?? block.input?.command ?? block.input?.description ?? block.input?.query ?? "";
                console.log(`[claude] tool: ${block.name}${label ? ` — ${String(label).slice(0, 80)}` : ""}`);
              } else if (block.type === "text" && block.text?.trim()) {
                console.log(`[claude] text: ${block.text.trim().slice(0, 120)}`);
              }
            }
          } else if (event.type === "result") {
            summary = event.result ?? "";
            costUsd = event.cost_usd ?? event.total_cost_usd ?? event.total_cost ?? 0;
            if (event.usage) {
              inputTokens = event.usage.input_tokens ?? 0;
              outputTokens = event.usage.output_tokens ?? 0;
            }
            console.log(`[claude] done — ${event.num_turns} turns, $${Number(costUsd).toFixed(4)}, ${event.is_error ? "ERROR" : "success"}`);
          }
        } catch { /* not JSON */ }
      }
    });

    runningChild = child;
    child.stderr.on("data", d => { stderr += d; process.stderr.write(d); });
    child.on("close", (code, signal) => {
      runningChild = null;
      if (signal === "SIGKILL" || signal === "SIGTERM") return reject(new Error("killed"));
      if (code !== 0) return reject(new Error(stderr.trim() || `claude exited with code ${code}`));
      resolve({ summary: summary || "(no output)", inputTokens, outputTokens, costUsd });
    });
  });
}

const TASK_RULES = `IMPORTANT — how you work here:
- You are in your own clone of Ki's repo, on the branch ${BRANCH}. Edit files and commit. Nothing you do here is live.
- After you finish, Ki shows the user your changes and they decide whether to deploy them. Never try to deploy, restart or rebuild anything yourself. There is no Docker here.
- Check syntax before you finish: python3 -m py_compile for Python files, node --check for JavaScript.
- If the change needs a new env var, a new Python or npm package, or a container rebuild, say so in your summary.
- You can't push. Don't try to change git remotes or hooks.

`;

// ── HTTP ──────────────────────────────────────────────────────────────────────

let busy = false;

app.post("/code", async (req, res) => {
  const { task, conversation_context } = req.body ?? {};
  if (!task) return res.status(400).json({ error: "task required" });
  if (busy) return res.status(409).json({ error: "The coding agent is already working on a task." });
  busy = true;
  console.log(`[coding-agent] task: ${task.slice(0, 120)}`);
  try {
    const live = sync();
    const headBefore = gitOrThrow(["rev-parse", "HEAD"]);
    const context = Array.isArray(conversation_context) && conversation_context.length > 0
      ? `RECENT CONVERSATION (read this to understand the full intent before looking at the code):\n${conversation_context.join("\n")}\n\n`
      : "";
    const { summary, inputTokens, outputTokens, costUsd } = await runClaude(`${TASK_RULES}${context}TASK:\n${task}`);
    commitAll(summary);

    const changed = git(["diff", "--name-only", headBefore, "HEAD"]).out;
    const filesWritten = changed ? changed.split("\n") : [];
    const pending = pendingFiles(live);
    return res.json({
      summary,
      files_written: filesWritten,
      pending_files: pending,
      pending_commits: Number(git(["rev-list", "--count", `${live}..HEAD`]).out || 0),
      required_env_vars: extractRequiredEnvVars(pending),
      input_tokens: inputTokens,
      output_tokens: outputTokens,
      cost_usd: costUsd,
    });
  } catch (e) {
    console.error(`[coding-agent] task failed: ${e.message}`);
    return res.status(500).json({ error: e.message });
  } finally {
    busy = false;
  }
});

// The undeployed work as a git bundle on top of `base` (the live checkout's
// HEAD, sent by the api). 204 when there is nothing to deploy.
app.post("/bundle", (req, res) => {
  if (busy) return res.status(409).json({ error: "The coding agent is still working on a task." });
  try {
    const live = sync();
    const base = req.body?.base || live;
    if (base !== live) {
      return res.status(409).json({ error: "The live checkout changed while preparing the deploy. Try again." });
    }
    const head = gitOrThrow(["rev-parse", "HEAD"]);
    if (head === base) return res.status(204).end();
    const file = "/tmp/ki-work.bundle";
    rmSync(file, { force: true });
    gitOrThrow(["bundle", "create", "-q", file, `${base}..${BRANCH}`]);
    res.set("Content-Type", "application/octet-stream");
    res.set("X-Ki-Head", head);
    return res.send(readFileSync(file));
  } catch (e) {
    console.error(`[coding-agent] bundle failed: ${e.message}`);
    return res.status(500).json({ error: e.message });
  }
});

// Drop undeployed work and start again from the live checkout.
app.post("/discard", (_req, res) => {
  if (busy) return res.status(409).json({ error: "The coding agent is still working on a task." });
  try {
    ensureWorkspace();
    const live = liveHead();
    const dropped = Number(git(["rev-list", "--count", `${live}..${BRANCH}`]).out || 0);
    gitOrThrow(["checkout", "-q", "-B", BRANCH]);
    gitOrThrow(["reset", "-q", "--hard", live]);
    git(["clean", "-fdq"]);
    console.log(`[coding-agent] discarded ${dropped} undeployed commit(s)`);
    return res.json({ ok: true, discarded_commits: dropped });
  } catch (e) {
    return res.status(500).json({ error: e.message });
  }
});

app.post("/logs", (req, res) => {
  const lines = Math.max(1, Math.min(parseInt(req.body?.lines) || 100, MAX_LOG_LINES));
  res.json({ output: LOG_LINES.slice(-lines).join("\n") || "(no output)" });
});

app.post("/kill", (_req, res) => {
  if (runningChild && !runningChild.killed) {
    runningChild.kill("SIGKILL");
    console.log("[kill] sent SIGKILL to running claude process");
    return res.json({ ok: true, killed: true });
  }
  return res.json({ ok: true, killed: false });
});

app.get("/health", (_req, res) => res.json({ status: "ok" }));

app.listen(PORT, () => {
  console.log(`[coding-agent] listening on :${PORT}`);
  try { ensureWorkspace(); } catch (e) { console.warn(`[coding-agent] ${e.message}`); }
});
