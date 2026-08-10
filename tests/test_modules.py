"""Tests for the R11 plug-in interface and its review gate. Ours.

Most of these assert a REFUSAL. That is the point: this is the gate in front of
LLM-generated code that will run in the same process as a scan holding database
credentials, and the default answer is no.
"""

from __future__ import annotations

import hashlib
import textwrap

import pytest

from aeo.modules.apply import apply_modules, merge_signals_into_scored
from aeo.modules.interface import (
    MAX_SIGNALS_PER_PROSPECT,
    MODULE_API_VERSION,
    sanitize_signals,
)
from aeo.modules.loader import ModuleRejected, load_modules, modules_enabled, validate_spec

GOOD_MODULE = textwrap.dedent(
    '''
    class _M:
        name = "detector"
        api_version = "1.0"
        def signals(self, prospect, *, context):
            return {"hit": True, "note": "found"}
    MODULE = _M()
    '''
).strip()


def _write(tmp_path, body: str, filename: str = "detector.py") -> tuple[dict, object]:
    path = tmp_path / filename
    path.write_text(body, encoding="utf-8")
    spec = {
        "name": "detector",
        "path": filename,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "api_version": MODULE_API_VERSION,
        "reviewed_by": "reviewer@example.com",
        "reviewed_at": "2026-08-04T00:00:00Z",
    }
    return spec, path


