/**
 * Map setup: basemap, the authenticated cluster source, and selection.
 */

import {
  Map as MapLibreMap,
  NavigationControl,
  Popup,
  ScaleControl,
  type ErrorEvent,
  type MapGeoJSONFeature,
  type MapLayerMouseEvent,
  type VectorTileSource,
} from 'maplibre-gl';
// Side-effect import: must run before any Map is constructed. See worker.ts.
import './worker.ts';
import { basemapStyle } from './basemap.ts';
import {
  BASE_INDIVIDUAL_FILTER,
  FRAMES_MAX_ZOOM,
  FRAMES_MIN_ZOOM,
  LAYER_AGGREGATE,
  LAYER_FRAMES,
  LAYER_INDIVIDUAL,
  LAYER_OBSERVATIONS,
  OBSERVATIONS_MAX_ZOOM,
  OBSERVATIONS_MIN_ZOOM,
  SOURCE_FRAMES,
  SOURCE_ID,
  SOURCE_LAYER,
  SOURCE_OBSERVATIONS,
  aggregateLayer,
  framesLayer,
  individualLayer,
  observationsLayer,
} from './layers.ts';
import { installTileAuthRecovery, refreshTokenCache, transformRequest } from './tile-auth.ts';
import { SEVERITY_TIERS, UNRATED_LABEL, severityLabel } from '../severity.ts';
import { el } from '../dom.ts';
import type { ExpressionSpecification, FilterSpecification } from '@maplibre/maplibre-gl-style-spec';
import { currentTheme, type Theme } from '../theme.ts';

/**
 * Above this zoom the server returns individual clusters; at or below it,
 * aggregated bins. Mirrors `tile_aggregate_max_zoom` in app/config.py.
 */
const AGGREGATE_MAX_ZOOM = 12;

/**
 * Source maxzoom. Deliberately NOT MapLibre's default of 22: left there, the map
 * would request a fresh tile from PostGIS at every integer zoom up to 22. At 14,
 * MapLibre fetches real tiles up to z14 and overzooms (client-side slices the
 * parent) above it, which halves the request count at street zooms while keeping
 * feature properties intact so clicks still work.
 *
 * It must stay ABOVE AGGREGATE_MAX_ZOOM — set it to 12 and the aggregated tiles
 * would be overzoomed forever and individual clusters would never appear.
 */
const SOURCE_MAX_ZOOM = 14;

/** Individual features only exist above AGGREGATE_MAX_ZOOM. */
const MIN_DETAIL_ZOOM = AGGREGATE_MAX_ZOOM + 1;

export interface MapView {
  lat: number;
  lon: number;
  zoom: number;
}

export interface PotholeMapOptions {
  container: HTMLElement;
  initialView: MapView;
  assetType: string;
  onSelect: (clusterId: string) => void;
  onViewChange: (view: MapView) => void;
  onError: (message: string) => void;
  /** Feature under the pointer, or null when it leaves. Drives the dock readout. */
  onHover?: (feature: Record<string, unknown> | null) => void;
}

export class PotholeMap {
  private readonly map: MapLibreMap;
  private assetType: string;
  private tileVersion = 0;
  /** Kept so the selection can be re-applied after setTiles or setStyle, both
   * of which discard feature state. */
  private selectedId: string | null = null;
  /**
   * Whether the raw-observation layer is drawn. Off by default: a cluster is the
   * unit of work an operator acts on, and 4,637 raw points would bury 25 of them.
   * The dock toggles it on when someone wants to see what was reported rather
   * than what corroborated.
   */
  private observationsVisible = false;
  /** Same reasoning as observationsVisible: 5,615 frames would bury 24 clusters. */
  private framesVisible = false;
  /** Reused rather than constructed per click, so only one can ever be open. */
  private readonly observationPopup = new Popup({
    closeButton: true,
    closeOnClick: true,
    maxWidth: '320px',
    className: 'observation-popup',
  });

