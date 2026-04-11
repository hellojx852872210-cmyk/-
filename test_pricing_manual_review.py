import unittest
from datetime import datetime, timedelta

from zhuanzhuan_pricing.automation.tasks import build_reprice_decision
from zhuanzhuan_pricing.core.models import BatchItem, ConfidenceLevel, PricingResult


class BuildRepriceDecisionManualReviewTest(unittest.TestCase):
    def test_below_cost_price_requires_manual_review_without_being_clamped(self):
        item = BatchItem(
            product_id="p1",
            qc_code="qc1",
            title="test",
            cost_price=3000,
            listed_time=datetime.now() - timedelta(days=20),
        )
        pricing = PricingResult(
            title="test",
            sample_count=3,
            fast_price=2600,
            conservative_price=2500,
            floor_price=2400,
            market_floor_price=2400,
            market_cap_price=3200,
            market_base_price=2550,
            recommended_price=2500,
            confidence=ConfidenceLevel.HIGH,
        )

        decision = build_reprice_decision(item, pricing, apply_rules=False)

        self.assertEqual(decision["final_price"], 2400)
        self.assertTrue(decision["below_cost"])
        self.assertTrue(decision["needs_manual_review"])
        self.assertGreater(decision["cost_floor"], decision["final_price"])
        self.assertIn("需人工确认", decision["manual_review_reason"])


if __name__ == "__main__":
    unittest.main()
