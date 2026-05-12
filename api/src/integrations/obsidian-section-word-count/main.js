/**
 * Section Word Count — Obsidian Plugin
 *
 * Uses CodeMirror 6 ViewPlugin + DecorationSet to inject a live word-count
 * widget at the end of the heading that owns the cursor's current section.
 *
 * Compatible with Obsidian 1.0+ (CM6 editor).
 */

'use strict';

/* ─── Obsidian / CM6 imports ──────────────────────────────────────────────── */
const { Plugin } = require('obsidian');

const {
  ViewPlugin,
  Decoration,
  WidgetType,
} = require('@codemirror/view');

const { RangeSetBuilder } = require('@codemirror/state');

/* ─── Helpers ─────────────────────────────────────────────────────────────── */

/**
 * Parse the heading level from a line of raw Markdown text.
 * Returns 1-6, or 0 if the line is not a heading.
 */
function headingLevel(lineText) {
  const m = lineText.match(/^(#{1,6})\s/);
  return m ? m[1].length : 0;
}

/**
 * Strip everything that isn't "real" prose before counting words.
 *
 * Removed:
 *  - YAML front-matter  (--- … ---)
 *  - Fenced code blocks (``` … ```)
 *  - Inline code        (`…`)
 *  - HTML tags          (<…>)
 *  - Markdown link/image syntax  [text](url) → keep "text"
 *  - Emphasis / bold markers     * _ ~ =
 *  - Heading markers             # at line start
 *  - Horizontal rules
 *  - Wikilinks  [[target|alias]] → keep alias or target
 *  - Tags  #tag
 */
function stripMarkdown(text) {
  // Fenced code blocks
  text = text.replace(/```[\s\S]*?```/g, ' ');
  text = text.replace(/~~~[\s\S]*?~~~/g, ' ');
  // Inline code
  text = text.replace(/`[^`]*`/g, ' ');
  // HTML tags
  text = text.replace(/<[^>]+>/g, ' ');
  // Wikilinks: keep alias if present, otherwise target
  text = text.replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, '$2');
  text = text.replace(/\[\[([^\]]+)\]\]/g, '$1');
  // Markdown images: drop alt+url
  text = text.replace(/!\[[^\]]*\]\([^)]*\)/g, ' ');
  // Markdown links: keep link text
  text = text.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
  // Heading markers at line start
  text = text.replace(/^#{1,6}\s+/gm, '');
  // Bold / italic / strikethrough / highlight markers
  text = text.replace(/[*_~=]{1,3}/g, ' ');
  // Horizontal rules
  text = text.replace(/^[-*_]{3,}\s*$/gm, ' ');
  // Tags (#tag)  — lone hash-words
  text = text.replace(/\B#[^\s#]+/g, ' ');
  // Bullet / numbered list markers
  text = text.replace(/^[\s]*[-+*]\s+/gm, ' ');
  text = text.replace(/^[\s]*\d+[.)]\s+/gm, ' ');
  // Blockquote markers
  text = text.replace(/^>\s*/gm, ' ');

  return text;
}

/**
 * Count the words in a cleaned string.
 * Splits on any run of whitespace; filters empty tokens.
 */
function countWords(text) {
  const cleaned = stripMarkdown(text);
  return cleaned
    .split(/\s+/)
    .filter(t => t.length > 0)
    .length;
}

/**
 * Given a CM6 Text doc and a cursor position (character offset), find:
 *   - the 1-based CM6 line number of the heading that owns this section
 *   - the word count for that section's body text
 *
 * Uses the CM6 doc API directly (doc.line, doc.lineAt, doc.lines) so line
 * boundaries are always correct regardless of line-ending style.
 *
 * Returns null if the cursor is not inside any headed section.
 */
function getSectionInfo(doc, cursorPos) {
  const totalLines = doc.lines; // CM6 doc.lines = total line count (1-based max)

  // Detect and skip YAML front-matter
  // Front-matter: first line is "---", closed by a later "---" or "..."
  let bodyStartLine = 1; // 1-based
  if (totalLines >= 1 && doc.line(1).text.trim() === '---') {
    for (let i = 2; i <= totalLines; i++) {
      const t = doc.line(i).text.trim();
      if (t === '---' || t === '...') {
        bodyStartLine = i + 1;
        break;
      }
    }
  }

  // Find the 1-based line number the cursor is on
  const cursorLineObj = doc.lineAt(cursorPos);
  const cursorLineNum = cursorLineObj.number; // 1-based

  // Walk backwards from the cursor line to find the nearest heading
  let sectionHeadingLineNum = -1;
  let sectionLevel = 0;

  for (let i = cursorLineNum; i >= bodyStartLine; i--) {
    const lvl = headingLevel(doc.line(i).text);
    if (lvl > 0) {
      sectionHeadingLineNum = i;
      sectionLevel = lvl;
      break;
    }
  }

  if (sectionHeadingLineNum === -1) {
    // Cursor is above all headings, or the document has no headings
    return null;
  }

  // Collect body lines: from the line after the heading until the next
  // heading of equal or higher level (lower number = higher level).
  const bodyParts = [];
  for (let i = sectionHeadingLineNum + 1; i <= totalLines; i++) {
    const lineText = doc.line(i).text;
    const lvl = headingLevel(lineText);
    if (lvl > 0 && lvl <= sectionLevel) {
      // Hit a sibling or parent heading — section ends here
      break;
    }
    bodyParts.push(lineText);
  }

  const wordCount = countWords(bodyParts.join('\n'));

  return {
    // CM6 uses 1-based line numbers; return 1-based so callers can use doc.line() directly
    headingLineNum: sectionHeadingLineNum,
    wordCount,
  };
}

/* ─── CM6 Widget ──────────────────────────────────────────────────────────── */

class WordCountWidget extends WidgetType {
  constructor(count) {
    super();
    this.count = count;
  }

  eq(other) {
    return other.count === this.count;
  }

  toDOM() {
    const span = document.createElement('span');
    span.className = 'section-word-count-widget';
    const label = this.count === 1 ? 'word' : 'words';
    span.textContent = `· ${this.count.toLocaleString()} ${label}`;
    return span;
  }

  ignoreEvent() {
    return true;
  }
}

/* ─── CM6 ViewPlugin ──────────────────────────────────────────────────────── */

const sectionWordCountPlugin = ViewPlugin.fromClass(
  class {
    constructor(view) {
      this.decorations = this.buildDecorations(view);
    }

    update(update) {
      // Rebuild whenever the document or selection changes
      if (
        update.docChanged ||
        update.selectionSet ||
        update.focusChanged
      ) {
        this.decorations = this.buildDecorations(update.view);
      }
    }

    buildDecorations(view) {
      const builder = new RangeSetBuilder();

      // Only decorate when the editor is focused — avoids stale widgets in
      // reading / preview mode or unfocused panes.
      if (!view.hasFocus) {
        return builder.finish();
      }

      const doc = view.state.doc;
      // Primary cursor position (character offset)
      const cursorPos = view.state.selection.main.head;

      const info = getSectionInfo(doc, cursorPos);
      if (!info) {
        return builder.finish();
      }

      // headingLineNum is already 1-based (CM6 convention)
      const cm6Line = doc.line(info.headingLineNum);

      // Place the widget at the very end of the heading line
      const insertPos = cm6Line.to;

      builder.add(
        insertPos,
        insertPos,
        Decoration.widget({
          widget: new WordCountWidget(info.wordCount),
          side: 1,   // render after the position (end of line)
        })
      );

      return builder.finish();
    }
  },
  {
    decorations: v => v.decorations,
  }
);

/* ─── Plugin class ────────────────────────────────────────────────────────── */

class SectionWordCountPlugin extends Plugin {
  async onload() {
    console.log('Section Word Count: loading');

    // Register the CM6 editor extension
    this.registerEditorExtension(sectionWordCountPlugin);
  }

  onunload() {
    console.log('Section Word Count: unloaded');
  }
}

module.exports = SectionWordCountPlugin;