  constructor(private readonly options: PotholeMapOptions) {
    this.assetType = options.assetType;

    this.map = new MapLibreMap({
      container: options.container,
      style: basemapStyle(currentTheme()),
      center: [options.initialView.lon, options.initialView.lat],
      zoom: options.initialView.zoom,
      attributionControl: { compact: true },
      // Attaches the bearer to /api/v1/* requests; see tile-auth.ts.
      transformRequest,
    });

    installTileAuthRecovery(this.map, () => this.reloadTiles());

    this.map.addControl(new NavigationControl({ showCompass: false }), 'bottom-right');
    this.map.addControl(new ScaleControl({ unit: 'metric' }), 'bottom-left');

    // Guarded for the same reason applyTheme's styledata handler is: flipping the
    // theme before the initial style has loaded runs setStyle first, whose handler
    // adds the source, and then this fires and throws "Source already exists" —
    // which aborts addClusterLayers partway and leaves the map with no cluster
    // layers at all. Reachable by toggling the theme within a second of signing in.
    this.map.on('load', () => {
      if (!this.map.getSource(SOURCE_ID)) this.addClusterLayers();
    });
    this.map.on('moveend', () => this.options.onViewChange(this.currentView()));
    this.map.on('error', (e: ErrorEvent) => {
      // 401s are handled by installTileAuthRecovery (refresh + refetch), since an
      // errored tile never retries itself. Anything else is logged rather than
      // left as a silently blank tile.
      const message = e.error?.message ?? 'Map error';
      // eslint-disable-next-line no-console
      console.warn('[map]', message);
      this.options.onError(message);
    });

    this.map.on('click', LAYER_INDIVIDUAL, (e: MapLayerMouseEvent) => {
      const feature = e.features?.[0];
      const clusterId = feature?.properties?.['cluster_id'];
      // Guarded because a parent tile can render aggregate features at a detail
      // zoom for a moment while the child tile loads.
      if (typeof clusterId === 'string') this.options.onSelect(clusterId);
    });

    // Clicking an aggregate zooms in. There is no server-side expansion-zoom
    // equivalent, so this is a fixed step toward the individual-feature zoom.
    this.map.on('click', LAYER_AGGREGATE, (e: MapLayerMouseEvent) => {
      this.map.easeTo({
        center: e.lngLat,
        zoom: Math.max(this.map.getZoom() + 2, MIN_DETAIL_ZOOM),
      });
    });

    // Raw observations open a read-only popup rather than the detail panel: an
    // observation is a sensor reading, not a work item, and there is no
    // /observations/{id} endpoint behind it. Everything it shows is already in
    // the tile, so the popup costs no request.
    this.map.on('click', LAYER_OBSERVATIONS, (e: MapLayerMouseEvent) => {
      const feature = e.features?.[0];
      if (!feature) return;
      this.observationPopup
        .setLngLat(e.lngLat)
        .setDOMContent(observationPopupContent(feature.properties ?? {}))
        .addTo(this.map);
    });

    this.map.on('mouseenter', LAYER_OBSERVATIONS, () => {
      this.map.getCanvas().style.cursor = 'pointer';
    });
    this.map.on('mouseleave', LAYER_OBSERVATIONS, () => {
      this.map.getCanvas().style.cursor = '';
    });

    this.map.on('click', LAYER_FRAMES, (e: MapLayerMouseEvent) => {
      const feature = e.features?.[0];
      if (!feature) return;
      this.observationPopup
        .setLngLat(e.lngLat)
        .setDOMContent(framePopupContent(feature.properties ?? {}))
        .addTo(this.map);
    });

    this.map.on('mouseenter', LAYER_FRAMES, () => {
      this.map.getCanvas().style.cursor = 'pointer';
    });
    this.map.on('mouseleave', LAYER_FRAMES, () => {
      this.map.getCanvas().style.cursor = '';
    });

    for (const layer of [LAYER_INDIVIDUAL, LAYER_AGGREGATE]) {
      this.map.on('mouseenter', layer, () => {
        this.map.getCanvas().style.cursor = 'pointer';
      });
      this.map.on('mouseleave', layer, () => {
        this.map.getCanvas().style.cursor = '';
      });
    }

    // Readout on the individual layer only: an aggregate bin has no severity or
    // device count to report, and showing its point_count in the same pill would
    // read as if one defect had 40 devices.
    this.map.on('mousemove', LAYER_INDIVIDUAL, (e: MapLayerMouseEvent) => {
      const properties = e.features?.[0]?.properties;
      if (properties) this.options.onHover?.(properties);
    });
    this.map.on('mouseleave', LAYER_INDIVIDUAL, () => this.options.onHover?.(null));
  }