class TestOffAtLaunch:
    def test_disabled_by_default(self, monkeypatch):
        # Decision D5. Shipping the interface must change no behaviour.
        monkeypatch.delenv("SCANNER_CUSTOM_MODULES_ENABLED", raising=False)
        assert modules_enabled() is False

    def test_loads_nothing_and_says_why(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SCANNER_CUSTOM_MODULES_ENABLED", raising=False)
        spec, _ = _write(tmp_path, GOOD_MODULE)
        rejects = []
        assert load_modules([spec], base_dir=tmp_path, on_reject=lambda n, r: rejects.append(r)) == []
        assert "R11 is off" in rejects[0]

    def test_only_the_exact_string_true_enables_it(self, monkeypatch):
        for value in ("1", "yes", "TRUE", "True", ""):
            monkeypatch.setenv("SCANNER_CUSTOM_MODULES_ENABLED", value)
            assert modules_enabled() is False, value
        monkeypatch.setenv("SCANNER_CUSTOM_MODULES_ENABLED", "true")
        assert modules_enabled() is True


class TestGate:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv("SCANNER_CUSTOM_MODULES_ENABLED", "true")

    def test_loads_a_reviewed_module(self, tmp_path):
        spec, _ = _write(tmp_path, GOOD_MODULE)
        loaded = load_modules([spec], base_dir=tmp_path)
        assert [m.name for m in loaded] == ["detector"]

    def test_rejects_a_checksum_mismatch(self, tmp_path):
        # The load-bearing check: without it "reviewed" refers to bytes that may
        # since have changed.
        spec, path = _write(tmp_path, GOOD_MODULE)
        path.write_text(GOOD_MODULE + "\nimport os\n", encoding="utf-8")
        with pytest.raises(ModuleRejected, match="checksum mismatch"):
            validate_spec(spec, base_dir=tmp_path)

    def test_rejects_a_path_escaping_the_module_directory(self, tmp_path):
        # A spec is data that arrived over the wire.
        spec, _ = _write(tmp_path, GOOD_MODULE)
        spec["path"] = "../../etc/passwd"
        with pytest.raises(ModuleRejected, match="escapes"):
            validate_spec(spec, base_dir=tmp_path)

    def test_rejects_an_absolute_path(self, tmp_path):
        spec, path = _write(tmp_path, GOOD_MODULE)
        spec["path"] = str(path.resolve().parent.parent / "elsewhere.py")
        with pytest.raises(ModuleRejected):
            validate_spec(spec, base_dir=tmp_path)

    @pytest.mark.parametrize("field", ["name", "sha256", "reviewed_by", "reviewed_at", "api_version"])
    def test_rejects_a_spec_missing_any_review_field(self, tmp_path, field):
        spec, _ = _write(tmp_path, GOOD_MODULE)
        spec[field] = ""
        with pytest.raises(ModuleRejected):
            validate_spec(spec, base_dir=tmp_path)

    def test_rejects_a_module_reviewed_against_an_older_api(self, tmp_path):
        # A contract change invalidates prior reviews — the reviewer approved code
        # against different expectations.
        spec, _ = _write(tmp_path, GOOD_MODULE)
        spec["api_version"] = "0.9"
        with pytest.raises(ModuleRejected, match="needs re-review"):
            validate_spec(spec, base_dir=tmp_path)

    def test_rejects_a_module_whose_declared_name_disagrees_with_its_spec(self, tmp_path):
        # Signals are namespaced by module name; a mismatch files them under the
        # wrong module.
        body = GOOD_MODULE.replace('name = "detector"', 'name = "something_else"')
        spec, _ = _write(tmp_path, body)
        rejects = []
        assert load_modules([spec], base_dir=tmp_path, on_reject=lambda n, r: rejects.append(r)) == []
        assert "wrong module" in rejects[0]

    def test_rejects_a_module_with_no_MODULE_attribute(self, tmp_path):
        spec, _ = _write(tmp_path, "x = 1")
        rejects = []
        assert load_modules([spec], base_dir=tmp_path, on_reject=lambda n, r: rejects.append(r)) == []
        assert "MODULE" in rejects[0]

    def test_a_module_that_explodes_on_import_is_reported_not_raised(self, tmp_path):
        # Import IS execution — there is no inspect-without-running. A module that
        # throws at its top level must not take the scan down with it.
        spec, _ = _write(tmp_path, "raise RuntimeError('boom at import')")
        rejects = []
        assert load_modules([spec], base_dir=tmp_path, on_reject=lambda n, r: rejects.append(r)) == []
        assert "failed to import" in rejects[0]

    def test_one_bad_module_does_not_block_a_good_one(self, tmp_path):
        good, _ = _write(tmp_path, GOOD_MODULE, "good.py")
        good["name"] = "detector"
        bad, _ = _write(tmp_path, "raise RuntimeError('x')", "bad.py")
        bad["name"] = "bad"
        loaded = load_modules([bad, good], base_dir=tmp_path, on_reject=lambda n, r: None)
        assert [m.name for m in loaded] == ["detector"]


class TestSanitizeSignals:
    def test_namespaces_keys_by_module(self):
        # Two modules cannot overwrite each other even if they pick the same key.
        assert sanitize_signals({"hit": True}, module_name="a") == {"a.hit": True}

    def test_drops_nested_structures_rather_than_flattening(self):
        # Flattening invents key names no reviewer approved.
        out = sanitize_signals({"ok": 1, "nested": {"a": 1}, "list": [1]}, module_name="m")
        assert out == {"m.ok": 1}

    def test_truncates_long_strings(self):
        out = sanitize_signals({"note": "x" * 5000}, module_name="m")
        assert len(out["m.note"]) == 500

    def test_caps_the_number_of_signals(self):
        out = sanitize_signals({f"k{i}": i for i in range(100)}, module_name="m")
        assert len(out) == MAX_SIGNALS_PER_PROSPECT

    def test_a_non_dict_return_yields_nothing(self):
        for value in (None, "text", 42, [1, 2]):
            assert sanitize_signals(value, module_name="m") == {}


class TestApply:
    class _Good:
        name = "good"
        api_version = MODULE_API_VERSION
        def signals(self, prospect, *, context):
            return {"seen": prospect["company_name"]}

    class _Explodes:
        name = "bad"
        api_version = MODULE_API_VERSION
        def signals(self, prospect, *, context):
            raise RuntimeError("boom")

    PROSPECTS = [{"id": "p1", "company_name": "First"}, {"id": "p2", "company_name": "Second"}]

    def test_collects_signals_per_prospect(self):
        out = apply_modules(self.PROSPECTS, [self._Good()], context={})
        assert out == {"p1": {"good.seen": "First"}, "p2": {"good.seen": "Second"}}

    def test_a_module_that_raises_does_not_stop_the_others(self):
        errors = []
        out = apply_modules(
            self.PROSPECTS, [self._Explodes(), self._Good()],
            context={}, on_error=lambda m, p, e: errors.append(m),
        )
        assert out["p1"] == {"good.seen": "First"}
        assert errors == ["bad", "bad"]

    def test_no_modules_means_no_work(self):
        assert apply_modules(self.PROSPECTS, [], context={}) == {}

    def test_skips_prospects_with_no_id(self):
        assert apply_modules([{"company_name": "No Id"}], [self._Good()], context={}) == {}


class TestMergeSignalsIntoScored:
    """Signals ride the `scored` event — AEO's second per-prospect write."""

    def test_attaches_signals_to_the_matching_scored_item(self):
        scored = [{"prospect_id": "p1", "score": 80}, {"prospect_id": "p2", "score": 70}]
        out = merge_signals_into_scored(scored, {"p1": {"good.seen": "First"}})
        assert out[0]["custom_signals"] == {"good.seen": "First"}
        assert "custom_signals" not in out[1]

    def test_no_signals_leaves_scored_untouched(self):
        scored = [{"prospect_id": "p1", "score": 80}]
        assert merge_signals_into_scored(scored, {}) == [{"prospect_id": "p1", "score": 80}]

    def test_drops_signals_for_a_prospect_that_did_not_survive_to_scoring(self):
        # AEO keys the write on prospect_id, so an orphan would match no row.
        # Not emitting it is honest; emitting it would look like data that landed.
        scored = [{"prospect_id": "p1", "score": 80}]
        out = merge_signals_into_scored(scored, {"gone": {"good.seen": "X"}})
        assert out == [{"prospect_id": "p1", "score": 80}]

    def test_merges_rather_than_replacing(self):
        scored = [{"prospect_id": "p1", "custom_signals": {"a.x": 1}}]
        out = merge_signals_into_scored(scored, {"p1": {"b.y": 2}})
        assert out[0]["custom_signals"] == {"a.x": 1, "b.y": 2}

    def test_applying_the_same_signals_twice_is_idempotent(self):
        # Both ends merge (here, and `custom_fields || custom_signals` in AEO),
        # so a replayed callback must not compound.
        scored = [{"prospect_id": "p1"}]
        once = merge_signals_into_scored(scored, {"p1": {"a.x": 1}})
        twice = merge_signals_into_scored(once, {"p1": {"a.x": 1}})
        assert twice[0]["custom_signals"] == {"a.x": 1}

    def test_end_to_end_shape_is_what_the_gateway_dto_declares(self):
        """apply → merge produces `custom_signals`: flat, namespaced, primitives.

        Pinned as one chain rather than two units because the DTO on the other
        side accepts exactly this and nothing re-derives it there.
        """
        prospects = [{"id": "p1", "company_name": "First"}]
        signals = apply_modules(prospects, [TestApply._Good()], context={})
        out = merge_signals_into_scored([{"prospect_id": "p1"}], signals)
        blob = out[0]["custom_signals"]
        assert blob == {"good.seen": "First"}
        assert all(isinstance(k, str) and "." in k for k in blob)
        assert all(isinstance(v, (str, int, float, bool, type(None))) for v in blob.values())
