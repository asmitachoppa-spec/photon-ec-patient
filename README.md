# photon-ec-patient

A patient-facing intake **form** for emergency contraception
(EC) eligibility, and the first half of a two-app system with
[photon-ec-clinician](../photon-ec-clinician). Built on Flask, calling
Photon's real Neutron sandbox API directly.

## Background

The whole point of Photon is to be able to streamline getting your prescriptions in a much better way. Fastening the process and eliminating unnecessary appointments have a lot of pros -- you save the doctor’s time, you get your medications quicker, but the one that we’re going to focus on is -- you can avoid uncomfortable conversations. And this superpower would be very useful for women who need emergency contraceptives

The target audience is young women who might be uncomfortable booking a doctor’s appointment and speaking with someone about needing this medication maybe without their parent’s knowledge. This workflow would create a seamless way for these women to prioritize their health in a comfortable way.

This is an app that clinics can incorporate into their systems to work with both patients and clinicians to prescribe these emergency contraceptives. I have built both sides of the app but this one in particulat is the patient side

When a patient has had unprotected sexual intercourse, they can use this app to understand what their next steps are. Based on how long ago the sex was and their weight, this app will either 1. tell them they can get an over-the-counter plan B pill (levonorgestrel) or 2. take the patient's information/sexual encounter information and escalate to a clinician for a prescription. This part of the flow is the patient side, which will cover the interaction with the patient, creating a createPatient photon call, and storing the sexual encounter information in a db. The next part will cover the clinician side.

## What this app does

1. **Step 1** (`/intake`): two questions -- hours since unprotected
   intercourse, and weight. A deterministic engine (`eligibility.py`,
   ported unchanged from `photon-ec-assistant` -- same cited thresholds,
   same disclaimer) decides, in plain auditable Python with **no LLM
   involved**, whether plain over-the-counter levonorgestrel is clearly
   sufficient.
   - If yes: shown the OTC recommendation and access info. **Nothing is
     sent anywhere** -- no Photon call, no database row. There's nothing
     for a clinician to review when the answer is "buy this at a
     pharmacy."
   - If no: routed to step 2.
2. **Step 2** (`/details`): allergies and current medications. This is the
   one point where this app talks to Photon at all -- a real
   `createPatient` mutation, syncing the patient's allergy and medication
   history so the clinician app's later interaction/allergy screen is
   checking a real, populated record instead of a blank one.
3. The app then writes **one row** to a shared Postgres table,
   `handoff_cases`, with `approval_fl = 'F'` ("not yet reviewed"). This app
   never picks a specific prescription drug or device -- that choice (Ella,
   ParaGard, Mirena, or Liletta) belongs to a human clinician in the
   sibling app, informed by this intake data and a real Photon safety
   check. This app's only job is the binary: does this need a clinician,
   yes or no.

## Known scope limitation / If I had more time

The step-1 OTC/escalate gate only has hours-since and weight to go on --
medications aren't asked until step 2, and step 2 only runs if step 1
already decided to escalate. That means a patient who is otherwise within
levonorgestrel's window and weight range, but taking an enzyme-inducing
medication (e.g. rifampin), will still be told "OTC is fine" at step 1,
since that interaction is never checked before the OTC recommendation is
shown. This is a direct consequence of asking only two questions up front,
not an oversight -- flagged here rather than silently fixed, since it's a
real product tradeoff (fewer questions before the OTC answer vs. catching
every interaction pre-gate) worth a conscious decision rather than an
assumption.

This entire screen is a form, which makes it easy to grab the neccesary information needed to run a deterministic function (based on weight + time since sex) to classify if this can be done OTC or not. But if I had more time, I would love to create a chatbot that could ask these same questions and potentially make the patient feel more comfortable answering them during an intense/scary time.

# Photon calls this app makes

Exactly one: `createPatient` (core API, M2M auth), only on the escalation
path. It does **not** call `prescriptionScreen`, search treatments, or
attempt a prescription write -- those require clinical judgment or
clinician-level Photon permissions and belong entirely to
`photon-ec-clinician`.

Photon's real schema requires `first`/`last` name, `dateOfBirth`, and
`phone` as mandatory `createPatient` arguments -- this only surfaces once
you run against the live sandbox (a `MissingFieldArgument` GraphQL error),
not from this sandbox's mock fallback. Step 2 collects all four for real
(with light phone normalization to the `+1XXXXXXXXXX` shape Photon's
`AWSPhone` scalar expects) rather than sending placeholders.
`PhotonClient.PLACEHOLDER_DATE_OF_BIRTH` / `PLACEHOLDER_PHONE` still exist
as an internal fallback if either somehow arrives empty, but the form
requires all four fields, so that path shouldn't normally be hit.

**Allergies and current medications are both real Photon catalog lookups,
not free-text fields.** `AllergenInput` only accepts `allergenId`
(confirmed against the live sandbox -- sending `{"name": ...}` fails with
"field that is not defined for input object type 'AllergenInput'"), and
`MedHistoryInput` only accepts `medicationId` (plus `active`/`comment`) the
same way. So each allergy and each medication a patient types is resolved
against Photon's own catalogs first -- `search_allergens()` for allergies,
`search_treatments()` for medications -- before `createPatient` runs.
`search_treatments()` deliberately queries the **clinical API's**
`treatments` catalog, not the core API's own `medications` query: confirmed
live that the core API's `medications` catalog is missing common generic
names outright (a filtered search for "rifampin" came back empty), while
`treatments` -- the exact same catalog `photon-ec-clinician`'s
`search_treatments` already uses to resolve the four EC buttons -- has it,
and the resulting ID round-trips correctly as `MedHistoryInput.medicationId`
on `createPatient` (confirmed live). Anything that doesn't match either
catalog isn't silently dropped -- it's left off the Photon record but
reported back as `unresolved_allergies` / `unresolved_medications`, shown
to the patient on the confirmation page. (`create_patient()` originally
accepted `medication_names` but never actually included them in the
mutation -- a real bug found and fixed while building this; nothing typed
into the medications field was ever synced to Photon before.)

Note this app's own `handoff_cases` row does **not** store any of
allergies/medications/name -- `photon-ec-clinician` queries them back live
from the Photon patient record this app just created, by
`photon_patient_id`, rather than this table carrying a second copy. See its
README for the full reasoning and the tradeoffs that introduces.

## Setup

```
cd backend
pip install -r requirements.txt
cp ../.env.example ../.env   # fill in PHOTON_* and DATABASE_URL
```

**Postgres**: this app and `photon-ec-clinician` share one Postgres
database via `DATABASE_URL`. Locally:

```
createdb photon_ec
# DATABASE_URL=postgresql://localhost:5432/photon_ec   (adjust user/password as needed)
```

The `handoff_cases` table is created automatically on first run
(`db.init_db()`, idempotent `CREATE TABLE IF NOT EXISTS`) -- whichever of
the two apps starts first bootstraps it.

```
python3 app.py
# -> http://localhost:5000
```