  private tiles(): string[] {
    return [
      buildTileUrl('/api/v1/tiles/clusters/{z}/{x}/{y}.mvt', {
        asset_type: this.assetType,
        // Repaired clusters are shown deliberately, styled differently. Hiding
        // them would make a just-repaired marker vanish under the operator's
        // open panel, with no way back to un-repair it.
        include_repaired: 'true',
        // The operator tier wants the single-device triage queue, which the
        // public read path hides.
        min_devices: '0',
        v: String(this.tileVersion),
      }),
    ];
  }

  private observationTiles(): string[] {
    return [
      buildTileUrl('/api/v1/tiles/observations/{z}/{x}/{y}.mvt', {
        asset_type: this.assetType,
        v: String(this.tileVersion),
      }),
    ];
  }

  private frameTiles(): string[] {
    return [
      buildTileUrl('/api/v1/tiles/frames/{z}/{x}/{y}.mvt', {
        // No asset_type: asset_frame has no such column. A frame is a photograph
        // of the road, not an assertion about one asset class.
        v: String(this.tileVersion),
      }),
    ];
  }

  private addClusterLayers(): void {
    this.map.addSource(SOURCE_ID, {
      type: 'vector',
      tiles: this.tiles(),
      minzoom: 0,
      maxzoom: SOURCE_MAX_ZOOM,
      // cluster_id is TEXT so MVT carries no numeric feature id; promoting it
      // is what makes setFeatureState work for the optimistic repair update.
      promoteId: { [SOURCE_LAYER]: 'cluster_id' },
    });
    // The raw-observation source. `minzoom` here is not cosmetic: the endpoint
    // 400s below OBSERVATIONS_MIN_ZOOM, and MapLibre never retries an errored
    // tile, so without it panning at z14 would permanently poison tiles that
    // would otherwise have loaded on the way back up.
    this.map.addSource(SOURCE_OBSERVATIONS, {
      type: 'vector',
      tiles: this.observationTiles(),
      minzoom: OBSERVATIONS_MIN_ZOOM,
      maxzoom: OBSERVATIONS_MAX_ZOOM,
    });
    this.map.addSource(SOURCE_FRAMES, {
      type: 'vector',
      tiles: this.frameTiles(),
      minzoom: FRAMES_MIN_ZOOM,
      maxzoom: FRAMES_MAX_ZOOM,
    });
    // Observations first, so clusters paint over them. Corroborated evidence
    // should never be occluded by a single unconfirmed reading.
    // Frames beneath observations: a camera guess is the weakest evidence on the
    // map, and there are more of them than anything else.
    this.map.addLayer(framesLayer());
    this.map.addLayer(observationsLayer());
    this.map.addLayer(aggregateLayer());
    this.map.addLayer(individualLayer());
    this.map.setLayoutProperty(
      LAYER_OBSERVATIONS,
      'visibility',
      this.observationsVisible ? 'visible' : 'none',
    );
    this.map.setLayoutProperty(
      LAYER_FRAMES,
      'visibility',
      this.framesVisible ? 'visible' : 'none',
    );
    // Feature state does not survive a source rebuild, so restore it here —
    // this runs on first load, after setStyle, and after an asset-type change.
    this.applySelected(this.selectedId, true);
  }

  currentView(): MapView {
    const center = this.map.getCenter();
    return { lat: center.lat, lon: center.lng, zoom: this.map.getZoom() };
  }

  setAssetType(assetType: string): void {
    if (assetType === this.assetType) return;
    this.assetType = assetType;
    this.reloadTiles();
  }

  /**
   * Re-style for a theme change.
   *
   * The basemap is a whole flavor, not a handful of paint properties — dozens of
   * layers change — so this swaps the style rather than patching it.
   *
   * Two consequences worth knowing. `setStyle` discards imperatively added
   * sources and layers, so the cluster source has to be rebuilt; that rebuild is
   * also what repaints the markers, because layers.ts resolves tokens through
   * `cssVar` at call time and theme.ts sets `data-theme` synchronously before we
   * get here. And feature state goes with the source, so an optimistic repair
   * flag set by markRepairedOptimistically is dropped — after a theme flip the
   * next tile fetch is the source of truth, which is correct but is a behaviour
   * change rather than an accident.
   */
  applyTheme(theme: Theme): void {
    this.map.setStyle(basemapStyle(theme));
    // `styledata` rather than `style.load`: the latter does not fire on setStyle
    // in every path. Guarded because the event can arrive more than once.
    this.map.once('styledata', () => {
      if (!this.map.getSource(SOURCE_ID)) this.addClusterLayers();
    });
  }

