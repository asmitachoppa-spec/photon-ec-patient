"""
Patient-facing EC (emergency contraception) intake

This form will gather information about the patient and their sexual
encounter to either 1. recommend they use levonorgestrel OTC (Plan B)
or 2. escalate to a clinician for a prescription. It will not use AI for
that recommendation but rather run a function (from eligibility.py)
that will sort the case in one of those two categories.

Flow:
  1. GET/POST /intake  -> gathers hours since intercourse + weight only.
     Uses eligibility.py to see if plain OTC levonorgestrel is
     clearly sufficient. If so, will display that and STOP. No Photon call
     or no clinician handoff
     to review when the answer is "buy this over the counter."
  2. If OTC isn't clearly sufficient, POST /details -> name, date of
     birth, and phone (Photon requires all three as mandatory createPatient
     arguments -- see photon_client.py), plus allergies + current
     medications. This will then talk to Photon and call createPatient
     (which the clinician app will query later to grab patient data)
     It will also then create a case and store that information in a
     `handoff_cases` Postgres table (approval_fl='F') for the clinician
     app to pick up. This app never decides *which* prescription drug or
     device to use -- Ella / ParaGard / Mirena / Liletta are chosen by a
     human clinician in the other app, informed by the intake data and a
     real Photon safety check. This app's job stops at "does this need a
     clinician, yes or no."

"""

from __future__ import annotations

import logging
import os
import uuid

from flask import Flask, render_template, request, session, redirect, url_for

import eligibility
import db
from photon_client import get_photon_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

_HERE = os.path.dirname(os.path.abspath(__file__))
_FRONTEND = os.path.join(_HERE, "..", "frontend")

app = Flask(
    __name__,
    template_folder=os.path.join(_FRONTEND, "templates"),
    static_folder=os.path.join(_FRONTEND, "static"),
)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

try:
    db.init_db()
except Exception as e:  # pragma: no cover - surfaced in the UI instead
    logger.warning("Could not initialize Postgres schema at startup: %s", e)


def _parse_list(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw or raw.lower() in ("none", "no", "n/a", "na"):
        return []
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def _normalize_phone(raw: str) -> str:
    """Best-effort normalize to the E.164-ish shape Photon's AWSPhone scalar
    expects. Not a full phone-validation library -- good enough for a US-
    centric demo intake form; a real deployment would validate this
    properly (and probably support non-US numbers)."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if raw.strip().startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else raw


@app.route("/")
def index():
    return redirect(url_for("intake"))


@app.route("/intake", methods=["GET", "POST"])
def intake():
    if request.method == "GET":
        return render_template("intake.html")

    try:
        hours = float(request.form["hours_since_intercourse"])
        weight = float(request.form["weight_lb"])
    except (KeyError, ValueError):
        return render_template(
            "intake.html",
            error="Please enter a number for both hours since intercourse and weight (lb).",
        )

    result = eligibility.evaluate(hours, weight, current_medications=[])
    otc_ok = (
        result.recommended == "levonorgestrel"
        and not next(o for o in result.options if o.option == "levonorgestrel").efficacy_note
    )

    session["hours_since_intercourse"] = hours
    session["weight_lb"] = weight

    if otc_ok:
        return render_template(
            "otc_result.html",
            hours=hours,
            weight=weight,
            display_name=eligibility.TREATMENT_NAMES["levonorgestrel"],
            access_info=eligibility.ACCESS_INFO["levonorgestrel"],
        )

    # Not clearly OTC-sufficient -- show *why*, then move to step 2.
    reasons = []
    for o in result.options:
        if o.option == "levonorgestrel":
            reasons = o.reasons or ([o.efficacy_note] if o.efficacy_note else [])
    return redirect(url_for("details"))


@app.route("/details", methods=["GET", "POST"])
def details():
    if "hours_since_intercourse" not in session:
        return redirect(url_for("intake"))

    if request.method == "GET":
        return render_template(
            "details.html",
            hours=session["hours_since_intercourse"],
            weight=session["weight_lb"],
        )

    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    date_of_birth = request.form.get("date_of_birth", "").strip()
    phone_raw = request.form.get("phone", "").strip()

    if not (first_name and last_name and date_of_birth and phone_raw):
        return render_template(
            "details.html",
            hours=session["hours_since_intercourse"],
            weight=session["weight_lb"],
            first_name=first_name,
            last_name=last_name,
            date_of_birth=date_of_birth,
            phone=phone_raw,
            error="Please fill in your name, date of birth, and phone number -- "
            "Photon requires these on the patient record it creates.",
        )

    phone = _normalize_phone(phone_raw)
    allergies = _parse_list(request.form.get("allergies", ""))
    medications = _parse_list(request.form.get("current_medications", ""))
    hours = session["hours_since_intercourse"]
    weight = session["weight_lb"]

    eligibility_dict = eligibility.result_to_dict(
        eligibility.evaluate(hours, weight, medications)
    )

    external_id = f"pat_{uuid.uuid4().hex[:12]}"
    session["external_id"] = external_id

    client = get_photon_client()
    photon_patient = client.create_patient(
        external_id=external_id,
        first=first_name,
        last=last_name,
        allergies=allergies,
        medication_names=medications,
        date_of_birth=date_of_birth,
        phone=phone,
    )

    # Name, allergies, and current medications are deliberately NOT passed
    # to insert_case -- they're already on the real Photon patient record
    # When the clinician app needs patient info from the case, it will query
    # based on the photon_patient_id
    case_id = db.insert_case(
        external_id=external_id,
        photon_patient_id=photon_patient.get("id"),
        hours_since_intercourse=hours,
        weight_lb=weight,
        interacting_meds_found=eligibility_dict["interacting_meds_found"],
        eligibility_summary=eligibility_dict,
    )

    return render_template(
        "submitted.html",
        case_id=case_id,
        external_id=external_id,
        photon_patient=photon_patient,
        eligibility=eligibility_dict,
    )


@app.route("/reset")
def reset():
    session.clear()
    return redirect(url_for("intake"))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
