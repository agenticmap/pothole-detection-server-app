/**
 * Map setup: basemap, the authenticated cluster source, and selection.
 */

import {
  Map as MapLibreMap,
  NavigationControl,
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
  LAYER_AGGREGATE,
  LAYER_INDIVIDUAL,
  SOURCE_ID,
  SOURCE_LAYER,
  aggregateLayer,
  individualLayer,
} from './layers.ts';
import { installTileAuthRecovery, refreshTokenCache, transformRequest } from './tile-auth.ts';
import { SEVERITY_TIERS, UNRATED_LABEL } from '../severity.ts';
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

    this.map.on('load', () => this.addClusterLayers());
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
    this.map.addLayer(aggregateLayer());
    this.map.addLayer(individualLayer());
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
  setClusterFilter(selectedTiers: ReadonlySet<string>, minDevices: number): void {
    if (!this.map.getLayer(LAYER_INDIVIDUAL)) return;

    const everyTier = selectedTiers.size === SEVERITY_TIERS.length + 1;
    if (everyTier && minDevices <= 1) {
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

    const filter: FilterSpecification = [
      'all',
      BASE_INDIVIDUAL_FILTER,
      // An empty selection must hide everything, not fall through to showing
      // everything — `['any']` with no clauses evaluates false, which is right.
      ['any', ...clauses],
      ['>=', ['coalesce', ['get', 'distinct_devices'], 0], minDevices],
    ];
    this.map.setFilter(LAYER_INDIVIDUAL, filter);
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

export { AGGREGATE_MAX_ZOOM, MIN_DETAIL_ZOOM, refreshTokenCache };
