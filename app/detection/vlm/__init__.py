"""Pluggable VLM verifier — the Stage-2 seam of the hybrid detector.

A verifier looks at one frame (or a crop) and answers "is this really a pothole?",
rejecting look-alikes (shadows, manholes, wet patches, markings). Backends (Claude,
Gemini, a local OpenAI-compatible server) swap by config, mirroring the detector
registry in app/detection/registry.py.
"""
