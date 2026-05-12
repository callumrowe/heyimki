# Section Word Count

An [Obsidian](https://obsidian.md) plugin that shows a **live word count for the section under your cursor**, displayed inline at the end of the heading — unobtrusively, without cluttering the note.

```
## My Section  · 247 words
```

The count updates in real time as you type.

---

## Features

- Detects which heading (H1–H6) owns the cursor's current position
- Counts only the body text of that section — stops at the next heading of equal or higher level (standard Markdown section scoping)
- Strips frontmatter, fenced code blocks, inline code, HTML, and Markdown syntax before counting — you get *actual* prose words
- Inline display is subtle: muted colour, small font, styled to feel native to Obsidian's UI
- Widget fades in/out smoothly; slightly brighter on the active line
- Works in both light and dark themes (uses Obsidian CSS variables)
- No external dependencies

---

## Manual Installation

Obsidian Community Plugins must go through the official review process, so until this plugin is listed there, install it manually:

### Step 1 — Locate your vault's plugin folder

In your file system, navigate to:

```
<your-vault>/.obsidian/plugins/
```

> On macOS/Linux the `.obsidian` folder is hidden. Use **Cmd+Shift+.** (Mac) or enable "Show hidden files" to see it.

### Step 2 — Copy the plugin files

Create a new folder called `section-word-count` inside `plugins/`, then copy these three files into it:

```
.obsidian/plugins/section-word-count/
├── manifest.json
├── main.js
└── styles.css
```

All three files are in this directory alongside this README.

### Step 3 — Enable the plugin in Obsidian

1. Open Obsidian
2. Go to **Settings → Community Plugins**
3. If prompted, disable Safe Mode (required for any community plugin)
4. Click **Reload plugins** (the circular arrow icon) or restart Obsidian
5. Find **Section Word Count** in the list and toggle it **on**

---

## Usage

Just write! Place your cursor anywhere inside a section (below a heading) and the word count appears at the end of that heading line. Move to a different section and the count follows.

The widget only appears in the **editor view** when the editor is focused — it won't clutter Reading Mode or exported documents.

---

## How Words Are Counted

Before counting, the following are stripped:

| Stripped | Example |
|---|---|
| YAML frontmatter | `---\ntitle: …\n---` |
| Fenced code blocks | ` ```python … ``` ` |
| Inline code | `` `variable` `` |
| HTML tags | `<br>`, `<span …>` |
| Markdown image syntax | `![alt](url)` |
| Markdown link URLs | `[text](url)` → counts "text" |
| Wikilinks | `[[Page\|Alias]]` → counts "Alias" |
| Emphasis markers | `**`, `_`, `~~`, `==` |
| Heading markers | `## ` at line start |
| Tags | `#tag` |
| Bullet/list markers | `- `, `1. ` |
| Blockquote markers | `> ` |

What remains is split on whitespace; the number of non-empty tokens is the word count.

---

## Compatibility

- Requires **Obsidian 1.0+** (CodeMirror 6 editor)
- Works on desktop and mobile

---

## Development

The plugin is a single vanilla-JS file with no build step required. If you want to iterate:

1. Edit `main.js` directly
2. In Obsidian: **Settings → Community Plugins → Section Word Count → reload** (or use the [Hot Reload](https://github.com/pjeby/hot-reload) community plugin)

If you prefer TypeScript, the logic maps 1:1 — rename to `main.ts`, add Obsidian/CM6 type imports from `obsidian` and `@codemirror/*`, and run `tsc` or your preferred bundler.
