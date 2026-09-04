import { describe, expect, it } from 'vitest';
import { OBSERVATION_CLASSES, observationClassFilter } from './layers';

const set = (...classes: string[]) => new Set(classes);

describe('observationClassFilter', () => {
  it('returns null when everything is selected, so no filter is installed', () => {
    expect(observationClassFilter(set(...OBSERVATION_CLASSES))).toBeNull();
  });

  it('hides everything on an empty selection rather than showing everything', () => {
    // `['any']` with no clauses evaluates false. The opposite mistake — falling
    // through to no filter — is the trap setClusterFilter documents, and it would
    // turn "deselect all" into "show all 5,686".
    expect(observationClassFilter(set())).toEqual(['any']);
  });

  it('selects a single class by equality', () => {
    expect(observationClassFilter(set('pothole'))).toEqual([
      'any',
      ['==', ['coalesce', ['get', 'sensor_class'], 'not'], 'pothole'],
    ]);
  });

  it('treats "other" as everything that is not pothole or crack', () => {
    // The data value is `not`, but the icon expression buckets any unrecognised
    // class as `other` too. Matching on equality with `not` would let a future
    // fourth class render with no chip able to select it.
    const filter = observationClassFilter(set('other'));
    expect(filter).toEqual([
      'any',
      ['!', ['in', ['coalesce', ['get', 'sensor_class'], 'not'], ['literal', ['pothole', 'crack']]]],
    ]);
  });

  it('combines a partial selection with any', () => {
    const filter = observationClassFilter(set('pothole', 'crack'));
    expect(filter?.[0]).toBe('any');
    expect(filter).toHaveLength(3);
  });

  it('reads a missing class as "not", matching the icon expression', () => {
    // ST_AsMVT omits null attributes entirely, so sensor_class can be absent.
    for (const selection of [set('pothole'), set('other')]) {
      expect(JSON.stringify(observationClassFilter(selection))).toContain(
        '["coalesce",["get","sensor_class"],"not"]',
      );
    }
  });
});