  /** Current viewport as a lon/lat bbox, for the stats query. */
  bounds(): { minLon: number; minLat: number; maxLon: number; maxLat: number } {
    const b = this.map.getBounds();
    return {
      minLon: b.getWest(),
      minLat: b.getSouth(),
      maxLon: b.getEast(),
      maxLat: b.getNorth(),
    };
  }

  /**
   * Show only the selected severity tiers and a minimum corroboration level.
   *
   * Client-side rather than a tile parameter, because it has to be instant — a
   * chip that costs a network round trip stops feeling like a filter. It is also
   * the only way to express this: the tile route's `severity_min` cannot describe
   * a tier *band*, and cannot select unrated clusters at all, since a NULL
   * severity fails `>= $8`.
   *
   * The counts on the chips and in the legend deliberately do NOT come from here.
   * They come from the stats endpoint, so they stay exact regardless of what the
   * per-tile cap dropped. See stats.ts.
   *
   * Applies to the individual layer only. Aggregate bins carry neither attribute,
   * so filtering them would empty the low zooms rather than filter them; the dock
   * says so instead.
   */
  setClusterFilter(
    selectedTiers: ReadonlySet<string>,
    minDevices: number,
    sources: { selected: ReadonlySet<string>; all: boolean },
  ): void {
    if (!this.map.getLayer(LAYER_INDIVIDUAL)) return;

    const everyTier = selectedTiers.size === SEVERITY_TIERS.length + 1;
    if (everyTier && minDevices <= 1 && sources.all) {
      this.map.setFilter(LAYER_INDIVIDUAL, BASE_INDIVIDUAL_FILTER);
      return;
    }

    const clauses: ExpressionSpecification[] = [];
    if (selectedTiers.has(UNRATED_LABEL)) {
      // ST_AsMVT omits null attributes entirely, so "unrated" is an absent key.
      clauses.push(['!', ['has', 'severity']]);
    }
    for (const [i, tier] of SEVERITY_TIERS.entries()) {
      if (!selectedTiers.has(tier.label)) continue;
      const next = SEVERITY_TIERS[i + 1];
      const bounded: ExpressionSpecification[] = [
        ['has', 'severity'],
        ['>=', ['get', 'severity'], tier.min],
      ];
      if (next) bounded.push(['<', ['get', 'severity'], next.min]);
      clauses.push(['all', ...bounded]);
    }

    const parts: ExpressionSpecification[] = [
      BASE_INDIVIDUAL_FILTER,
      // An empty selection must hide everything, not fall through to showing
      // everything — `['any']` with no clauses evaluates false, which is right.
      ['any', ...clauses],
      ['>=', ['coalesce', ['get', 'distinct_devices'], 0], minDevices],
    ];
    // Skipped when every known source is on, so the common case adds no work.
    if (!sources.all) {
      parts.push(['in', ['get', 'source'], ['literal', [...sources.selected]]]);
    }
    this.map.setFilter(LAYER_INDIVIDUAL, ['all', ...parts] as FilterSpecification);
  }

  /**
   * Emphasise the open cluster: larger, with a dark ring (see layers.ts).
   *
   * Feature state rather than a separate highlight layer — one source of geometry,
   * and it costs no extra tile request. It is remembered because setTiles and
   * setStyle both drop feature state, so the selection would silently lose its
   * emphasis after a theme flip or an asset-type change.
   */
  setSelected(clusterId: string | null): void {
    if (this.selectedId === clusterId) return;
    this.applySelected(this.selectedId, false);
    this.selectedId = clusterId;
    this.applySelected(clusterId, true);
  }

  private applySelected(clusterId: string | null, selected: boolean): void {
    if (!clusterId) return;
    if (!this.map.getSource(SOURCE_ID)) return;
    this.map.setFeatureState(
      { source: SOURCE_ID, sourceLayer: SOURCE_LAYER, id: clusterId },
      { selected },
    );
  }

  /** MapLibre does not watch its container; the shell's ResizeObserver calls this. */
  resize(): void {
    this.map.resize();
  }

