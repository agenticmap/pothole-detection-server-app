/**
 * Copy MapLibre's worker into public/ so it can be served as a static asset.
 *
 * See src/map/worker.ts for why this is necessary — in short, MapLibre computes
 * its worker URL at runtime, so no bundler emits the chunk and the worker 404s.
 *
 * Copied on predev/prebuild rather than committed: node_modules stays the single
 * source of truth, so bumping maplibre-gl cannot leave a stale worker behind that
 * mismatches the main-thread build.
 */

import { copyFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const from = join(here, '..', 'node_modules', 'maplibre-gl', 'dist');
const to = join(here, '..', 'public', 'maplibre');

// maplibre-gl-shared.mjs is not optional: the worker imports it by relative path,
// so it has to sit beside the worker at the served URL.
const files = ['maplibre-gl-worker.mjs', 'maplibre-gl-shared.mjs'];

mkdirSync(to, { recursive: true });
for (const file of files) {
  copyFileSync(join(from, file), join(to, file));
}
console.log(`copied ${files.length} maplibre worker files -> public/maplibre/`);
