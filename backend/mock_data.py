"""Fallback mock data for photon-ec-patient, used only when the live Photon
sandbox can't be reached (see photon_client.py). This app makes exactly one
Photon call -- createPatient, to sync the patient record + allergies/med
history the clinician app will need -- so this file is deliberately small.
Every value is tagged `_source: "mock"`.
"""

from __future__ import annotations

# Mock allergen catalog used in photon-clinic-assistant, so a
# demo run recognizes the same handful of common allergy terms.
_MOCK_ALLERGEN_CATALOG = {
    "penicillin": [{"id": "alg_penicillin01", "name": "Penicillin", "rxcui": "7980"}],
    "sulfa": [{"id": "alg_sulfa01", "name": "Sulfonamides", "rxcui": "10156"}],
    "nsaid": [{"id": "alg_nsaid01", "name": "NSAIDs", "rxcui": "1441"}],
}

# Same catalog photon-ec-clinician's mock_data.py uses for its
# search_treatments mock -- this app resolves a patient's *current
# medications* against the same real Photon `treatments` catalog (see
# photon_client.search_treatments; confirmed live that the resulting med_
# ID is accepted as MedHistoryInput.medicationId on createPatient), so the
# mock fallback should recognize the same handful of names, including the
# CYP3A4 inducers eligibility.py already knows about.
_MOCK_TREATMENT_CATALOG = {
    "rifampin": [{"id": "med_mock_rifampin", "name": "Rifampin 300 MG Oral Capsule"}],
    "carbamazepine": [{"id": "med_mock_carbamazepine", "name": "Carbamazepine 200 MG Oral Tablet"}],
    "topiramate": [{"id": "med_mock_topiramate", "name": "Topiramate 100 MG Oral Tablet"}],
    "phenytoin": [{"id": "med_mock_phenytoin", "name": "Phenytoin 100 MG Oral Capsule"}],
}


def mock_search_allergens(name: str) -> dict:
    key = name.lower().strip()
    for k, v in _MOCK_ALLERGEN_CATALOG.items():
        if k in key or key in k:
            return {"allergens": v, "_source": "mock"}
    # Unlike mock_search_treatments, we do NOT invent a fake ID for an
    # unrecognized term here
    return {"allergens": [], "_source": "mock"}


def mock_search_treatments(term: str) -> dict:
    key = term.lower().strip()
    for k, v in _MOCK_TREATMENT_CATALOG.items():
        if k in key or key in k:
            return {"treatments": v, "_source": "mock"}
    return {"treatments": [], "_source": "mock"}


def mock_create_patient(external_id: str, first: str, last: str, allergies: list[str] | None = None) -> dict:
    return {
        "id": f"pat_mock_{external_id}",
        "allergyStatus": "ALLERGIES_SET" if allergies else "NO_KNOWN_ALLERGIES",
        "_source": "mock",
    }