  /** Force a tile refetch (cache-busted). */
  reloadTiles(): void {
    this.tileVersion += 1;
    const source = this.map.getSource(SOURCE_ID);
    if (source && 'setTiles' in source) {
      (source as VectorTileSource).setTiles(this.tiles());
    }
    // The observation source needs the same treatment: it is fetched with the
    // same bearer, so a 401 recovery that refreshed only the cluster tiles would
    // leave the observation layer permanently blank.
    const observations = this.map.getSource(SOURCE_OBSERVATIONS);
    if (observations && 'setTiles' in observations) {
      (observations as VectorTileSource).setTiles(this.observationTiles());
    }
    const frames = this.map.getSource(SOURCE_FRAMES);
    if (frames && 'setTiles' in frames) {
      (frames as VectorTileSource).setTiles(this.frameTiles());
    }
  }

  /** Show or hide the camera-frame layer. Driven by the dock toggle. */
  setFramesVisible(visible: boolean): void {
    this.framesVisible = visible;
    if (!this.map.getLayer(LAYER_FRAMES)) return;
    this.map.setLayoutProperty(LAYER_FRAMES, 'visibility', visible ? 'visible' : 'none');
  }

  /** Show or hide the raw-observation layer. Driven by the dock toggle. */
  setObservationsVisible(visible: boolean): void {
    this.observationsVisible = visible;
    if (!this.map.getLayer(LAYER_OBSERVATIONS)) return;
    this.map.setLayoutProperty(
      LAYER_OBSERVATIONS,
      'visibility',
      visible ? 'visible' : 'none',
    );
  }

  /** Minimum zoom at which the observation layer has anything to show. */
  observationsMinZoom(): number {
    return OBSERVATIONS_MIN_ZOOM;
  }

  /**
   * Paint a cluster as repaired immediately, without waiting for a tile refetch.
   * Feature state is lost on setTiles, so this is used INSTEAD of a reload, not
   * alongside one.
   */
  markRepairedOptimistically(clusterId: string, repaired: boolean): void {
    this.map.setFeatureState(
      { source: SOURCE_ID, sourceLayer: SOURCE_LAYER, id: clusterId },
      { repaired },
    );
  }

  flyToCluster(lat: number, lon: number): void {
    this.map.easeTo({ center: [lon, lat], zoom: Math.max(this.map.getZoom(), MIN_DETAIL_ZOOM) });
  }

  /** True when the current zoom shows individual, clickable clusters. */
  isDetailZoom(): boolean {
    return this.map.getZoom() > AGGREGATE_MAX_ZOOM;
  }

  visibleFeatureCount(): number {
    if (!this.map.isStyleLoaded()) return 0;
    const features: MapGeoJSONFeature[] = this.map.queryRenderedFeatures({
      layers: [LAYER_INDIVIDUAL, LAYER_AGGREGATE],
    });
    return features.length;
  }

  onIdle(cb: () => void): void {
    this.map.on('idle', cb);
  }

  destroy(): void {
    this.map.remove();
  }
}

/**
 * Build an absolute tile template. MapLibre requires an absolute URL for a
 * source's `tiles`, and leaves the {z}/{x}/{y} placeholders alone.
 */
function buildTileUrl(path: string, params: Record<string, string>): string {
  const query = new URLSearchParams(params).toString();
  return `${location.origin}${path}?${query}`;
}

/**
 * Everything the observations tile carries about one reading, as a definition
 * list.
 *
 * Built with textContent throughout — `client_id` and `sensor_class` are values
 * the database holds, and this file has no business deciding they are safe to
 * parse as HTML.
 *
 * Unknown keys are rendered rather than dropped, so a column added to
 * _OBSERVATION_TILE_SQL shows up here without a frontend change. That is the
 * point of the popup: it is an inspector for the pipeline, not a curated card.
 */
