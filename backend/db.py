"""Shared Postgres handoff table.

This is the shared piece of infrastructure between this app and the
separate photon-ec-clinician app: a `handoff_cases` table both point at via
the same DATABASE_URL. This app only INSERTs a new row, with
approval_fl='F' (meaning it hasn't been reviewed yet) -- it never
queries or updates one. The clinician app is the only thing that reads
pending rows and flips approval_fl to 'T' or 'D'.

The records only store what is needed for that specific case regarding that
patient. The actual patient information is stored with Photon.
The row carries only what's needed to route the case to
a clinician (see SCHEMA for more detail).

Schema is created idempotently (CREATE TABLE IF NOT EXISTS) here so
whichever of the two apps happens to start first bootstraps it.
"""

from __future__ import annotations

import os
import json
import logging

import psycopg2

from env_loader import load_dotenv

load_dotenv()

logger = logging.getLogger("db")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/photon_ec")

SCHEMA = """
CREATE TABLE IF NOT EXISTS handoff_cases (
    id                      SERIAL PRIMARY KEY,
    external_id             TEXT UNIQUE NOT NULL,
    photon_patient_id       TEXT,
    hours_since_intercourse NUMERIC,
    weight_lb               NUMERIC,
    interacting_meds_found  JSONB NOT NULL DEFAULT '[]',
    eligibility_summary     JSONB,
    approval_fl             CHAR(1) NOT NULL DEFAULT 'F',
    chosen_treatment        TEXT,
    chosen_treatment_id     TEXT,
    screen_result           JSONB,
    rx_write_attempt        JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at             TIMESTAMPTZ,
    declined_at             TIMESTAMPTZ
);
"""


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(SCHEMA)
        logger.info("handoff_cases table ready")
    finally:
        conn.close()


def insert_case(
    external_id: str,
    photon_patient_id: str | None,
    hours_since_intercourse: float,
    weight_lb: float,
    interacting_meds_found: list[str],
    eligibility_summary: dict,
) -> int:
    conn = get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO handoff_cases
                    (external_id, photon_patient_id,
                     hours_since_intercourse, weight_lb,
                     interacting_meds_found, eligibility_summary, approval_fl)
                VALUES (%s, %s, %s, %s, %s, %s, 'F')
                ON CONFLICT (external_id) DO UPDATE SET
                    photon_patient_id       = EXCLUDED.photon_patient_id,
                    hours_since_intercourse = EXCLUDED.hours_since_intercourse,
                    weight_lb               = EXCLUDED.weight_lb,
                    interacting_meds_found  = EXCLUDED.interacting_meds_found,
                    eligibility_summary     = EXCLUDED.eligibility_summary
                RETURNING id;
                """,
                (
                    external_id,
                    photon_patient_id,
                    hours_since_intercourse,
                    weight_lb,
                    json.dumps(interacting_meds_found),
                    json.dumps(eligibility_summary),
                ),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()
