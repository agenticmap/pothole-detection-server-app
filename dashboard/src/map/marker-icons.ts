/**
 * Shape markers for the map, so the three kinds of thing on it are not all circles.
 *
 * WHY SHAPES. Everything on this map was a circle, and the channels were so nearly
 * identical that a sensor event and a cluster were indistinguishable: the events layer
 * painted its classes from `--severity-4` / `--severity-2`, so a pothole event was
 * **bit-identical** (`#8c491a`) to a Severe cluster, and a crack event to a Moderate
 * one. Frames and events shared opacity, stroke width and stroke opacity, and their
 * radii differed by half a pixel.
 *
 * The grammar now is one shape per kind of thing:
 *
 *   circle    a CLUSTER   — a corroborated defect, the primary object
 *   triangle  an EVENT    — one sensor reading, before clustering
 *   square    a FRAME     — one camera image
 *
 * Shape is the channel that survives greyscale, which is the test colour cannot pass.
 *
 * WHY PRE-TINTED IMAGES RATHER THAN SDF. `icon-color` only applies to SDF images, and
 * generating a true signed-distance field from a canvas is a real algorithm with soft
 * edges as the failure mode. The state space here is tiny — a shape times a handful of
 * colours times solid-or-hollow — so baking one small bitmap per state is exact, has
 * no edge artefacts, and lets `icon-image` pick with a plain `match` expression.
 *
 * THE TRAP. `setStyle` discards imperatively added images along with sources and
 * layers, and `applyTheme` calls `setStyle` on every theme flip. So registration has
 * to happen inside `addClusterLayers`, which is the one place that rebuilds after a
 * style swap — the same reasoning that already applies to the sources there. Getting
 * this wrong shows up as markers silently vanishing the first time someone toggles
 * dark mode.
 */

export type MarkerShape = 'triangle' | 'square';

/** Which classes and states get their own bitmap. Pure data — see `iconName`. */
export interface IconRole {
  name: string;
  shape: MarkerShape;
  /** Resolved colour. Baked into the bitmap, so a theme flip re-registers. */
  color: string;
  /** Outline only, for "this reading reached no cluster". */
  hollow: boolean;
}

/**
 * The icon name for one state. Kept pure and exported so the layer expressions and
 * the registration loop cannot disagree about what exists — a `match` naming an
 * unregistered image renders nothing at all, silently.
 */
export function iconName(shape: MarkerShape, role: string, hollow: boolean): string {
  return `rw-${shape}-${role}${hollow ? '-hollow' : ''}`;
}

/** Every event-class role, in the order `sensor_class` can take. */
export const EVENT_ROLES = ['pothole', 'crack', 'other'] as const;
/** Frame states: scored by the detector, or never looked at. */
export const FRAME_ROLES = ['scored', 'unscored'] as const;

export interface MarkerColors {
  eventPothole: string;
  eventCrack: string;
  eventOther: string;
  frameScored: string;
  frameUnscored: string;
}

/** The full set of bitmaps to register, derived from the colours. */
export function iconRoles(colors: MarkerColors): IconRole[] {
  const events: [string, string][] = [
    ['pothole', colors.eventPothole],
    ['crack', colors.eventCrack],
    ['other', colors.eventOther],
  ];
  const frames: [string, string][] = [
    ['scored', colors.frameScored],
    ['unscored', colors.frameUnscored],
  ];
  const out: IconRole[] = [];
  for (const [role, color] of events) {
    for (const hollow of [false, true]) {
      out.push({ name: iconName('triangle', role, hollow), shape: 'triangle', color, hollow });
    }
  }
  for (const [role, color] of frames) {
    for (const hollow of [false, true]) {
      out.push({ name: iconName('square', role, hollow), shape: 'square', color, hollow });
    }
  }
  return out;
}

/** Bitmap side, in CSS pixels before `icon-size`. */
const PX = 18;

/**
 * Draw one marker bitmap.
 *
 * Rendered at 2x and handed to MapLibre with `pixelRatio: 2`, so it stays crisp on a
 * retina display without being blurry on a 1x one. The dark outline is the same
 * device the box overlay uses: a bright shape on blown-out sky and a dark shape on
 * asphalt both need an edge that is not the fill colour.
 */
function drawIcon(role: IconRole, ratio: number): ImageData | null {
  const side = PX * ratio;
  const canvas = document.createElement('canvas');
  canvas.width = side;
  canvas.height = side;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;

  const pad = 2 * ratio;
  const w = side - pad * 2;
  ctx.beginPath();
  if (role.shape === 'triangle') {
    // Apex up. Inset so the stroke is not clipped by the bitmap edge.
    ctx.moveTo(side / 2, pad);
    ctx.lineTo(side - pad, side - pad);
    ctx.lineTo(pad, side - pad);
    ctx.closePath();
  } else {
    const r = 2 * ratio;
    ctx.roundRect(pad, pad, w, w, r);
  }

  if (!role.hollow) {
    ctx.fillStyle = role.color;
    ctx.fill();
  }
  // The dark under-edge first, then the colour on top: two strokes is what holds
  // against both ends of a photograph's range.
  ctx.lineWidth = 3 * ratio;
  ctx.strokeStyle = 'rgba(0,0,0,0.55)';
  ctx.stroke();
  ctx.lineWidth = 1.6 * ratio;
  ctx.strokeStyle = role.color;
  ctx.stroke();

  return ctx.getImageData(0, 0, side, side);
}

/**
 * Register every marker bitmap on the map, replacing any already there.
 *
 * Must be called from `addClusterLayers`, i.e. after every `setStyle` — see the
 * module header. `hasImage` is checked because `addImage` throws on a duplicate and
 * `styledata` can arrive more than once.
 */
export function registerMarkerIcons(
  map: {
    hasImage: (id: string) => boolean;
    removeImage: (id: string) => void;
    addImage: (id: string, image: ImageData, options?: { pixelRatio?: number }) => void;
  },
  colors: MarkerColors,
): void {
  const ratio = 2;
  for (const role of iconRoles(colors)) {
    const image = drawIcon(role, ratio);
    if (!image) continue;
    // Replace rather than skip: the colours are baked in, so a theme flip must
    // overwrite the old bitmaps or the markers keep the previous theme's palette.
    if (map.hasImage(role.name)) map.removeImage(role.name);
    map.addImage(role.name, image, { pixelRatio: ratio });
  }
}
