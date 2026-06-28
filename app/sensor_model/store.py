"""Persistence for SensorModel artifacts against the sensor_model table.

A fit inserts a new row and flips it active (deactivating the prior active row
in the same transaction — the partial unique index allows only one active row).
Loading reconstructs the SensorModel, unpickling the GMM + IsolationForest from
the joblib blob.
"""

from __future__ import annotations

import io
import json
import logging

import asyncpg
import joblib

from app.sensor_model.model import (
    SensorModel,
    SeverityCalibration,
    Standardization,
)

logger = logging.getLogger(__name__)

_INSERT_MODEL_SQL = """
INSERT INTO sensor_model (
    model_version, n_observations, k, bic,
    standardization_jsonb, components_jsonb, class_map_jsonb, severity_calib_jsonb,
    model_blob, sklearn_version, is_active
)
VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10, TRUE)
"""


async def save_model(pool: asyncpg.Pool, model: SensorModel, blob: bytes) -> None:
    """Persist a fitted model and make it the active one (atomically)."""
    class_map_json = {str(k): v for k, v in model.class_map.items()}
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE sensor_model SET is_active = FALSE WHERE is_active")
            await conn.execute(
                _INSERT_MODEL_SQL,
                model.model_version,                       # $1
                model.n_observations,                      # $2
                model.k,                                   # $3
                model.bic,                                 # $4
                json.dumps(model.standardization.to_jsonb()),   # $5
                json.dumps(model.components),              # $6
                json.dumps(class_map_json),                # $7
                json.dumps(model.severity_calib.to_jsonb()),    # $8
                blob,                                      # $9
                model.sklearn_version,                     # $10
            )
    logger.info(
        "Saved active sensor_model %s (k=%d, n=%d, bic=%.2f)",
        model.model_version, model.k, model.n_observations, model.bic or float("nan"),
    )


_SELECT_ACTIVE_SQL = """
SELECT model_version, n_observations, k, bic,
       standardization_jsonb, components_jsonb, class_map_jsonb, severity_calib_jsonb,
       model_blob, sklearn_version
FROM sensor_model
WHERE is_active
LIMIT 1
"""


def _loads(value) -> object:
    """asyncpg returns JSONB as str by default; tolerate already-parsed values."""
    if isinstance(value, str | bytes | bytearray):
        return json.loads(value)
    return value


async def load_active_model(pool: asyncpg.Pool) -> SensorModel | None:
    """Load the active SensorModel, or None if no model has been fit yet."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_SELECT_ACTIVE_SQL)
    if row is None:
        return None

    standardization = Standardization.from_jsonb(_loads(row["standardization_jsonb"]))
    components = _loads(row["components_jsonb"])
    class_map_raw = _loads(row["class_map_jsonb"])
    class_map = {int(k): v for k, v in class_map_raw.items()}
    severity_calib = SeverityCalibration.from_jsonb(_loads(row["severity_calib_jsonb"]))

    gmm = iforest = None
    if row["model_blob"] is not None:
        gmm, iforest = joblib.load(io.BytesIO(row["model_blob"]))

    return SensorModel(
        model_version=row["model_version"],
        standardization=standardization,
        class_map=class_map,
        severity_calib=severity_calib,
        k=row["k"],
        n_observations=row["n_observations"],
        bic=row["bic"],
        sklearn_version=row["sklearn_version"],
        gmm=gmm,
        iforest=iforest,
        components=components,
    )
