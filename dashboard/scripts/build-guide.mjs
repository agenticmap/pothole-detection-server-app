/**
 * Render the operator guide to a static page the console can link to.
 *
 * docs/guides/operator-console.md is the single source of truth. This renders it
 * into public/guide.html on predev/prebuild, exactly as copy-maplibre-worker.mjs
 * stages the worker: generated, gitignored, and copied into dist/ by Vite. A
 * second hand-maintained HTML copy would drift from the markdown within a round.
 *
 * `marked` is a devDependency and runs here only — the output is static HTML, so
 * nothing ships to the browser.
 *
 * ## The guide is served UNAUTHENTICATED
 *
 * app/main.py mounts dashboard/dist at /dashboard with StaticFiles, which does no
 * auth. /dashboard/guide.html is therefore readable by anyone who can reach the
 * server, the same as index.html. That is fine for a document describing how the
 * console works — but it is why the guide must never carry credentials, corpus
 * data, or anything about a specific city. Keep it about behaviour.
 */

import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { marked } from 'marked';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..', '..');
const source = join(repoRoot, 'docs', 'guides', 'operator-console.md');
const publicDir = join(here, '..', 'public');
const target = join(publicDir, 'guide.html');

let markdown;
try {
  markdown = readFileSync(source, 'utf8');
} catch (cause) {
  // Fail the build rather than emit an empty page: a Help link to a blank
  // document is worse than a build that tells you what is missing.
  throw new Error(
    `Cannot build the operator guide: ${source} is unreadable.\n` +
      `The console's Help link renders from that file. Restore it, or remove the\n` +
      `build-guide step from package.json if the guide is being retired.`,
    { cause },
  );
}

// Strip the `updated:` front matter the docs/ convention puts on every file, and
// keep the date to show in the footer.
const frontMatter = markdown.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n/);
let updated = null;
if (frontMatter) {
  updated = frontMatter[1].match(/updated:\s*(\S+)/)?.[1] ?? null;
  markdown = markdown.slice(frontMatter[0].length);
}

// Relative links point at other markdown files in the repo, which are not served.
// Render them as plain text so the reader is not handed a link that 404s.
// marked 18 passes a token object and expects the renderer to call back into the
// parser for inline content, so extend Renderer and delegate rather than hand-roll.
class GuideRenderer extends marked.Renderer {
  // marked stopped emitting heading ids in v5, so the guide's own "skip to
  // Reading the numbers" links had nothing to land on. Slugify the same way
  // GitHub does, so an anchor works both here and in the markdown source.
  heading(token) {
    const text = this.parser.parseInline(token.tokens);
    const id = text
      .replace(/<[^>]*>/g, '')
      .toLowerCase()
      .replace(/[^\w\s-]/g, '')
      .trim()
      .replace(/\s+/g, '-');
    return `<h${token.depth} id="${id}">${text}</h${token.depth}>\n`;
  }

  link(token) {
    if (/^(https?:|#)/.test(token.href)) return super.link(token);
    // Keep the words, drop the link. `<code>` because these are all file paths —
    // but most of this guide's doc links are already backticked, so unwrap an
    // existing <code> rather than nesting two of them and double-styling.
    const inner = this.parser.parseInline(token.tokens);
    const text = inner.replace(/^<code>([\s\S]*)<\/code>$/, '$1');
    return `<code class="doc-ref">${text}</code>`;
  }
}
const renderer = new GuideRenderer();

const body = marked.parse(markdown, { renderer });

const html = `<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Operator console — user guide · RoadWatch</title>
<link rel="stylesheet" href="guide-tokens.css">
<style>
  body {
    margin: 0;
    padding: 3rem 1.5rem 6rem;
    background: var(--color-surface, #fff);
    color: var(--color-text, #1a1a1a);
    font-family: var(--font-body, system-ui, sans-serif);
    line-height: 1.65;
  }
  main { max-width: 46rem; margin: 0 auto; }
  h1, h2, h3 { line-height: 1.25; margin-top: 2.5em; }
  h1 { margin-top: 0; }
  h2 { border-bottom: 1px solid var(--color-border, #ddd); padding-bottom: .3em; }
  a { color: var(--color-accent-2, #2b6cb0); }
  code {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: .9em;
    background: var(--color-surface-sunken, rgba(0,0,0,.05));
    padding: .1em .35em;
    border-radius: 4px;
  }
  pre { background: var(--color-surface-sunken, rgba(0,0,0,.05)); padding: 1rem; overflow-x: auto; border-radius: 8px; }
  pre code { background: none; padding: 0; }
  table { border-collapse: collapse; width: 100%; margin: 1.5em 0; }
  th, td { border: 1px solid var(--color-border, #ddd); padding: .5em .7em; text-align: left; vertical-align: top; }
  th { background: var(--color-surface-sunken, rgba(0,0,0,.04)); }
  blockquote {
    margin: 1.5em 0;
    padding: .1em 1.2em;
    border-left: 3px solid var(--color-accent, #c96a2b);
    background: var(--color-surface-sunken, rgba(0,0,0,.03));
  }
  hr { border: 0; border-top: 1px solid var(--color-border, #ddd); margin: 3em 0; }
  .doc-ref { white-space: nowrap; }
  .guide-foot { max-width: 46rem; margin: 4rem auto 0; padding-top: 1.5rem;
    border-top: 1px solid var(--color-border, #ddd);
    font-size: .875rem; color: var(--color-text-subtle, #666); }
</style>
</head>
<body>
<main>${body}</main>
<footer class="guide-foot">
  <a href="./">← Back to the console</a>${updated ? ` · Last updated ${updated}` : ''}
</footer>
<script>
  // Match whatever theme the operator picked in the console. Same origin, same key
  // as theme.ts; this page has no toggle of its own on purpose.
  try {
    var t = localStorage.getItem('roadwatch.theme');
    if (t !== 'light' && t !== 'dark') {
      t = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    document.documentElement.dataset.theme = t;
  } catch (e) { /* blocked storage: the light default already applied */ }
</script>
</body>
</html>
`;

mkdirSync(publicDir, { recursive: true });
// Copy rather than inline, so the guide and the console cannot disagree about a colour.
copyFileSync(join(here, '..', 'src', 'tokens.css'), join(publicDir, 'guide-tokens.css'));
writeFileSync(target, html, 'utf8');
console.log(`rendered operator guide -> public/guide.html (${html.length} bytes)`);
