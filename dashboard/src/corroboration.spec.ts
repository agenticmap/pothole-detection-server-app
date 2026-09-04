import { describe, expect, it } from 'vitest';
// Vite's ?raw import rather than node:fs, so this stays inside the browser type
// space — pulling in @types/node for one test would put `process` and friends
// within reach of application code, in a codebase that has none.
import configSource from '../../app/config.py?raw';
import { isCorroborated, MIN_DISTINCT_DEVICES, MIN_DISTINCT_PASSES } from './corroboration';

describe('isCorroborated', () => {
  it('is an OR, not an AND — one phone on three days counts', () => {
    // The paper's own validation was one phone driven on five days. Requiring two
    // devices AND three passes would score that campaign at zero.
    expect(isCorroborated(1, 3)).toBe(true);
    expect(isCorroborated(2, 1)).toBe(true);
  });

  it('rejects the shape every cluster in the corpus currently has', () => {
    expect(isCorroborated(1, 1)).toBe(false);
  });

  it('is inclusive at both floors', () => {
    expect(isCorroborated(MIN_DISTINCT_DEVICES, 0)).toBe(true);
    expect(isCorroborated(MIN_DISTINCT_DEVICES - 1, 0)).toBe(false);
    expect(isCorroborated(0, MIN_DISTINCT_PASSES)).toBe(true);
    expect(isCorroborated(0, MIN_DISTINCT_PASSES - 1)).toBe(false);
  });
});

/**
 * The map draws this distinction itself because the tile applies no corroboration
 * filter. That means the numbers live in two languages, and nothing but this test
 * stops them drifting into disagreeing about what "confirmed" means.
 */
describe('the floors match the server', () => {
  const readSetting = (name: string): number => {
    const match = configSource.match(new RegExp(`^\\s*${name}:\\s*int\\s*=\\s*(\\d+)`, 'm'));
    if (!match?.[1]) {
      // Not a skip: a guard that silently stops running is worse than no guard.
      throw new Error(`Could not read ${name} from app/config.py`);
    }
    return Number(match[1]);
  };

  it('uses the same device floor as cluster_min_distinct_devices', () => {
    expect(MIN_DISTINCT_DEVICES).toBe(readSetting('cluster_min_distinct_devices'));
  });

  it('uses the same pass floor as cluster_min_distinct_passes', () => {
    expect(MIN_DISTINCT_PASSES).toBe(readSetting('cluster_min_distinct_passes'));
  });
});
