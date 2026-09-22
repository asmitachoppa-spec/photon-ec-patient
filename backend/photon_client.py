"""Photon Health API client for the patient-facing intake form.

This app makes exactly one real Photon call: createPatient, once a patient's
intake needs a clinician and adds in allergies and medication history info.

It does NOT call prescriptionScreen or attempt a
prescription write -- that's the clinician app's job, once a human has
picked a specific treatment.

Every call falls back to mock_data.py and is
tagged `_source: "live" | "mock"` so nothing is silently pretended to be
real.
"""

from __future__ import annotations

import os
import time
import logging
from typing import Optional

import requests

from env_loader import load_dotenv
from mock_data import mock_create_patient, mock_search_allergens, mock_search_treatments
from catalog_agent import try_ai_resolve

load_dotenv()

logger = logging.getLogger("photon_client")


class PhotonAuthError(Exception):
    pass


class PhotonClient:
    def __init__(self) -> None:
        self.client_id = os.getenv("PHOTON_CLIENT_ID")
        self.client_secret = os.getenv("PHOTON_CLIENT_SECRET")
        self.org_id = os.getenv("PHOTON_ORG_ID")
        self.audience = os.getenv("PHOTON_AUDIENCE", "https://api.neutron.health")
        self.token_url = os.getenv("PHOTON_TOKEN_URL", "https://auth.neutron.health/oauth/token")
        self.api_url = os.getenv("PHOTON_API_URL", "https://api.neutron.health/graphql")
        self.clinical_api_url = os.getenv(
            "PHOTON_CLINICAL_API_URL", "https://clinical-api.neutron.health/graphql"
        )

        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._live_disabled_reason: Optional[str] = None

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        if not (self.client_id and self.client_secret):
            raise PhotonAuthError("Missing PHOTON_CLIENT_ID / PHOTON_CLIENT_SECRET")
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "audience": self.audience,
            "grant_type": "client_credentials",
        }
        try:
            resp = requests.post(self.token_url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise PhotonAuthError(f"Token exchange failed: {e}") from e
        if "access_token" not in data:
            raise PhotonAuthError(f"Token exchange returned no access_token: {data}")
        self._token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 3600)
        return self._token

    def is_live(self) -> bool:
        if self._live_disabled_reason is not None:
            return False
        try:
            self._get_token()
            return True
        except PhotonAuthError as e:
            self._live_disabled_reason = str(e)
            logger.warning("Photon live API unavailable, falling back to mock data: %s", e)
            return False

    @staticmethod
    def _handle_response(resp: "requests.Response", label: str) -> dict:
        """`resp.raise_for_status()` on its own discards the response body,
        so a non-2xx status surfaces as a bare "400 Client Error: Bad
        Request" with no indication of *why* -- unhelpful both for us and
        for whoever's reading the logs. Read the body first (JSON if
        possible, else raw text) and include it in whatever gets raised,
        the same way a 200-with-GraphQL-errors response already is below."""
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code >= 400:
            detail = body if body is not None else resp.text[:1000]
            raise RuntimeError(f"HTTP {resp.status_code} from {label}: {detail}")
        if body is None:
            raise RuntimeError(f"{label} returned a non-JSON 2xx response: {resp.text[:1000]}")
        if body.get("errors"):
            raise RuntimeError(body["errors"])
        return body["data"]

    def _post_core(self, query: str, variables: dict) -> dict:
        token = self._get_token()
        resp = requests.post(
            self.api_url,
            json={"query": query, "variables": variables},
            headers={"authorization": f"Bearer {token}"},
            timeout=15,
        )
        return self._handle_response(resp, "core API")

    def _post_clinical(self, query: str, variables: dict) -> dict:
        token = self._get_token()
        resp = requests.post(
            self.clinical_api_url,
            json={"query": query, "variables": variables},
            headers={
                "x-photon-auth-token": token,
                "x-photon-auth-token-type": "auth0",
            },
            timeout=15,
        )
        return self._handle_response(resp, "clinical API")

    # ------------------------------------------------------------------
    # AllergenInput takes a real allergenId, not a free-text name (confirmed
    # against the live sandbox -- a "field that is not defined for input
    # object type 'AllergenInput'" error surfaces immediately if you send
    # {"name": ...} instead). So a patient-typed allergy has to be resolved
    # against Photon's own allergen catalog first, the same way a treatment
    # name has to be resolved via search_treatments before it can be used
    # anywhere else in this workspace's Photon projects.
    # ------------------------------------------------------------------
    def search_allergens(self, name: str) -> dict:
        # Two real schema mismatches fixed here (confirmed against the live
        # sandbox): $filter is AllergenFilter! (non-null), not AllergenFilter
        # -- Photon's validator rejects a nullable-typed variable in a
        # non-null argument position even if a value is always supplied --
        # and Allergen has no rxcui field, only id/name.
        query = """
        query Allergens($filter: AllergenFilter!) {
          allergens(filter: $filter) { id name }
        }
        """
        try:
            data = self._post_clinical(query, {"filter": {"name": name}})
            return {"allergens": data["allergens"], "_source": "live"}
        except Exception as e:
            logger.warning("search_allergens(%r) falling back to mock: %s", name, e)
            return mock_search_allergens(name)

    # ------------------------------------------------------------------
    # Resolves a patient-typed *medication* name to a real Photon
    # medicationId, the same way search_allergens resolves an allergy name
    # to an allergenId. Confirmed live against the sandbox: the core API's
    # own `medications` catalog turned out to be missing common generic
    # names outright (a filtered search for "rifampin" came back empty),
    # while the clinical API's `treatments` catalog -- the same one
    # photon-ec-clinician's search_treatments already uses to resolve the
    # four EC buttons -- has it, and the resulting med_ ID is accepted as
    # MedHistoryInput.medicationId on createPatient below (confirmed with a
    # live createPatient + medicationHistory round-trip). So this
    # deliberately queries the clinical API's treatments catalog, not the
    # core API's medications one, even though the medication is only ever
    # used on the core API side.
    # ------------------------------------------------------------------
    def search_treatments(self, term: str) -> dict:
        query = """
        query Treatments($filter: TreatmentFilter!) {
          treatments(filter: $filter) { id name }
        }
        """
        try:
            data = self._post_clinical(query, {"filter": {"term": term}})
            return {"treatments": data["treatments"], "_source": "live"}
        except Exception as e:
            logger.warning("search_treatments(%r) falling back to mock: %s", term, e)
            return mock_search_treatments(term)

    # ------------------------------------------------------------------
    # The one Photon call this app makes: sync a real patient record with
    # allergies + medication history, so the clinician app's later
    # prescriptionScreen call is checking a real, populated Photon record
    # rather than a blank one. See README for why this app stops here.
    #
    # Photon's real schema requires dateOfBirth and phone as mandatory
    # createPatient arguments (confirmed against the live sandbox -- a
    # MissingFieldArgument error surfaces immediately if either is
    # omitted). This intake form deliberately doesn't ask a patient for
    # either -- that's real personal data outside this app's actual scope
    # (hours since intercourse, weight, allergies, medications) -- so both
    # are sent as clearly-fake placeholders, the same way `first`/`last`
    # are already placeholders ("Patient"/"Portal") rather than a real
    # collected name. See README for this tradeoff and the alternative
    # (actually asking for DOB/phone) if you'd rather collect them for
    # real.
    # ------------------------------------------------------------------
    PLACEHOLDER_DATE_OF_BIRTH = "1990-01-01"
    PLACEHOLDER_PHONE = "+10000000000"

    def create_patient(
        self,
        external_id: str,
        first: str,
        last: str,
        allergies: Optional[list[str]] = None,
        medication_names: Optional[list[str]] = None,
        date_of_birth: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> dict:
        query = """
        mutation createPatient(
          $externalId: ID
          $name: NameInput!
          $dateOfBirth: AWSDate!
          $sex: SexType!
          $phone: AWSPhone!
          $allergies: [AllergenInput]
          $medicationHistory: [MedHistoryInput]
        ) {
          createPatient(
            externalId: $externalId
            name: $name
            dateOfBirth: $dateOfBirth
            sex: $sex
            phone: $phone
            allergies: $allergies
            medicationHistory: $medicationHistory
          ) {
            id
            allergyStatus
          }
        }
        """
        # Resolve each patient-typed allergy name to a real Photon allergenId.
        # Anything that doesn't match Photon's catalog is left out of the
        # mutation (rather than guessing an ID or dropping the mutation
        # entirely) and reported back in `unresolved_allergies` so the UI
        # and the clinician app can say plainly "this was reported but not
        # synced to Photon" instead of silently losing it.
        allergy_input = []
        unresolved_allergies = []
        ai_matched_allergies = []
        for a in allergies or []:
            search = self.search_allergens(a)
            matches = search.get("allergens") or []
            if not matches:
                # Direct search came back empty -- before giving up, let an
                # AI agent try a few plausible alternate searches (typo fix,
                # brand/generic swap, shortened form) against this exact
                # same real catalog. It can only "match" something a real
                # search_allergens() call actually returned; see
                # catalog_agent.py for why this is a lookup-table-matching
                # task, not a clinical one.
                ai_match = try_ai_resolve(a, "allergy", self.search_allergens, "allergens")
                if ai_match:
                    matches = [{"id": ai_match["id"], "name": ai_match["name"]}]
                    ai_matched_allergies.append({"typed": a, **ai_match})
            if matches:
                allergy_input.append({"allergenId": matches[0]["id"]})
            else:
                unresolved_allergies.append(a)

        # Same resolve-or-report pattern for current medications -- this is
        # the fix for a real, previously-undiscovered bug: this function
        # already accepted `medication_names` but never actually included
        # them in the mutation, so nothing typed here was ever synced to
        # Photon. That mattered more once the clinician app started reading
        # current medications back from Photon live (see get_patient in the
        # sibling app) instead of from a locally-stored column.
        medication_input = []
        unresolved_medications = []
        ai_matched_medications = []
        for m in medication_names or []:
            search = self.search_treatments(m)
            matches = search.get("treatments") or []
            if not matches:
                ai_match = try_ai_resolve(m, "medication", self.search_treatments, "treatments")
                if ai_match:
                    matches = [{"id": ai_match["id"], "name": ai_match["name"]}]
                    ai_matched_medications.append({"typed": m, **ai_match})
            if matches:
                medication_input.append({"medicationId": matches[0]["id"], "active": True})
            else:
                unresolved_medications.append(m)

        try:
            data = self._post_core(
                query,
                {
                    "externalId": external_id,
                    "name": {"first": first, "last": last},
                    "dateOfBirth": date_of_birth or self.PLACEHOLDER_DATE_OF_BIRTH,
                    "sex": "FEMALE",
                    "phone": phone or self.PLACEHOLDER_PHONE,
                    "allergies": allergy_input or None,
                    "medicationHistory": medication_input or None,
                },
            )
            return {
                **data["createPatient"],
                "_source": "live",
                "unresolved_allergies": unresolved_allergies,
                "unresolved_medications": unresolved_medications,
                "ai_matched_allergies": ai_matched_allergies,
                "ai_matched_medications": ai_matched_medications,
            }
        except Exception as e:
            logger.warning("create_patient falling back to mock: %s", e)
            result = mock_create_patient(external_id, first, last, allergies)
            result["unresolved_allergies"] = unresolved_allergies
            result["unresolved_medications"] = unresolved_medications
            result["ai_matched_allergies"] = ai_matched_allergies
            result["ai_matched_medications"] = ai_matched_medications
            return result


_client: Optional[PhotonClient] = None


def get_photon_client() -> PhotonClient:
    global _client
    if _client is None:
        _client = PhotonClient()
    return _client
