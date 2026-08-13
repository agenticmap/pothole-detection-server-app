"""Server-side frame detection (Phase 2.3).

A pluggable inference worker that runs a bigger detector on uploaded JPEGs and
populates the asset_frame.server_* columns, feeding the fusion engine a stronger
visual signal than the on-device probability.
"""
