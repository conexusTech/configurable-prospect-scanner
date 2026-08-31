"""The batch sizes are a TWO-REPO CONTRACT. This is our half of the pin.

🔴 `aeo-backend` prices a run BEFORE it is dispatched, freezes the number into
`scan_runs.est_cost`, and shows it to the customer as what the run will cost. Its
formula is `ceil(entities / batch)` using constants that MIRROR the ones here
(`BATCH_BY_LANES` / `CONTACT_BATCH` in `src/backend/scan-runs/prospect-cost.constants.ts`).

If these move and the gateway's do not, the estimate drifts from the bill and **nothing
detects the gap** — an estimate is not reconciled against actuals automatically, and the
variance would read as ordinary model-cost noise. So the values are asserted literally
here and literally there, and §10's re-measurement must change both.
"""

from aeo.phases._batching import CONTACT_BATCH, batch_size_for, chunk


class TestTheGatewayMirrorsTheseNumbers:
    def test_batch_size_by_group_count(self):
        # aeo-backend: BATCH_BY_LANES = {1: 8, 2: 6}, BATCH_MULTI_LANE = 5
        assert batch_size_for(1) == 8
        assert batch_size_for(2) == 6
        assert batch_size_for(3) == 5
        assert batch_size_for(4) == 5
        assert batch_size_for(9) == 5

    def test_contact_batch(self):
        # aeo-backend: CONTACT_BATCH = 6
        assert CONTACT_BATCH == 6

    def test_a_zero_or_negative_group_count_does_not_produce_a_zero_batch(self):
        # A zero batch would divide by zero on the gateway side and loop forever here.
        assert batch_size_for(0) == 8
        assert batch_size_for(-3) == 8

    def test_chunking_matches_the_gateway_formula_exactly(self):
        # The gateway prices `ceil(entities / batch)`. Anything else here — a dropped
        # remainder, an off-by-one on the final partial batch — makes the estimate wrong
        # in a way no test on either side would otherwise catch.
        import math

        for size in (5, 6, 8):
            for n in (0, 1, 4, 5, 6, 7, 13, 50, 120, 150):
                items = list(range(n))
                assert len(chunk(items, size)) == math.ceil(n / size) if n else True
                # No entity is lost or duplicated by the split.
                flat = [x for batch in chunk(items, size) for x in batch]
                assert flat == items
