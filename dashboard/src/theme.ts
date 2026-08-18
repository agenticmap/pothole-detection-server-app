/**
 * Light / dark theme.
 *
 * The whole theme is a `data-theme` attribute on <html> plus the token block in
 * tokens.css — no component reads a colour directly, which is what makes this a
 * one-attribute switch.
 *
 * One consumer is NOT CSS, which is why callers must react to a change rather
 * than assume the attribute is enough: MapLibre paint cannot read CSS custom
 * properties, so severity.ts resolves them to literal hex when the layers are
 * built. Those layers have to be repainted or the markers keep the old palette —
 * and, more visibly, the halo stays light on a dark basemap. The shell threads
 * that through `ShellCallbacks.onThemeChange`; see map.ts::applyTheme.
 */

export type Theme = 'light' | 'dark';

const STORAGE_KEY = 'roadwatch.theme';

let current: Theme = 'light';

function systemPreference(): Theme {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function stored(): Theme | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === 'light' || value === 'dark' ? value : null;
  } catch {
    // Private browsing or a blocked origin — fall back to the system preference.
    return null;
  }
}

function apply(theme: Theme): void {
  current = theme;
  // Set synchronously, so a getComputedStyle read immediately afterwards (which
  // is exactly what severity.ts does when the map repaints) sees the new tokens.
  document.documentElement.dataset['theme'] = theme;
}

/** Resolve and apply the initial theme. Call once, before the first render. */
export function initTheme(): Theme {
  apply(stored() ?? systemPreference());
  return current;
}

export function currentTheme(): Theme {
  return current;
}

export function setTheme(theme: Theme): void {
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Preference simply won't survive a reload; not worth failing the toggle.
  }
  apply(theme);
}

export function toggleTheme(): Theme {
  setTheme(current === 'dark' ? 'light' : 'dark');
  return current;
}
