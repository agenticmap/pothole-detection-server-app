---
updated: 2026-09-03
---

# The operator console — a user guide

RoadWatch's console has two jobs, and they are different work for different moments:

- **Map** — look at what the system thinks it has found, decide what is worth repairing, and record
  that a repair happened.
- **Frame review** — look at photographs the camera captured and tell the system what is actually in
  them. This is what makes the detector better.

This guide covers both. You do not need to read it in order; the map half and the review half stand
alone. If you only want to know what a number means, skip to
[Reading the numbers](#reading-the-numbers). If something looks broken, skip to
[When something looks wrong](#when-something-looks-wrong) — most of the surprising behaviour is
deliberate and has a reason.

> **A note on numbers.** This guide deliberately contains no counts of frames, defects or labels.
> Those change every day, and a guide that quotes them is wrong within a week. Where a figure
> matters, the guide points at the place on screen that shows you the current one.

---

## Before you start

### What the console is, and what it is not

Municipal asset software generally runs **Collect → Assess → Prioritise → Act → Track**. This
console covers Collect, Assess and Prioritise. It does not yet do Act or Track.

That is why the module rail on the left has six entries and only two of them work. **Map** and
**Frame review** are live. **Inventory**, **Work orders**, **Reports** and **Admin** are visible but
disabled, each with a tooltip saying "not yet available". They are placeholders for work that has
not been built, not buttons that are broken.

### What you need

- **A browser with WebGL2.** The map is a hardware-accelerated vector map. Without WebGL2 you get a
  page reading *"This dashboard needs WebGL2, which this browser or graphics driver does not
  provide. This is common over remote desktop or in a virtual machine."* That message is literal —
  if you are connecting over RDP, try running the browser locally instead.
- **An account.** There is no sign-up. An administrator creates accounts with
  `scripts/create_staff.py`, and **each operator needs their own** — repairs are recorded against
  the user who made them, so a shared login destroys the audit trail.

### Roles

There are three, ranked: `viewer` < `staff` < `admin`.

| | viewer | staff | admin |
|---|:---:|:---:|:---:|
| See the map, filters, legend, totals | ✅ | ✅ | ✅ |
| Open a defect, see its photographs | ✅ | ✅ | ✅ |
| Read the review queue, page through frames | ✅ | ✅ | ✅ |
| Record a verdict, draw boxes, submit | — | ✅ | ✅ |
| Mark a defect repaired or reopen it | — | ✅ | ✅ |
| Withdraw someone else's submitted boxes | — | — | ✅ (no button) |

Where you cannot do something, the console tells you rather than showing a control that fails: the
detail panel says *"Marking repairs requires the staff role."* and review says *"Read-only:
recording a verdict needs the staff role."*

> ### ⚠️ Marking repairs currently needs `admin`, not `staff`
>
> Defects are owned by an organisation, and repairing an **unowned** defect requires `admin`. The
> clustering job does not currently assign an owner, so **every defect the pipeline produces is
> unowned** and a `staff` account cannot repair any of them.
>
> When this bites, the message you get is *"Your account no longer has permission to do that"*,
> which misdescribes the cause — your account is fine. Use an `admin` account until this is fixed.
> Recorded in
> [`phase-2.11-console-legibility.md`](../phases/phase-2.11-console-legibility.md).

---

## Signing in and getting around

Sign in with your email and password. If you are away for a while you will land back on the login
screen with *"Your session expired. Please sign in again."* — that is a timeout, not an error, and
nothing you saved is lost.

**The top bar**, left to right: the RoadWatch mark; an **asset type** selector (only Potholes is
active — Signs, Streetlights and Crosswalks are marked "(soon)"); a **count tag** reading e.g.
`95 open defects`, which becomes `66 of 95 open defects` when a filter is hiding some; your email
and `org · role`; a **theme toggle** (its label names what clicking will *give* you, so "Dark mode"
means click for dark); **Help**, which opens this guide; and **Sign out**.

**Switching modules keeps your place.** The map is never torn down when you go to review, so coming
back is instant and your viewport is where you left it. A half-finished review pass survives a trip
to the map too.

### Send a colleague a link to exactly what you are looking at

This is the most useful thing in the console that nothing on screen advertises. **The address bar
tracks your state** — map position and zoom, which defect is open, which module you are in, and in
review the band, ordering and current frame.

So you can copy the URL and send it, and your colleague opens the same defect, or the same
photograph, not just the same app. It works across modules: a link captured in review still restores
the map viewport when they switch back.

---

# Part 1 — The map

## Reading the markers

Everything on the map is one of three kinds of record, and **the shape tells you which**. Shape
carries the meaning rather than colour because colour is already spoken for by severity — and
because shape still works in greyscale, or for a colour-blind operator.

| shape | what it is |
|---|---|
| ● **circle** | A **defect** — one or more readings the system has grouped into a candidate |
| ▲ **triangle** | A single **sensor reading** — one jolt the phone's accelerometer recorded |
| ■ **square** | A single **camera frame** — one photograph |

**One rule spans all three shapes: solid means it fed something, hollow means it fed nothing.**

- A **hollow triangle** is a reading that reached no defect. Most readings are hollow, and that is
  normal — see below.
- A **hollow square** is a photograph that matched no jolt.
- A **hollow circle** is a defect **nothing has corroborated yet**. On the data collected so far
  that is *every* defect, which is the same fact the **Corroborated** card reports as `0 of N`.

Two more modifiers:

- **Grey** means "no useful class": a grey triangle is a reading the classifier put in neither the
  pothole nor the crack group, and a grey square is a photograph the detector has not scored yet.
  (A reading is *always* scored — there is no "not yet scored" state for a triangle.)
- **A large circle with a number in it** is not one defect — it is *that many* defects grouped
  because you are zoomed out. Click it to zoom in.

> **Only pothole-classed readings can ever become a defect.** Crack and other readings are shown so
> you can check the classifier's work, but they are never eligible for grouping, so they are always
> hollow. That is why the Class filter below starts on **Pothole** alone.

Colour on a circle is **severity**. The **legend**, bottom-right, is grouped into *Severity*, *What
it is* and *Hollow means*, and is always on screen. On a narrow window it collapses to swatches with
the labels moved into tooltips.

## Zoom decides what exists

This surprises people, so it is worth stating plainly: **zoom level changes which layers exist at
all**, not just how big things look.

| zoom | what you see |
|---|---|
| **12 and below** | Grouped bubbles with counts. No individual defects. |
| **13 and above** | Individual defects, with severity colour and filters working. |
| **15 and above** | Raw sensor readings and camera frames, if you switch them on. |

Below zoom 13 you will see a banner reading *"Zoom in past level 13 to see individual potholes."*
and **every filter is disabled**, with the dock explaining: *"Filters apply at street zoom — grouped
markers carry no severity or device count."* That is true — the grouped bubbles genuinely do not
carry those fields, so there is nothing to filter on.

## The dock

The panel on the left. It collapses to a bar with the `‹` button if you want the map wider.

### The five numbers at the top

| Card | What it means |
|---|---|
| **Open defects in view** | Defect candidates in the current viewport, not repaired. |
| **Corroborated** | Of those, how many have been **confirmed by more than one look** — shown as a ratio like `0 of 204`. See below. |
| **Rated severe** | Share in the top severity tier. Shows `—` below ten defects, because a percentage of a tiny number misleads. |
| **Mean confidence** | Average confidence across the defects in view. |
| **Repaired this month** | Repairs recorded in the last 30 days. |

**Corroborated is the one to understand.** Creating a defect takes a single reading. *Publishing*
one — serving it to the public API and the mobile app — takes corroboration: two different devices,
or three separate passes. The two numbers are deliberately different, and the ratio shows you the
gap. `0 of 204` means the system has 204 candidates and **none of them has been seen twice**. That
is not a bug in the console; it is a fact about the data collected so far.

**Every card's change indicator is an em-dash.** There is no month-ago baseline to compare against,
so rather than invent a trend the console shows a dash and explains why in the tooltip. Same for
**Find a street**, which is disabled: defects carry coordinates, not street names, and the field says
so rather than being quietly removed.

### The filters

- **Severity tier** — chips for Low, Moderate, High, Severe and Unrated, each with a live count.
  Multi-select. **You cannot switch off the last active one**, because that would blank the map with
  no obvious way back.
- **Detection source** — how the defect was found. In practice everything is `crowd` today, so
  expect a single chip. The group hides itself entirely when there is nothing in view.
- **Corroboration** — `Any`, `2+ devices`, `4+ devices`. Pick one.
- **Raw detections** — two checkboxes, **both off by default**: *Sensor observations* and *Camera
  frames*. These show individual readings before grouping, including the ones that were rejected.
  They need **zoom 15 or more**; the dock says so. They are off by default because there are far more
  of them than there are defects, and at street zoom they bury the thing you came to look at.
- **Class** — appears under *Sensor observations* once that layer is on: `Pothole`, `Crack`,
  `Other`. **Pothole only, by default**, because those are the only readings that can form a defect;
  the great majority of what the phone records is crack or other. Switch the others on when you want
  to audit what the classifier is doing rather than what reached the map.

At the bottom, a **hover readout**: point at any marker and it reads out that record's severity,
device count, passes and observation count without opening anything. It stays visible when the dock
is collapsed.

## Clicking things

- **A defect (circle)** → opens the detail panel on the right.
- **A grouped bubble** → zooms in. It has nothing else to show.
- **A sensor reading (triangle)** → a small read-only popup titled **Raw observation**. It opens
  with two separate sentences, and they answer different questions:
  1. **Did it feed a defect?** — *"Fed defect clu_…"* or *"Fed no defect — this reading reached no
     cluster."* This is what the marker's fill shows.
  2. **Were the measurement conditions ordinary?** — *"Ordinary measurement conditions."* or
     *"Outlier: unusual measurement conditions (speed or road noise), not how often it was seen."*

  Then the class, `P(pothole)`, severity, speed, GPS accuracy and time. **The two sentences are
  independent**: a reading can be flagged as an outlier and still feed a defect, if a camera frame
  matched it confidently.
- **A camera frame (square)** → a popup with **the photograph**, a sentence saying where the frame
  stands (*not yet scored* / *scored but unpaired* / *paired with a sensor event and fused*), the
  scores, and an **Open full size** button.

## The detail panel

Opened by clicking a defect. The heading is the **coordinates**, not a street name — the system does
not know street names, and inventing one for a screen a crew gets dispatched from would be worse
than showing none.

1. **Badges** — Open or Repaired, and the severity tier with its number.
2. **The facts** — corroborating devices, corroborating passes, observation count, confidence, last
   seen, source, and the location.
   **Below them, when a defect has fewer than two passes, a sentence explaining what is missing** —
   for example *"All observations within 12 s — one drive-past, not repeat corroboration."* Read
   that sentence. A defect built from four readings taken twelve seconds apart is one car going
   past one rough patch, not four independent confirmations.
3. **Camera frames** — thumbnails, best-scoring first. Click any of them to open it full size.
   **Detector boxes are ON here by default**, unlike in review: you are looking at evidence for a
   repair decision, so the model's opinion is useful. A `VLM` badge means a language model also
   looked at that frame and the server score is a blend of the two.
4. **Observations** — the individual readings behind this defect.
5. **Repair history** — who marked it repaired or reopened it, when, and any note. Only appears once
   something has happened.
6. **The repair action** — an optional note (crew, work order number) and a **Mark repaired** /
   **Reopen defect** button. See the role warning at the top of this guide.

Marking a repair updates the marker immediately. Repaired defects stay on the map rather than
vanishing under your own panel.

## The frame viewer

Opens full size over everything, either from a thumbnail in the detail panel or from **Open full
size** in a map popup. Press **Escape** to close, or **← / →** to page through the other photographs
of the same defect.

Controls across the top: **Server boxes** and **On-device boxes** toggles (greyed out when there are
none, so an empty result reads as "the detector found nothing" rather than "the toggle is broken"),
and **Turn 90°** for a photograph stored sideways. Turning is for viewing only and is never saved.

The stroke legend shows three line styles — **Human**, **Server detector**, **On-device detector**.
They are lines rather than dots because the two machine detectors share a colour on purpose and are
told apart by their dash pattern.

Below the image: **Detection** (the scores, the model, when it was scored and captured), **Pairing**
(how confident the match between photograph and jolt was, and the time and distance between them),
and **VLM verification** when a language model has looked at it — including its written reasoning,
labelled *"A language model's account of the image, not a measurement."*

---

# Part 2 — Frame review

## What this is for

The detector is only as good as the examples it has been shown. Frame review is where a person looks
at a photograph and records what is actually in it. Those judgements are the ground truth everything
else is measured against.

There are two modes and they are separate jobs:

- **Judge** — is there a pothole in this picture at all? Fast; a keypress per frame.
- **Draw boxes** — *where* is it? Slower, and only worth doing on frames already judged positive.

> **Never run `scripts/label_frames.py` at the same time as this module.** They are two clients
> writing the same rows, and the last write wins. Pick one.

## Choosing what to work on

In the rail on the left:

- **Score band** — which frames to pull, by how confident the detector was. The default is **≥ 0.30**,
  which is the seam where the model is uncertain and your judgement is worth the most. `All` is
  available but reviewing frames the model already scores at zero teaches it little.
- **Score order** — highest-scoring first, the densest part of the seam.
- **Blind** — the server withholds the model's score and boxes entirely, so its opinion cannot
  anchor yours. Use it when you want your labels to be independent evidence.
- **Check my work** — queues **only frames that are already finished**, so you can re-read your own
  decisions. It is not an "include everything" switch; mixing it with normal work means paging past
  things you have already done.

Switching mode or band refetches the queue, because each combination is a genuinely different set of
frames.

Across the top you get your position (`frame 3 / 50`), how many remain in the band overall, and how
many you have done this session.

## Judge mode — the keys

| Key | Does |
|---|---|
| `1` | Pothole |
| `0` | Not a pothole |
| `u` | Unsure |
| `m` | Tag "manhole" |
| `s` | Tag "tar seal" |
| `g` | Tag "grate" |
| `w` | Tag "wet/shadow" |
| `n` | Write a note |
| `b` | Show the model's boxes |
| `j / →` | Next frame |
| `k / ←` | Previous frame |
| `r` | Reload the queue |
| `t` | Turn the frame 90° |

A verdict is recorded the moment you press `1`, `0` or `u`, and the queue advances. The four tags are
shortcuts for the commonest reasons something *looks* like a pothole but is not — record one when it
applies, because "not a pothole, it's a manhole" is far more useful than a bare no.

**`u` (Unsure) is a real answer**, not a way of skipping. If you cannot tell, that is information.

## Draw boxes mode — the keys

| Key | Does |
|---|---|
| `1` | Draw as pothole |
| `2` | Draw as manhole |
| `3` | Draw as grate |
| `4` | Draw as patch |
| `5` | Draw as crack |
| `Enter` | Save and move on |
| `s` | Submit every draft |
| `r` | Reload the queue |
| `Del` | Delete the selected box |
| `Esc` | Deselect |
| `b` | Show the model's boxes |
| `j / →` | Save, then next (Shift: peek without saving) |
| `k / ←` | Save, then previous (Shift: peek) |
| `Home` | First frame (records nothing) |
| `End` | Last frame (records nothing) |
| `t` | Turn the frame 90° (drawing pauses) |

Draw by dragging on the image. Click a box to select it. The digit keys set which class the *next*
box will be — they do not label the picture, which is what they do in Judge mode. The on-screen
**Keys** panel under the image is generated from the same list the console actually listens to, so
it can never be out of date.

## The five things worth knowing before a pass

**1. Saving is not submitting.** Moving between frames saves a **draft**. Drafts are invisible to
everything downstream. Only **`s`** signs your work off, and only that removes frames from your
queue. If the counter is not going down, this is why.

**2. Zero boxes is a real answer.** Saving a frame with nothing drawn records *"reviewed, genuinely
clean"* — which the model needs. It is not the same as skipping.

**3. Box regions, not lines.** A thin sliver drawn around a hairline crack is mostly undamaged
asphalt, and the model learns that the asphalt is the defect. Draw a compact region. The console
warns you when a box is more sliver than region — it is a warning, not a refusal.

**4. Peeking does not count.** Arriving at a frame with **Shift**, or via `Home`/`End`, marks it
*looked at, not worked on*, and it will not be submitted. The frame says so on screen. Go back
without Shift to include it.

**5. The model's boxes are hidden on purpose.** Press `b` if you want them, but be aware that
anchoring on the model's opinion has been **measured to make the detector worse** — recall fell from
0.708 to 0.354 across three passes when labellers could see them. There is deliberately no "accept
the model's boxes" button.

## Turning a sideways frame

A handful of old photographs were stored on their side by an early version of the phone app. Press
`t` to turn one upright for viewing. It is never saved, it resets when you move on, and **drawing is
disabled while a frame is turned** — a box drawn on a rotated view would be stored against the
unrotated picture.

---

## Reading the numbers

| You see | What it actually means |
|---|---|
| **`P(pothole)`** | *Which* group the jolt falls into, not *how sure* the system is. It comes from an unsupervised model that grouped the readings by shape; the highest-energy group got called "pothole". 0.998 means "comfortably inside that group", **not** "99.8% likely to be a real pothole". |
| **`Severity`** | The size of the jolt, divided by speed — the same defect hit slowly implies a rougher defect. Calibrated on one city's data. |
| **Tier (Low…Severe)** | Fixed bands over the severity number: 0 / 0.25 / 0.5 / 0.75. |
| **`Server p`** | The server detector's score for a photograph. **`0.000` means "found nothing", which is different from not yet scored** — the latter says *not yet scored*. If the frame carries a `VLM` badge, this is a blend of two opinions, not the detector alone. |
| **`On-device p`** | What the phone thought at capture time, before upload. |
| **`Fused confidence`** | How well a photograph and a jolt matched each other in time and space. |
| **`Corroborating passes`** | Separate drives past the same spot. A pass is one device's continuous run with no gap over 20 minutes. **1 means nobody has ever confirmed this defect.** |
| **`Corroborated` (dock)** | How many defects in view meet the publishing bar: two devices, or three passes. |

**"Outlier" does not mean "only seen once", and it does not mean "not a pothole."**
It is not a count of anything. Being seen more than once is *corroboration*, which is a completely
separate mechanism (see `Corroborating passes` above). The gate is a separate check that
looks only at how noisy the road was and how fast you were going — it never sees the class or
`P(pothole)`. It is asking "was this a strange measurement condition?", not "was this a pothole?". A
big jolt at walking pace is unusual whatever caused it. **Roughly one in four pothole readings is
rejected, by design**, and that cost was accepted to fix a worse problem: an earlier version of the
gate had learned that potholes *were* the anomaly and was rejecting almost all of them.

For where all of these come from, with the formulas and the measurements,
read [`from-reading-to-defect.md`](../architecture/from-reading-to-defect.md).

---

## When something looks wrong

| What you see | What is actually happening |
|---|---|
| The console shows defects, the mobile app shows none | Working as designed. Creating a defect takes one reading; publishing takes corroboration. The **Corroborated** card shows the gap. |
| A reading at `P(pothole) 0.998` is flagged as an outlier | The outlier check is a different test and never sees that number. See above. |
| Most triangles are hollow | Expected. Only pothole-classed readings can form a defect, and they are a small minority of what the phone records. |
| I turned on Sensor observations and only see a few | The Class filter starts on **Pothole**. Switch on Crack and Other to see everything. |
| Every defect circle is hollow | Correct, and it is the point: nothing in the collected data has been corroborated yet. It matches the **Corroborated** card. |
| The map is empty | Three separate causes: you are below zoom 13 (banner says so); the starting view is configured and may point somewhere with no data; or the map failed to load its worker, in which case **nothing** vector renders. |
| I ticked a raw-detection box and nothing appeared | Those layers need **zoom 15+**. Zoom in — they will not appear on their own. |
| All the filters are greyed out | You are at zoom 12 or below. Grouped markers carry no severity or device count. |
| **Mark repaired** fails with a permission error | Almost certainly the unowned-defect rule at the top of this guide — you need `admin`, not `staff`. It is not that your account changed. |
| A photograph is on its side | An old app build. Press `t` in review, or **Turn 90°** in the viewer. |
| `Server p` is `0.000` | The detector looked and found nothing. Not the same as unscored, which says *not yet scored*. |
| The change indicators are all dashes, and street search is disabled | Both honest blanks with tooltips explaining why. No month-ago baseline exists; no street names exist. |
| I drew boxes and the counter did not move | You saved drafts. Press **`s`** to submit. |
| I get signed out constantly, or random errors | A server configuration problem, not you — the signing key is not being persisted. Tell whoever runs the server. |

---

## What the console cannot do yet

- **Inventory, Work orders, Reports and Admin** are not built.
- **No password reset, no self-service change, no MFA.** An administrator has to reset a password.
- **Box drawing is mouse-only** — there is no keyboard way to draw.
- **Two people reviewing the same frame overwrite each other's boxes.** Agree who is working on
  which band.
- **No way to withdraw a submission** from the interface. An administrator can do it directly.
- **No street names anywhere**, so no street search and no street-based reporting.

---

## Where to read more

| Question | Document |
|---|---|
| Where do the numbers come from? | [`architecture/from-reading-to-defect.md`](../architecture/from-reading-to-defect.md) |
| What was built when, and what did it cost? | [`roadmap.md`](../roadmap.md) and [`phases/`](../phases/) |
| How do I run the pipeline or a collection drive? | [`runbooks/`](../runbooks/) |
| How do I develop the console itself? | [`dashboard/README.md`](../../dashboard/README.md) |
