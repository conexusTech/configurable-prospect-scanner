"""The commissioning organization must not appear in its own prospect list.

**Measured need.** On the first real production run (`c214e3d5`) the org — `Lee Company` —
was discovered as a prospect for itself and passed every filter. It was rejected only
because it happens to have in-house mechanical staff, i.e. by luck of an unrelated
disqualifier. An org without that quirk would have been sold to itself, and the operator
would have seen their own company at the top of a list they paid for.
"""
from __future__ import annotations

import av_lead_scanner as als

ORG = {
    "organization": {
        "name": "Lee Company",
        "aliases": ["Lee Co.", "Lee Mechanical"],
        "exclusions": ["Rock City Mechanical"],
    }
}


class TestExcludedProspectNames:
    def test_includes_the_org_its_aliases_and_its_exclusions(self):
        assert als.excluded_prospect_names(ORG) == {
            "lee company",
            "lee co",
            "lee mechanical",
            "rock city mechanical",
        }

    def test_matches_legal_suffix_and_case_variants(self):
        """Uses `normalize_name`, the same key cross-source dedup uses, so a suffix or
        punctuation difference cannot slip past."""
        excluded = als.excluded_prospect_names(ORG)
        for variant in ("Lee Company", "Lee Company, LLC", "lee company inc.", "LEE CO."):
            assert als.normalize_name(variant) in excluded, variant

    def test_does_not_exclude_an_unrelated_firm(self):
        assert als.normalize_name("Southeast Venture") not in als.excluded_prospect_names(ORG)

    def test_matching_is_on_the_FULL_normalized_name_not_a_substring(self):
        """⚠️ **Documented limitation, chosen deliberately.**

        An operator writing `Rock City Mechanical` will NOT exclude
        `Rock City Mechanical Company LLC`, because `normalize_name` strips `llc`/`inc`/
        `corp` but not `company`, and matching is exact on the result.

        Substring matching was considered and rejected: an exclusion of `Lee` would then
        silently delete `Leeds Property Group`. **A false exclusion removes a legitimate
        prospect with no error and no trace**, which is worse and less visible than a miss
        the operator can see and fix by writing the fuller name. Same reasoning as
        refusing to fuzzy-match scoring factor names onto collected fields.
        """
        excluded = als.excluded_prospect_names(ORG)
        assert als.normalize_name("Rock City Mechanical Company LLC") not in excluded
        # The fix available to the operator: write the name as it appears.
        wider = als.excluded_prospect_names(
            {"organization": {"name": "x", "exclusions": ["Rock City Mechanical Company"]}}
        )
        assert als.normalize_name("Rock City Mechanical Company LLC") in wider

    def test_tolerates_a_context_with_no_identity(self):
        assert als.excluded_prospect_names({}) == set()
        assert als.excluded_prospect_names({"organization": {}}) == set()
        assert als.excluded_prospect_names({"organization": {"name": "  "}}) == set()

    def test_falls_back_to_the_top_level_organization_name(self):
        assert als.excluded_prospect_names({"organization_name": "Lee Company"}) == {"lee company"}


class TestAssemblyDropsExcludedProspects:
    def _groups(self):
        row = lambda name: {  # noqa: E731
            "raw": {"organization_name": name},
            "source": "s",
            "name_field": "organization_name",
        }
        return {
            als.normalize_name("Lee Company"): [row("Lee Company")],
            als.normalize_name("Southeast Venture"): [row("Southeast Venture")],
        }

    def test_the_org_is_dropped_and_others_survive(self):
        out = als._assemble_prospects(
            groups=self._groups(),
            scan_run_id="run-1",
            excluded_names=als.excluded_prospect_names(ORG),
        )
        names = [p["company_name"] for p in out]
        assert "Southeast Venture" in names
        assert "Lee Company" not in names

    def test_without_an_exclusion_set_nothing_is_dropped(self):
        out = als._assemble_prospects(groups=self._groups(), scan_run_id="run-1")
        assert len(out) == 2
