/**
 * Keyboard bindings, and the legend rendered from the same data the dispatcher reads.
 *
 * **Two key maps, never one.** `1` means "this frame is a pothole" in verdict mode
 * and "draw the next box as a pothole" in box mode. The CLI keeps them in separate
 * branches with the reason written down: sharing the handler "would make the visible
 * legend a lie in one mode or the other". Here the legend is *generated* from the
 * binding list, so the two cannot drift even in principle.
 *
 * Digits are matched on `event.code`, not `event.key`. On AZERTY the unshifted number
 * row produces `&é"'(`, and this is a tool people sit with for hours.
 */

export interface Binding {
  /** `event.code` for digits, `event.key` (lowercased) otherwise. */
  match: (e: KeyboardEvent) => boolean;
  /** What the legend shows. */
  keyLabel: string;
  description: string;
  run: (e: KeyboardEvent) => void;
}

const code = (c: string) => (e: KeyboardEvent) => e.code === c;
const key = (k: string) => (e: KeyboardEvent) => e.key.toLowerCase() === k;
const anyKey =
  (...keys: string[]) =>
  (e: KeyboardEvent) =>
    keys.includes(e.key);

export interface VerdictActions {
  judge: (label: number) => void;
  tag: (text: string) => void;
  focusNote: () => void;
  toggleModelBoxes: () => void;
  move: (step: number, shift: boolean) => void;
  reload: () => void;
  /** View-only rotation, for the sideways legacy frames. Never persisted. */
  rotate: () => void;
}

/** Reason tags, from the CLI. A tag is a shortcut for the commonest notes. */
export const REASON_TAGS: Record<string, string> = {
  m: 'manhole',
  s: 'tar seal',
  g: 'grate',
  w: 'wet/shadow',
};

export function verdictBindings(a: VerdictActions): Binding[] {
  return [
    { match: code('Digit1'), keyLabel: '1', description: 'Pothole', run: () => a.judge(1) },
    { match: code('Digit0'), keyLabel: '0', description: 'Not a pothole', run: () => a.judge(0) },
    {
      match: key('u'),
      keyLabel: 'u',
      // Recorded as a decision, not left as a gap -- see migrations/010.
      description: 'Unsure',
      run: () => a.judge(-1),
    },
    ...Object.entries(REASON_TAGS).map(([k, text]) => ({
      match: key(k),
      keyLabel: k,
      description: `Tag "${text}"`,
      run: () => a.tag(text),
    })),
    { match: key('n'), keyLabel: 'n', description: 'Write a note', run: () => a.focusNote() },
    {
      match: key('b'),
      keyLabel: 'b',
      description: "Show the model's boxes",
      run: () => a.toggleModelBoxes(),
    },
    {
      match: anyKey('j', 'ArrowRight', 'ArrowDown'),
      keyLabel: 'j / →',
      description: 'Next frame',
      run: (e) => a.move(1, e.shiftKey),
    },
    {
      match: anyKey('k', 'ArrowLeft', 'ArrowUp'),
      keyLabel: 'k / ←',
      description: 'Previous frame',
      run: (e) => a.move(-1, e.shiftKey),
    },
    { match: key('r'), keyLabel: 'r', description: 'Reload the queue', run: () => a.reload() },
    {
      // `t` for turn -- `r` is reload in both maps, and a rotate that reloaded the
      // queue would be a spectacular way to lose a pass.
      match: key('t'),
      keyLabel: 't',
      description: 'Turn the frame 90°',
      run: () => a.rotate(),
    },
  ];
}

export interface BoxActions {
  setClass: (index: number) => void;
  save: () => void;
  submit: () => void;
  reload: () => void;
  deleteSelected: () => void;
  deselect: () => void;
  toggleModelBoxes: () => void;
  move: (step: number, shift: boolean) => void;
  jump: (to: 'start' | 'end') => void;
  /** View-only rotation. Drawing is disabled while it is non-zero. */
  rotate: () => void;
}

/**
 * Box mode's map. Separate from the verdict map on purpose.
 *
 * `1` means "this frame is a pothole" over there and "draw the next box as a
 * pothole" here. The CLI keeps them apart with the reason written down: sharing one
 * handler "would make the visible legend a lie in one mode or the other".
 */
