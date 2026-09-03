---
updated: 2026-09-03
---

# Documentation index

Start with [`roadmap.md`](./roadmap.md) — it is the master status table, one row per phase, each
linking to the record below.

Docs are grouped by **what you want from them**, not by when they were written:

| Folder | Read it when you want… |
|---|---|
| [`architecture/`](#architecture) | to know how the system is *meant* to work, and why |
| [`phases/`](#phases) | to know what was built in a given phase and what it cost |
| [`runbooks/`](#runbooks) | to actually run something |
| [`research/`](#research) | measured findings, written to be cited |
| [`reference/`](#reference) | lookup tables (licences, hashes) |

**When a doc and the code disagree, the code wins.** Several of these were written before the thing
they describe existed; the ones that have been overtaken say so at the top.

---

## architecture

Design and rationale. Longer-lived than any single phase.

| Doc | What it settles |
|---|---|
| [`enterprise-architecture-plan.md`](./architecture/enterprise-architecture-plan.md) | The platform strategy: mobile wire contract frozen at v1, portable PostGIS schema, generic asset model from day one. Also the Supabase-vs-self-hosted and dashboard-stack decisions. |
| [`detection-approach.md`](./architecture/detection-approach.md) | Why server-side detection is a YOLO → VLM hybrid rather than one bigger model. |
| [`detection-model-strategy.md`](./architecture/detection-model-strategy.md) | Models A/B/C — road damage, street furniture, road markings — and why only A may write `server_probability`. |

## phases

One doc per phase: what shipped, what was measured, what it cost. **Negative results are kept**, and
are usually the more useful ones.

| Doc | Phase |
|---|---|
| [`phase-2.1-fusion-engine-plan.md`](./phases/phase-2.1-fusion-engine-plan.md) | Sensor classifier ported from MATLAB + logit-space fusion |
| [`phase-2.2-clustering-plan.md`](./phases/phase-2.2-clustering-plan.md) | PostGIS `ST_ClusterDBSCAN` crowd clustering |
| [`phase-2.2b-read-path-plan.md`](./phases/phase-2.2b-read-path-plan.md) | Public zoom-aware `GET /potholes` |
| [`phase-2.2c-spatiotemporal-fusion.md`](./phases/phase-2.2c-spatiotemporal-fusion.md) | Spatiotemporally weighted cluster confidence |
| [`phase-2.2d-pairing-search.md`](./phases/phase-2.2d-pairing-search.md) | The lookahead pairing cost — the camera sees a pothole *before* the wheel hits it |
| [`phase-2.3-detection-plan.md`](./phases/phase-2.3-detection-plan.md) | The pluggable inference worker, and the hybrid VLM backend |
| [`phase-2.4-auth-plan.md`](./phases/phase-2.4-auth-plan.md) | City-staff tier behind RS256 bearer tokens |
| [`phase-2.5-dashboard-plan.md`](./phases/phase-2.5-dashboard-plan.md) | Operator dashboard: vector tiles, detail panel, repair marking |
| [`phase-2.5b-dashboard-design.md`](./phases/phase-2.5b-dashboard-design.md) | Design pass, self-hosted basemap, severity recalibration |
| [`phase-2.6-hardening.md`](./phases/phase-2.6-hardening.md) | Production hardening |
| [`phase-2.7-detection-enablement.md`](./phases/phase-2.7-detection-enablement.md) | First real ONNX execution, ROI crop, labelling tool, offline eval |
| [`phase-2.7b-road-surface-classes.md`](./phases/phase-2.7b-road-surface-classes.md) | **Negative result.** Multi-class distractors cost recall; every real-domain box was a negative |
| [`phase-2.7c-public-data.md`](./phases/phase-2.7c-public-data.md) | **Qualified no.** RDD2022 reversed the collapse but did not beat v1 |
| [`phase-2.7d-review-surface.md`](./phases/phase-2.7d-review-surface.md) | The labelling loop moved into the console — and a submit path that would have signed off frames nobody opened, which the exporter ships as background images |
| [`phase-2.9-vlm-verification.md`](./phases/phase-2.9-vlm-verification.md) | The VLM has never been measured; the harness that will, and the thresholds it refutes |
| [`phase-2.10-imagery-surfaces.md`](./phases/phase-2.10-imagery-surfaces.md) | Two thousand frames nobody had scored, a panel cropping away the road surface, and the first VLM that ever answered |
| [`integration-round-2026-08.md`](./phases/integration-round-2026-08.md) | **The first full round.** An outlier gate that had learned to reject potholes, a severity scale saturating below its own minimum — and the finding that no cluster has ever been corroborated |

## runbooks

Procedures. Commands you run, in order.

| Doc | Use it to |
|---|---|
| [`integration-round-runbook.md`](./runbooks/integration-round-runbook.md) | Take collected drives all the way to a populated operator console, and verify the app's read path |
| [`phase-2.2d-runbook.md`](./runbooks/phase-2.2d-runbook.md) | Re-fuse an existing database under the new pairing search |
| [`phase-2.7-runbook.md`](./runbooks/phase-2.7-runbook.md) | Train, evaluate, and enable a detection model end to end |
| [`road-test-readiness.md`](./runbooks/road-test-readiness.md) | Check the full loop before a collection drive |

## research

Findings, written to be read on their own and to source a report.

| Doc | What it establishes |
|---|---|
| [`detection-research-record.md`](./research/detection-research-record.md) | Consolidated account of all five detection models. Headline: archive mAP50 spanned 0.030 while real-frame recall spanned 3.29× — the offline metric never once predicted quality. |
| [`paper-fidelity-assessment.md`](./research/paper-fidelity-assessment.md) | **Where the implementation matches Sattar et al. and where it does not**, point by point, with the paper quoted. Also the measurements that closed three hypotheses: no clustering parameter, and no classification strategy, recovers a cross-day repeat. |
| [`app-capture-findings.md`](./research/app-capture-findings.md) | The limiting factor is the data, not the detector: whole-second timestamps, reused GPS fixes, 25% padding, one night hour at 72 km/h. |
| [`corroboration-coverage-analysis.md`](./research/corroboration-coverage-analysis.md) | **Why nothing corroborates: route coverage, not the detector.** 91.9% of pothole detections sit on road only one day ever covered, so they were never eligible to co-locate; conditioned on revisited road the dataset holds 18, against a null whose median is 0. Records two retracted explanations and why each got through. |
| `research/probabilistic-crowdsourcing-road-anomaly-2018-SHS.docx` | The source paper the fusion engine implements (§4.4–4.5 are what Phase 2.2c encodes). **Not tracked in git** — `.gitignore` excludes `*.docx`, so this is a local file only. Originally filed as `3 - Probabilistic based crowdsourcing technique for road surface anomaly classification_Rev_Aug218_SHS.docx`. |

## reference

| Doc | Contents |
|---|---|
| [`model-attribution.md`](./reference/model-attribution.md) | Every shipped model: licence, provenance, SHA-256. Update it on any model change. |
