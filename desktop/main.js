const { app, BrowserWindow, ipcMain, nativeTheme, shell } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");

// Load desktop/.env into process.env if present
const ENV_PATH = path.join(__dirname, ".env");
try {
  fs.readFileSync(ENV_PATH, "utf-8")
    .split("\n")
    .forEach((line) => {
      const match = line.match(/^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$/);
      if (match)
        process.env[match[1]] = match[2].trim().replace(/^["']|["']$/g, "");
    });
} catch {}

function getConfigPath() {
  return path.join(app.getPath("userData"), "ki-config.json");
}

function loadConfig() {
  try {
    return JSON.parse(fs.readFileSync(getConfigPath(), "utf-8"));
  } catch {
    return null;
  }
}

function saveConfig(config) {
  const p = getConfigPath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(config, null, 2));
}

ipcMain.handle("load-config", () => loadConfig());
ipcMain.handle("save-config", (_, config) => {
  saveConfig(config);
  // Apply opacity immediately if present, so it takes effect without reload
  if (win && config.opacity != null) {
    const clamped = Math.max(0, Math.min(1, config.opacity));
    win.setOpacity(clamped);
  }
  return true;
});
ipcMain.handle("get-env", (_, key) => process.env[key] ?? null);
ipcMain.handle("open-external", (_, url) => shell.openExternal(url));
ipcMain.handle("set-bg-opacity", (_, value) => {
  if (win) {
    const clamped = Math.max(0, Math.min(1, value));
    win.setOpacity(clamped);
  }
});

// Run `ki deploy` as a login shell process, streaming output back to the renderer.
ipcMain.on("run-deploy", (event) => {
  const kiScript = path.join(app.getPath("home"), "DEVELOPMENT", "ki", "bin", "ki");
  const child = spawn("/bin/zsh", ["-l", "-c", `${kiScript} deploy`], {
    cwd: path.join(app.getPath("home"), "DEVELOPMENT", "ki"),
  });
  const send = (text) => {
    try { event.sender.send("deploy-output", text); } catch {}
  };
  child.stdout.on("data", (d) => send(d.toString()));
  child.stderr.on("data", (d) => send(d.toString()));
  child.on("close", (code) => {
    try { event.sender.send("deploy-done", code ?? 0); } catch {}
  });
  child.on("error", (err) => {
    send(`Error: ${err.message}\n`);
    try { event.sender.send("deploy-done", 1); } catch {}
  });
});

let win = null;

function createWindow() {
  win = new BrowserWindow({
    width: 680,
    height: 820,
    minWidth: 460,
    minHeight: 580,
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 14, y: 12 },
    transparent: true,
    backgroundColor: "#00000000",
    vibrancy: "under-window",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.loadFile(path.join(__dirname, "..", "api", "web", "index.html"));

  // Open external auth URLs in the system browser instead of navigating Electron.
  // Google OAuth initiates at /auth/google and redirects through accounts.google.com —
  // the whole flow must happen externally because the callback goes to the production URL.
  win.webContents.on("will-navigate", (event, url) => {
    if (
      url.startsWith("https://accounts.google.com") ||
      url.startsWith("https://oauth2.googleapis.com")
    ) {
      event.preventDefault();
      shell.openExternal(url);
      return;
    }
    if (url.includes("/auth/google")) {
      event.preventDefault();
      let targetUrl = url;
      if (url.startsWith("file://")) {
        // file:// context: reconstruct as the production HTTPS URL
        const authPath = url.substring(url.indexOf("/auth/google"));
        targetUrl = `https://ki.example.com${authPath}`;
      }
      shell.openExternal(targetUrl);
    }
  });

  // Also handle window.open() / target="_blank" links for the same auth patterns.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (
      url.startsWith("https://accounts.google.com") ||
      url.startsWith("https://oauth2.googleapis.com") ||
      url.includes("/auth/google")
    ) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });

  win.webContents.on('did-finish-load', () => {
    win.webContents.setZoomFactor(1.0);
    // Apply saved window opacity directly via win.setOpacity()
    const savedConfig = loadConfig();
    const bgOpacity = savedConfig?.opacity ?? 0.62;
    const clamped = Math.max(0, Math.min(1, bgOpacity));
    win.setOpacity(clamped);
  })
}

app.whenReady().then(() => {
  if (app.dock) {
    app.dock.setIcon(path.join(__dirname, "icon.png"));
  }
  createWindow();
});
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
