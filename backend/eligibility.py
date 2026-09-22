"""
Deterministic emergency contraception (EC) eligibility engine.

This is where the eligibility for whether they should see a clinician happens.
The logic was created based on weight and hours since sex. There is no LLM
call anywhere in this module. Claude (see assistant.py) is
only ever handed this module's *output* to translate into a friendly
explanation; it never originates a threshold, a dose, or a "you should/
shouldn't take this" judgment itself. An LLM can misremember a cutoff or
hedge inconsistently, and we don't want to rely on an LLM for medical diagnosing.


DISCLAIMER: The thresholds below reflect commonly cited public
guidance current as of this writing (see SOURCES) but have NOT been
reviewed by a clinician or pharmacist for this project, and small details of
real EC guidance shift over time and by formulation.

SOURCES (public clinical guidance this logic is modeled on):
  - FDA prescribing information / labeling for levonorgestrel EC products
    (e.g. Plan B One-Step): labeled window is within 72 hours of unprotected
    intercourse.
  - FDA prescribing information for ulipristal acetate (ella): labeled
    window is within 120 hours (5 days).
  - Copper IUD (e.g. Paragard) placement for EC: effective within 120 hours
    (5 days), per ACOG and CDC/US Selected Practice Recommendations (US
    SPR) guidance on EC.
  - Glasier A, et al. "Can we identify women at risk of pregnancy despite
    using emergency contraception? Data from randomized trials of
    ulipristal acetate and levonorgestrel." Contraception, 2011 -- the
    primary dataset behind the widely cited observation that LNG efficacy
    drops sharply above ~165 lb / BMI 25, and is not reliably effective
    above ~195 lb / BMI 30, with ulipristal degrading less steeply but
    still losing reliability above similar cutoffs. Also reflected in
    WHO/EMA product-label discussions following that analysis.
  - CDC US Selected Practice Recommendations for Contraceptive Use, EC
    section: notes reduced hormonal EC effectiveness in higher-weight/BMI
    patients and that the copper IUD's effectiveness does not vary by
    weight, making it the most reliable option for higher-weight patients
    or with CYP3A4-inducer interactions.
  - CYP3A4 induction reducing hormonal EC effectiveness: rifampin,
    carbamazepine, phenytoin, topiramate, and St. John's Wort are the
    commonly cited enzyme-inducing agents flagged in EC prescribing
    guidance (Liletta/Paragard clinician references; UptoDate EC drug
    interaction summaries). This is mechanistically distinct from CYP3A4
    *inhibitors* (e.g. ritonavir-boosted regimens like Paxlovid), which
    raise drug levels rather than lower them -- see README for the note on
    why this project uses the inducer interaction, not the Paxlovid one,
    as its demo case.

"""

from __future__ import annotations

from dataclasses import dataclass, field

LB_PER_KG = 2.20462

# CYP3A4 INDUCERS -- these speed clearance of hormonal EC (LNG + ulipristal),
# reducing effectiveness. This is the deterministic interaction list Photon's
# prescriptionScreen also independently flags for; we keep our own copy here
# so the *eligibility* verdict doesn't depend on Photon being reachable, and
# cross-check it against Photon's live screen where available (see
# photon_client.screen_prescription / app.py).
CYP3A4_INDUCERS = {
    "rifampin": "Rifampin",
    "rifampicin": "Rifampin",
    "carbamazepine": "Carbamazepine",
    "phenytoin": "Phenytoin",
    "topiramate": "Topiramate",
    "st john's wort": "St. John's Wort",
    "st. john's wort": "St. John's Wort",
    "st johns wort": "St. John's Wort",
}

WEIGHT_MODERATE_LB = 165.0  # LNG efficacy starts dropping (Glasier 2011)
WEIGHT_HIGH_LB = 195.0      # LNG unreliable / ulipristal also degrading

TREATMENT_NAMES = {
    "levonorgestrel": "Levonorgestrel (e.g. Plan B One-Step)",
    "ulipristal": "Ulipristal acetate (ella)",
    "copper_iud": "Copper IUD (e.g. Paragard)",
}

# The eligibility engine's own option keys aren't drug names Photon's
# treatment search would recognize -- "ulipristal acetate" itself didn't
# surface a result in the sandbox catalog (see README); "ella" and
# "paragard" are the terms that actually match. Kept as the single source
# of truth for that mapping so app.py's initial lookup and handoff.py's
# referral-packet lookup can't drift out of sync with each other.
TREATMENT_SEARCH_TERMS = {
    "levonorgestrel": "levonorgestrel",
    "ulipristal": "ella",
    "copper_iud": "paragard",
}