function observationPopupContent(props: Record<string, unknown>): HTMLElement {
  const rows: Array<[string, string]> = [];

  const num = (v: unknown, digits: number): string | null =>
    typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : null;

  const outlier = props['sensor_is_outlier'] === true;

  rows.push(['Class', String(props['sensor_class'] ?? 'unscored')]);

  const p = num(props['sensor_p_pothole'], 3);
  if (p !== null) rows.push(['P(pothole)', p]);

  const severity = num(props['sensor_severity'], 3);
  if (severity !== null) {
    const label = severityLabel(props['sensor_severity'] as number);
    rows.push(['Severity', `${severity} · ${label}`]);
  }

  const speed = props['speed_mps'];
  if (typeof speed === 'number') {
    rows.push(['Speed', `${speed.toFixed(1)} m/s · ${(speed * 3.6).toFixed(0)} km/h`]);
  }

  const accuracy = num(props['accuracy_m'], 1);
  if (accuracy !== null) rows.push(['GPS accuracy', `${accuracy} m`]);

  const ts = props['ts_epoch'];
  if (typeof ts === 'number') {
    rows.push(['Recorded', new Date(ts * 1000).toISOString().replace('T', ' ').slice(0, 19) + 'Z']);
  }

  // Anything the tile gained that this function does not know about.
  const known = new Set([
    'client_id', 'sensor_class', 'sensor_p_pothole', 'sensor_severity',
    'sensor_is_outlier', 'speed_mps', 'accuracy_m', 'ts_epoch',
  ]);
  for (const [key, value] of Object.entries(props)) {
    if (!known.has(key)) rows.push([key, String(value)]);
  }

  const dl = el('dl', { class: 'observation-popup-grid' });
  for (const [term, value] of rows) {
    dl.append(el('dt', { text: term }), el('dd', { text: value }));
  }

  return el('div', {}, [
    el('h3', { class: 'observation-popup-title', text: 'Raw observation' }),
    // The outlier flag is the reason this layer is inspectable at all: it is
    // what the cluster member gate silently drops. Stated as a sentence rather
    // than a true/false row, because "sensor_is_outlier: true" does not tell an
    // operator that the reading was excluded from clustering.
    el('p', {
      class: outlier ? 'observation-popup-flag is-outlier' : 'observation-popup-flag',
      text: outlier
        ? 'Rejected by the outlier gate — excluded from clustering.'
        : 'Passed the outlier gate.',
    }),
    dl,
    el('p', {
      class: 'observation-popup-id',
      text: String(props['client_id'] ?? 'unknown id'),
    }),
  ]);
}

/**
 * One camera frame: what the detector scored it, and whether that reached fusion.
 *
 * The fusion outcome leads, for the same reason the outlier flag leads on an
 * observation. A frame scoring 0.9 that paired with nothing contributed nothing
 * to any cluster, and no arrangement of the score alone says so.
 */
function framePopupContent(props: Record<string, unknown>): HTMLElement {
  const rows: Array<[string, string]> = [];

  const num = (v: unknown, digits: number): string | null =>
    typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : null;

  const detected = props['detected'] === true;
  const paired = props['paired'] === true;

  const serverP = num(props['server_probability'], 3);
  rows.push(['Server p', serverP ?? 'not scored']);

  const deviceP = num(props['device_probability'], 3);
  if (deviceP !== null) rows.push(['On-device p', deviceP]);

  const model = props['server_model_id'];
  if (typeof model === 'string' && model) rows.push(['Model', model]);

  const boxes = props['server_box_count'];
  if (typeof boxes === 'number') rows.push(['Boxes found', String(boxes)]);

  const fused = num(props['fused_confidence'], 3);
  if (fused !== null) {
    rows.push(['Fused confidence', props['is_primary'] === true ? `${fused} (primary)` : fused]);
  }

  const ts = props['ts_epoch'];
  if (typeof ts === 'number') {
    rows.push(['Captured', new Date(ts * 1000).toISOString().replace('T', ' ').slice(0, 19) + 'Z']);
  }

  const known = new Set([
    'client_id', 'device_probability', 'server_probability', 'server_model_id',
    'server_box_count', 'detected', 'paired', 'is_primary', 'fused_confidence', 'ts_epoch',
  ]);
  for (const [key, value] of Object.entries(props)) {
    if (!known.has(key)) rows.push([key, String(value)]);
  }

  let status: string;
  let severe = false;
  if (!detected) {
    status = 'Not yet scored — the detection worker has not reached this frame.';
    severe = true;
  } else if (!paired) {
    status = 'Scored but unpaired — no sensor event matched, so it reached no cluster.';
    severe = true;
  } else {
    status = 'Paired with a sensor event and fused.';
  }

  const dl = el('dl', { class: 'observation-popup-grid' });
  for (const [term, value] of rows) {
    dl.append(el('dt', { text: term }), el('dd', { text: value }));
  }

  return el('div', {}, [
    el('h3', { class: 'observation-popup-title', text: 'Camera frame' }),
    el('p', {
      class: severe ? 'observation-popup-flag is-outlier' : 'observation-popup-flag',
      text: status,
    }),
    dl,
    el('p', {
      class: 'observation-popup-id',
      text: String(props['client_id'] ?? 'unknown id'),
    }),
  ]);
}

export { AGGREGATE_MAX_ZOOM, MIN_DETAIL_ZOOM, refreshTokenCache };
