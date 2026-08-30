/**
 * Tell MapLibre where its Web Worker lives.
 *
 * Import this for its side effect, before any Map is constructed.
 *
 * ## Why this file exists
 *
 * MapLibre 6 ships its worker as a separate ES module and locates it at runtime:
 *
 *     const file = url.endsWith('-dev.mjs') ? 'maplibre-gl-worker-dev.mjs' : 'maplibre-gl-worker.mjs';
 *     return new URL(`./${file}`, import.meta.url).href;
 *
 * That URL is *computed*, not a literal `new URL('./worker.mjs', import.meta.url)`
 * expression, so no bundler can statically detect it — Vite emits no worker chunk
 * and rewrites nothing. The bundled app then asks for
 * `assets/maplibre-gl-worker.mjs` next to its own entry chunk, gets a 404, and the
 * worker never boots.
 *
 * The failure is silent and total. Vector tiles are parsed in the worker, so every
 * vector source sits in `state: 'loading'` for ever: no error, no console warning,
 * and **zero network requests for tiles**. A map with a working basemap and no
 * markers looks exactly like a map with no data.
 *
 * This is worth stating plainly because it was previously misdiagnosed, and the
 * wrong conclusion was written into docs/phases/phase-2.5-dashboard-plan.md: that
 * MapLibre's `addProtocol` "does not work for vector tiles" because the worker
 * never consults the main thread. It does — MapLibre's own docs describe workers
 * delegating unknown protocols to the main thread, and PMTiles is their canonical
 * example. The evidence that supposedly proved otherwise (MapLibre's *own* demo
 * vector source hanging identically) was in fact the tell: nothing vector-based
 * worked, because the worker did not exist.
 *
 * ## The fix
 *
 * Serve the worker and the shared chunk it imports as ordinary static assets and
 * point MapLibre at them. `scripts/copy-maplibre-worker.mjs` copies both out of
 * node_modules into `public/maplibre/` on `predev` and `prebuild`, so the files
 * are never vendored into the repo and cannot drift from the installed version.
 *
 * `BASE_URL` rather than a hardcoded '/dashboard/': the bundle is served under a
 * base path, and the worker's own `import './maplibre-gl-shared.mjs'` resolves
 * relative to whatever URL we give here — so it has to be the real one.
 */

import { setWorkerUrl } from 'maplibre-gl';

setWorkerUrl(`${import.meta.env.BASE_URL}maplibre/maplibre-gl-worker.mjs`);