# Fixed, non-clinical ACCESS/logistics facts -- "how do I actually get this,"
# not "is this safe for me." Kept here, as trusted static reference data
# alongside TREATMENT_NAMES, for the same reason as everything else in this
# file: a chat follow-up like "where can I get X" should be answered from a
# sourced fact table, not from the LLM's own recalled knowledge of pharmacy
# regulations. Source: FDA's 2013 removal of the age/ID restriction on
# levonorgestrel EC (OTC, no prescription); ella's FDA labeling requires a
# prescription; copper IUD placement requires a clinician procedure visit.
ACCESS_INFO = {
    "levonorgestrel": (
        "Available over-the-counter at most pharmacies (CVS, Walgreens, Rite Aid, "
        "grocery/big-box store pharmacies, etc.) -- no ID, prescription, or age "
        "restriction required in the US. It's sometimes kept on a locked shelf or "
        "behind the pharmacy counter for anti-theft reasons, not because of any "
        "purchase restriction -- just ask the pharmacist if you don't see it on the shelf."
    ),
    "ulipristal": (
        "Requires a prescription. That can come from a clinician visit (in person or "
        "via telehealth) or, in some states, directly from specially authorized "
        "pharmacists -- calling ahead to a local pharmacy or a clinic like Planned "
        "Parenthood to ask about same-day availability is usually the fastest path."
    ),
    "copper_iud": (
        "Requires an in-office appointment with a clinician (OB-GYN, family medicine, "
        "or a clinic like Planned Parenthood) for placement. Same-day appointments for "
        "this specifically aren't always available, so it's worth calling ahead and "
        "explaining it's time-sensitive."
    ),
}


@dataclass
class OptionResult:
    option: str
    display_name: str
    eligible: bool
    requires_clinician: bool
    reasons: list[str] = field(default_factory=list)  # why ineligible / caveats
    efficacy_note: str | None = None  # human-readable efficacy caveat, still just data


@dataclass
class EligibilityResult:
    hours_since_intercourse: float
    weight_lb: float
    interacting_meds_found: list[str]
    options: list[OptionResult]
    recommended: str | None  # option key of the top recommendation, or None
    escalate: bool
    escalate_reasons: list[str]


def kg_to_lb(kg: float) -> float:
    return round(kg * LB_PER_KG, 1)


def find_cyp3a4_inducers(medication_names: list[str]) -> list[str]:
    """Substring match against the known inducer list. Real
    version would resolve these via Photon's allergen/treatment search + a
    proper RxNorm/interaction class lookup rather than string matching --
    string matching is a demo-scope simplification"""
    found = []
    lowered = [m.lower() for m in medication_names]
    for name in lowered:
        for key, display in CYP3A4_INDUCERS.items():
            if key in name and display not in found:
                found.append(display)
    return found


