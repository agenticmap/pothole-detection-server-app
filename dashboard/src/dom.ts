/**
 * Minimal DOM helpers.
 *
 * `el()` never accepts HTML — text goes in via textContent. That is deliberate
 * and load-bearing: the repair note is operator-supplied free text up to 2000
 * characters, and the API echoes it back in repair_history[].note alongside
 * user_email. Building that panel with template strings and innerHTML is exactly
 * how it becomes stored XSS. There is no escape hatch here on purpose.
 */

type Attrs = Record<string, string | number | boolean | undefined>;
type Child = Node | string | null | undefined | false;

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  children: Child[] = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === false) continue;
    if (key === 'class') node.className = String(value);
    else if (key === 'text') node.textContent = String(value);
    else node.setAttribute(key, String(value));
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

export function clear(node: Element): void {
  node.replaceChildren();
}

/**
 * A labelled value row, with tabular figures so columns don't jitter.
 *
 * `title` is for a field whose label cannot carry its own caveat — the panel's
 * classifier score is a saturating posterior, not a measure of certainty, and a
 * bare number invites the wrong reading. On the row rather than the value, so
 * hovering anywhere on the line shows it.
 */
export function field(
  label: string,
  value: string,
  mono = false,
  title?: string,
): HTMLElement {
  return el('div', { class: 'field', ...(title ? { title } : {}) }, [
    el('span', { class: 'field-label', text: label }),
    el('span', { class: mono ? 'field-value mono' : 'field-value', text: value }),
  ]);
}

export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** "1 device" / "3 devices" — a count that reads like a bug erodes trust in the data. */
export function plural(count: number, singular: string, pluralForm?: string): string {
  return `${count.toLocaleString()} ${count === 1 ? singular : (pluralForm ?? singular + 's')}`;
}