export function boxBindings(a: BoxActions, classNames: readonly string[]): Binding[] {
  const classKeys: Binding[] = classNames.slice(0, 5).map((name, i) => ({
    match: code(`Digit${i + 1}`),
    keyLabel: String(i + 1),
    description: `Draw as ${name}`,
    run: () => a.setClass(i),
  }));

  return [
    ...classKeys,
    {
      match: anyKey('Enter'),
      keyLabel: 'Enter',
      description: 'Save and move on',
      run: () => a.save(),
    },
    { match: key('s'), keyLabel: 's', description: 'Submit every draft', run: () => a.submit() },
    { match: key('r'), keyLabel: 'r', description: 'Reload the queue', run: () => a.reload() },
    {
      match: anyKey('Delete', 'Backspace'),
      keyLabel: 'Del',
      description: 'Delete the selected box',
      run: () => a.deleteSelected(),
    },
    {
      match: anyKey('Escape'),
      keyLabel: 'Esc',
      description: 'Deselect',
      run: () => a.deselect(),
    },
    {
      match: key('b'),
      keyLabel: 'b',
      description: "Show the model's boxes",
      run: () => a.toggleModelBoxes(),
    },
    {
      match: anyKey('j', 'ArrowRight', 'ArrowDown'),
      keyLabel: 'j / →',
      // Navigation SAVES here, unlike verdict mode. Shift is the escape hatch.
      description: 'Save, then next (Shift: peek without saving)',
      run: (e) => a.move(1, e.shiftKey),
    },
    {
      match: anyKey('k', 'ArrowLeft', 'ArrowUp'),
      keyLabel: 'k / ←',
      description: 'Save, then previous (Shift: peek)',
      run: (e) => a.move(-1, e.shiftKey),
    },
    {
      match: anyKey('Home'),
      keyLabel: 'Home',
      description: 'First frame (records nothing)',
      run: () => a.jump('start'),
    },
    {
      match: anyKey('End'),
      keyLabel: 'End',
      description: 'Last frame (records nothing)',
      run: () => a.jump('end'),
    },
    {
      // Same key as verdict mode. Drawing is suppressed while the frame is turned --
      // a box drawn on a rotated view would be stored against the unrotated pixels.
      match: key('t'),
      keyLabel: 't',
      description: 'Turn the frame 90° (drawing pauses)',
      run: () => a.rotate(),
    },
  ];
}

/**
 * Should this event be ignored because the operator is typing?
 *
 * The CLI compared the target against its one textarea by identity. That breaks the
 * moment a band input or a seed field exists — and this module has both. Without it,
 * typing "s" in a note submits the whole batch.
 */
export function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return (
    tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable === true
  );
}

/** Run the first binding that matches. Returns true if one did. */
/**
 * Is the event aimed at a control the browser already handles?
 *
 * Without this, `Enter` on a focused Sign out button matches box mode's save
 * binding, `preventDefault()` cancels the sign-out, and the frame is saved
 * instead. `1` on any focused button records a verdict on a frame the operator
 * is not looking at.
 *
 * It is also what makes the shortcuts and normal Tab navigation coexist: the
 * alternative was calling `blur()` after every click, which worked but threw
 * focus to <body> on each activation, so a Tab user restarted from the top of
 * the document every time.
 */
function isInteractive(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  return !!el?.closest?.('button, a[href], [role="button"], summary');
}

export function dispatch(bindings: readonly Binding[], e: KeyboardEvent): boolean {
  if (isTyping(e.target)) return false;
  // Held keys must not write. judge() records a verdict and advances, so without
  // this a stuck `1` labels frames at the OS key-repeat rate -- and these labels
  // are the ground truth the promotion gate is judged on.
  if (e.repeat) return false;
  // Space and Enter belong to whatever control has focus.
  if ((e.key === 'Enter' || e.key === ' ') && isInteractive(e.target)) return false;
  // A modifier means the operator is talking to the browser, not to us.
  if (e.ctrlKey || e.metaKey || e.altKey) return false;
  for (const b of bindings) {
    if (b.match(e)) {
      e.preventDefault();
      b.run(e);
      return true;
    }
  }
  return false;
}