def evaluate(
    hours_since_intercourse: float,
    weight_lb: float,
    current_medications: list[str],
) -> EligibilityResult:
    """Same inputs always produce the same verdict. This is
    the deterministic core"""
    inducers = find_cyp3a4_inducers(current_medications)
    hormonal_impaired = len(inducers) > 0

    options: list[OptionResult] = []

    # --- Levonorgestrel: labeled window 72h ---
    lng_reasons = []
    lng_eligible = hours_since_intercourse <= 72
    if not lng_eligible:
        lng_reasons.append(
            f"It's been about {hours_since_intercourse:.0f} hours since unprotected sex; "
            "levonorgestrel is only labeled effective within 72 hours (3 days)."
        )
    lng_efficacy_note = None
    if lng_eligible:
        if weight_lb >= WEIGHT_HIGH_LB:
            lng_eligible = False
            lng_reasons.append(
                f"At {weight_lb:.0f} lb, levonorgestrel is not considered reliably effective "
                f"(data shows a sharp efficacy drop above ~{WEIGHT_HIGH_LB:.0f} lb)."
            )
        elif weight_lb >= WEIGHT_MODERATE_LB:
            lng_efficacy_note = (
                f"At {weight_lb:.0f} lb, levonorgestrel's effectiveness is reduced "
                f"(data shows declining efficacy above ~{WEIGHT_MODERATE_LB:.0f} lb) -- "
                "ulipristal or a copper IUD is more reliable at this weight."
            )
    if lng_eligible and hormonal_impaired:
        lng_efficacy_note = (
            (lng_efficacy_note + " " if lng_efficacy_note else "")
            + f"Also flagged: {', '.join(inducers)} can reduce hormonal EC effectiveness "
              "by speeding how quickly the body clears it."
        )
    options.append(
        OptionResult(
            option="levonorgestrel",
            display_name=TREATMENT_NAMES["levonorgestrel"],
            eligible=lng_eligible,
            requires_clinician=False,
            reasons=lng_reasons,
            efficacy_note=lng_efficacy_note,
        )
    )

    # --- Ulipristal acetate (ella): labeled window 120h ---
    upa_reasons = []
    upa_eligible = hours_since_intercourse <= 120
    if not upa_eligible:
        upa_reasons.append(
            f"It's been about {hours_since_intercourse:.0f} hours since unprotected sex; "
            "ulipristal is only labeled effective within 120 hours (5 days)."
        )
    upa_efficacy_note = None
    if upa_eligible:
        if weight_lb >= WEIGHT_HIGH_LB:
            upa_efficacy_note = (
                f"At {weight_lb:.0f} lb, ulipristal's effectiveness is also reduced, though less "
                "steeply than levonorgestrel's -- a copper IUD is the most reliable option at this weight."
            )
    if upa_eligible and hormonal_impaired:
        upa_efficacy_note = (
            (upa_efficacy_note + " " if upa_efficacy_note else "")
            + f"Also flagged: {', '.join(inducers)} can reduce hormonal EC effectiveness "
              "by speeding how quickly the body clears it -- a copper IUD isn't affected by this."
        )
    options.append(
        OptionResult(
            option="ulipristal",
            display_name=TREATMENT_NAMES["ulipristal"],
            eligible=upa_eligible,
            requires_clinician=True,  # prescription required
            reasons=upa_reasons,
            efficacy_note=upa_efficacy_note,
        )
    )

    # --- Copper IUD: labeled window 120h, no weight-based efficacy drop ---
    iud_reasons = []
    iud_eligible = hours_since_intercourse <= 120
    if not iud_eligible:
        iud_reasons.append(
            f"It's been about {hours_since_intercourse:.0f} hours since unprotected sex; "
            "copper IUD placement for EC is only effective within 120 hours (5 days)."
        )
    options.append(
        OptionResult(
            option="copper_iud",
            display_name=TREATMENT_NAMES["copper_iud"],
            eligible=iud_eligible,
            requires_clinician=True,  # in-office placement
            reasons=iud_reasons,
            efficacy_note=(
                "Effectiveness does not vary by weight and is not reduced by the "
                "medications flagged above -- the most reliable single option, "
                "though it requires an in-office appointment."
                if iud_eligible
                else None
            ),
        )
    )

    # --- Pick a recommendation deterministically ---
    # Priority: if nothing is eligible -> None (escalate).
    # Else prefer, in order: an eligible option with no efficacy caveat and no
    # clinician requirement (LNG, OTC) > copper IUD (most robust) > ulipristal.
    recommended = None
    by_option = {o.option: o for o in options}
    if by_option["levonorgestrel"].eligible and not by_option["levonorgestrel"].efficacy_note:
        recommended = "levonorgestrel"
    elif by_option["copper_iud"].eligible:
        recommended = "copper_iud"
    elif by_option["ulipristal"].eligible:
        recommended = "ulipristal"
    elif by_option["levonorgestrel"].eligible:
        recommended = "levonorgestrel"  # eligible but with a caveat, last resort

    escalate = recommended is None
    escalate_reasons = []
    if escalate:
        escalate_reasons.append(
            "No EC option is within its effective time window and/or reliable at this "
            "weight based on the information given -- this needs a clinician, not a "
            "chatbot recommendation."
        )

    return EligibilityResult(
        hours_since_intercourse=hours_since_intercourse,
        weight_lb=weight_lb,
        interacting_meds_found=inducers,
        options=options,
        recommended=recommended,
        escalate=escalate,
        escalate_reasons=escalate_reasons,
    )


def result_to_dict(result: EligibilityResult) -> dict:
    return {
        "hours_since_intercourse": result.hours_since_intercourse,
        "weight_lb": result.weight_lb,
        "interacting_meds_found": result.interacting_meds_found,
        "recommended": result.recommended,
        "escalate": result.escalate,
        "escalate_reasons": result.escalate_reasons,
        "options": [
            {
                "option": o.option,
                "display_name": o.display_name,
                "eligible": o.eligible,
                "requires_clinician": o.requires_clinician,
                "reasons": o.reasons,
                "efficacy_note": o.efficacy_note,
            }
            for o in result.options
        ],
    }


if __name__ == "__main__":
    # Quick self-check of the boundary conditions, run with:
    #   python3 eligibility.py
    cases = [
        ("clean, within window, average weight", 20, 140, []),
        ("just past LNG window, ulipristal still fine", 80, 140, []),
        ("past everything", 200, 140, []),
        ("high weight, LNG unreliable, ulipristal/IUD still ok", 30, 210, []),
        ("moderate weight, LNG reduced but still listed", 10, 175, []),
        ("on rifampin: hormonal flagged, IUD unaffected", 24, 140, ["Rifampin 300mg"]),
    ]
    for label, hours, weight, meds in cases:
        r = evaluate(hours, weight, meds)
        print(f"\n=== {label} ===")
        print(f"  recommended: {r.recommended}  escalate: {r.escalate}")
        for o in r.options:
            print(f"  - {o.option}: eligible={o.eligible} note={o.efficacy_note or o.reasons}")
