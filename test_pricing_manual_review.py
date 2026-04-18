import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from zhuanzhuan_pricing.automation.tasks import (
    _match_detail_for_erp_item,
    build_reprice_decision,
    task_auto_list,
    task_auto_reprice,
    task_stale_drop,
)
from zhuanzhuan_pricing.core.batch_store import BatchItemStore
from zhuanzhuan_pricing.core.models import (
    Account,
    BatchItem,
    ConfidenceLevel,
    PricingResult,
    ProductDetail,
    ProductStatus,
    SoldRecord,
)
from zhuanzhuan_pricing.core.utils import fuzzy_model_match
from zhuanzhuan_pricing.services.reprice_service import (
    pricing_preview,
    recalc_decision_with_final_price,
    run_reprice_pipeline,
)
from zhuanzhuan_pricing.services.zhuanzhuan_api import ImeiService


class FuzzyModelMatchTest(unittest.TestCase):
    def test_single_token_query_still_supports_fuzzy_match(self):
        self.assertTrue(fuzzy_model_match("14", "Apple iPhone 14 Pro Max 256G"))

    def test_full_width_digit_query_supports_fuzzy_match(self):
        self.assertTrue(fuzzy_model_match("１４", "Apple iPhone 14 Pro Max 256G"))

    def test_multi_token_query_does_not_match_longer_suffix_variant(self):
        self.assertFalse(fuzzy_model_match("14pro", "Apple iPhone 14 Pro Max 256G"))

    def test_longer_suffix_query_does_not_match_shorter_variant(self):
        self.assertFalse(fuzzy_model_match("14promax", "Apple iPhone 14 Pro 256G"))

    def test_multi_token_exact_model_still_matches(self):
        self.assertTrue(fuzzy_model_match("14pro", "Apple iPhone 14 Pro 256G"))


class _StubSoldCache:
    def __init__(self, records):
        self._records = list(records)

    def filter_by_model_key(self, model="", condition="", capacity="", color=""):
        return list(self._records)


class _StubAccountStore:
    def __init__(self, accounts):
        self._accounts = list(accounts)

    def enabled_accounts(self):
        return list(self._accounts)


class _StubHistoryDb:
    def __init__(self):
        self.records = []

    def record(self, change):
        self.records.append(change)


class _FakeImeiService:
    details_by_qc = {}
    details_by_imei = {}
    changed_prices = []
    listed_prices = []

    def __init__(self, account_name, cookie):
        self.account_name = account_name
        self.cookie = cookie

    @classmethod
    def reset(cls):
        cls.details_by_qc = {}
        cls.details_by_imei = {}
        cls.changed_prices = []
        cls.listed_prices = []

    def check_cookie_valid(self):
        return True, ""

    def query_by_qc_code(self, qc_code):
        return self.details_by_qc.get(qc_code)
    def query_by_imei(self, imei):
        return self.details_by_imei.get(imei)

    def _query_product_by_id(self, product_id, statuses=()):
        for detail in self.details_by_qc.values():
            if getattr(detail, "product_id", "") == product_id:
                return detail
        return None

    def estimate_settle_price(self, product_id, price):
        return price - 100 if price else None

    def change_price(self, detail, new_price):
        self.changed_prices.append((detail.product_id, new_price))
        detail.current_price = new_price
        detail.settle_price = new_price - 100
        return True, "改价成功"

    def list_product(self, product_id, price, qc_code):
        self.listed_prices.append((product_id, price, qc_code))
        detail = self.details_by_qc.get(qc_code)
        if detail is not None:
            detail.current_price = price
            detail.settle_price = price - 100
            detail.status = ProductStatus.ON_SALE
        return True, "上架成功"


class ImeiServiceBatchLookupTest(unittest.TestCase):
    def test_product_status_maps_status_70_to_on_sale(self):
        self.assertEqual(ProductStatus.from_raw("70"), ProductStatus.ON_SALE)

    def test_fetch_by_codes_splits_imei_and_qc_batches(self):
        svc = ImeiService("shop-a", "cookie-a")
        calls = []

        def fake_list(query):
            calls.append(query)
            return {"code": 0, "data": {"list": []}}

        svc._merchant_product_list = fake_list
        svc._fallback_fetch_by_codes = lambda codes, statuses: []

        details, missing = svc.fetch_by_codes(["998005980", "351678912726500"])

        self.assertEqual(details, [])
        self.assertEqual(missing, ["998005980", "351678912726500"])
        self.assertEqual(len(calls), 10)
        self.assertEqual(calls[0]["statusList"], ["70"])
        imei_calls = [query for query in calls if "imeis" in query]
        qc_calls = [query for query in calls if "qcCodes" in query]
        self.assertEqual(len(imei_calls), 5)
        self.assertEqual(len(qc_calls), 5)
        self.assertTrue(all(query["imeis"] == ["351678912726500"] for query in imei_calls))
        self.assertTrue(all(query["qcCodes"] == ["998005980"] for query in qc_calls))

    def test_fetch_by_codes_respects_custom_batch_size(self):
        svc = ImeiService("shop-a", "cookie-a")
        calls = []

        def fake_list(query):
            calls.append(query)
            return {"code": 0, "data": {"list": []}}

        svc._merchant_product_list = fake_list
        svc._fallback_fetch_by_codes = lambda codes, statuses: []

        codes = [f"qc{i}" for i in range(45)]
        details, missing = svc.fetch_by_codes(codes, batch_size=20)

        self.assertEqual(details, [])
        self.assertEqual(missing, codes)
        self.assertEqual(len(calls), 15)
        qc_calls = [query for query in calls if "qcCodes" in query]
        self.assertEqual(len(qc_calls), 15)
        chunk_sizes = [len(query["qcCodes"]) for query in qc_calls]
        self.assertEqual(chunk_sizes, [20] * 5 + [20] * 5 + [5] * 5)


    def test_fetch_by_codes_fallback_does_not_abort_when_single_code_lookup_raises(self):
        svc = ImeiService("shop-a", "cookie-a")
        fallback_calls = []

        def fail_batch(query):
            raise RuntimeError("商家接口返回错误: 系统异常")

        def fake_query(field_name, value, statuses, fallback_code=""):
            fallback_calls.append((field_name, value))
            if value == "bad-qc":
                raise RuntimeError("single lookup failed")
            return ProductDetail(
                product_id="p-good",
                qc_code="good-qc",
                title="ok",
                current_price=1000,
                status=ProductStatus.ON_SALE,
            )

        svc._merchant_product_list = fail_batch
        svc._query_by_field = fake_query

        details, missing = svc.fetch_by_codes(["bad-qc", "good-qc"], batch_size=20)

        self.assertEqual([detail.product_id for detail in details], ["p-good"])
        self.assertEqual(missing, ["bad-qc"])
        self.assertIn(("qcCodes", "bad-qc"), fallback_calls)
        self.assertIn(("imeis", "bad-qc"), fallback_calls)
        self.assertIn(("qcCodes", "good-qc"), fallback_calls)

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

        self.assertEqual(decision["final_price"], 2398)
        self.assertEqual(decision["final_price"] % 10, 8)
        self.assertTrue(decision["needs_manual_review"])
        self.assertGreater(decision["cost_floor"], decision["final_price"])
        self.assertIn("需人工确认", decision["manual_review_reason"])


class RunRepricePipelineTest(unittest.TestCase):
    def test_pipeline_returns_pricing_decision_preview_and_suggested_values(self):
        item = BatchItem(
            product_id="p2",
            qc_code="qc2",
            title="iPhone 14",
            model="iPhone 14",
            condition="95新",
            capacity="128G",
            color="黑色",
            current_price=3200,
            cost_price=2600,
            listed_time=datetime.now() - timedelta(days=5),
        )
        records = [
            SoldRecord(
                product_id="s1",
                title="iPhone 14",
                sold_price=3450,
                sold_time=datetime.now() - timedelta(days=1),
                model="iPhone 14",
                condition="95新",
                capacity="128G",
                color="黑色",
            ),
            SoldRecord(
                product_id="s2",
                title="iPhone 14",
                sold_price=3380,
                sold_time=datetime.now() - timedelta(days=2),
                model="iPhone 14",
                condition="95新",
                capacity="128G",
                color="黑色",
            ),
            SoldRecord(
                product_id="s3",
                title="iPhone 14",
                sold_price=3420,
                sold_time=datetime.now() - timedelta(days=3),
                model="iPhone 14",
                condition="95新",
                capacity="128G",
                color="黑色",
            ),
        ]

        pipeline = run_reprice_pipeline(item, _StubSoldCache(records), apply_rules=False)

        self.assertIsNotNone(pipeline["pricing"])
        self.assertIsInstance(pipeline["decision"], dict)
        self.assertIsInstance(pipeline["preview"], str)
        self.assertTrue(pipeline["preview"])
        self.assertEqual(pipeline["suggested_price"], pipeline["decision"]["final_price"])
        self.assertEqual(pipeline["suggested_settle_price"], pipeline["decision"]["final_settle_price"])

    def test_pipeline_without_samples_returns_empty_suggestion_and_stable_preview(self):
        item = BatchItem(
            product_id="p3",
            qc_code="qc3",
            title="Unknown Phone",
            model="Unknown Phone",
            condition="95新",
            current_price=2000,
        )

        pipeline = run_reprice_pipeline(item, _StubSoldCache([]), apply_rules=False)

        self.assertIsNotNone(pipeline["pricing"])
        self.assertEqual(pipeline["pricing"].sample_count, 0)
        self.assertIsNone(pipeline["suggested_price"])
        self.assertIn("样本 0", pipeline["preview"])
        self.assertIn("当前 2000 -> 最终 -", pipeline["preview"])
        self.assertIn("提示 暂无成交样本", pipeline["preview"])

    def test_pipeline_marks_manual_review_when_final_price_is_below_cost(self):
        item = BatchItem(
            product_id="p4",
            qc_code="qc4",
            title="test",
            model="test",
            cost_price=3000,
            listed_time=datetime.now() - timedelta(days=20),
        )
        records = [
            SoldRecord(
                product_id="s4",
                title="test",
                sold_price=2450,
                sold_time=datetime.now() - timedelta(days=1),
                model="test",
            ),
            SoldRecord(
                product_id="s5",
                title="test",
                sold_price=2500,
                sold_time=datetime.now() - timedelta(days=2),
                model="test",
            ),
            SoldRecord(
                product_id="s6",
                title="test",
                sold_price=2520,
                sold_time=datetime.now() - timedelta(days=3),
                model="test",
            ),
        ]

        pipeline = run_reprice_pipeline(item, _StubSoldCache(records), apply_rules=False)

        self.assertTrue(pipeline["decision"]["below_cost"])
        self.assertTrue(pipeline["decision"]["needs_manual_review"])
        self.assertIn("需人工确认", pipeline["decision"]["manual_review_reason"])
        self.assertIn("需人工确认", pipeline["preview"])

    def test_pipeline_exposes_structured_decision_fields_and_summary_first_preview(self):
        item = BatchItem(
            product_id="p5",
            qc_code="qc5",
            title="iPhone 14",
            model="iPhone 14",
            condition="95新",
            capacity="128G",
            color="黑色",
            current_price=3200,
            cost_price=2600,
            listed_time=datetime.now() - timedelta(days=5),
        )
        records = [
            SoldRecord("s51", "iPhone 14", 3450, datetime.now() - timedelta(days=1), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s52", "iPhone 14", 3380, datetime.now() - timedelta(days=2), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s53", "iPhone 14", 3420, datetime.now() - timedelta(days=3), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
        ]

        pipeline = run_reprice_pipeline(item, _StubSoldCache(records), apply_rules=False)
        decision = pipeline["decision"]
        preview = pipeline["preview"]

        self.assertEqual(decision["current_price"], 3200)
        self.assertIsNotNone(decision["pricing_anchor_price"])
        self.assertIsNotNone(decision["base_candidate_price"])
        self.assertEqual(decision["rule_adjusted_price"], decision["system_price"])
        self.assertEqual(decision["price_delta"], decision["final_price"] - decision["current_price"])
        self.assertFalse(decision["decision_flags"]["needs_manual_review"])
        self.assertTrue(any("市场信号" in step for step in decision["decision_steps"]))
        self.assertTrue(any("相对当前价" in step for step in decision["decision_steps"]))
        self.assertIn("当前 3200 -> 最终", preview)
        self.assertIn("预计到手", preview)
        self.assertIn("系统建议价", preview)
        self.assertIn("相对当前", preview)
        self.assertIn("决策链路", preview)

    def test_pricing_preview_includes_manual_review_loss_summary(self):
        item = BatchItem(
            product_id="p6",
            qc_code="qc6",
            title="test",
            model="test",
            current_price=3200,
            cost_price=3000,
            listed_time=datetime.now() - timedelta(days=20),
        )
        records = [
            SoldRecord("s61", "test", 2450, datetime.now() - timedelta(days=1), model="test"),
            SoldRecord("s62", "test", 2500, datetime.now() - timedelta(days=2), model="test"),
            SoldRecord("s63", "test", 2520, datetime.now() - timedelta(days=3), model="test"),
        ]

        pipeline = run_reprice_pipeline(item, _StubSoldCache(records), apply_rules=False)
        preview = pricing_preview(pipeline["pricing"], None, item, pipeline["decision"])

        self.assertIn("需人工确认", preview)
        self.assertIn("预计亏损", preview)
        self.assertIn("成本底线", preview)
        self.assertIn("决策链路", preview)

    def test_recalc_decision_keeps_stage_reason_in_steps_and_preview(self):
        item = BatchItem(
            product_id="p7",
            qc_code="qc7",
            title="iPhone 14",
            model="iPhone 14",
            condition="95新",
            capacity="128G",
            color="黑色",
            current_price=3200,
            cost_price=2600,
            listed_time=datetime.now() - timedelta(days=20),
        )
        records = [
            SoldRecord("s71", "iPhone 14", 3450, datetime.now() - timedelta(days=1), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s72", "iPhone 14", 3380, datetime.now() - timedelta(days=2), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s73", "iPhone 14", 3420, datetime.now() - timedelta(days=3), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
        ]

        pipeline = run_reprice_pipeline(item, _StubSoldCache(records), apply_rules=False)
        updated = recalc_decision_with_final_price(
            item,
            pipeline["decision"],
            2800,
            extra_steps=["阶段2：按滞销规则额外下调 20%"],
        )
        preview = pricing_preview(pipeline["pricing"], None, item, updated)

        self.assertEqual(updated["final_price"], 3378)
        self.assertEqual(updated["final_price"] % 10, 8)
        self.assertEqual(updated["price_delta"], 178)
        self.assertTrue(any("阶段2" in step for step in updated["decision_steps"]))
        self.assertTrue(any("阶段2" in line for line in updated["explain_lines"]))
        self.assertIn("相对当前 +178", preview)
        self.assertIn("决策链路", preview)

    def setUp(self):
        _FakeImeiService.reset()
        self.account_store = _StubAccountStore([Account(name="shop-a", cookie="cookie-a")])
        self.history_db = _StubHistoryDb()

    def _patch_task_runtime(self):
        return patch.multiple(
            "zhuanzhuan_pricing.automation.tasks",
            ImeiService=_FakeImeiService,
            get_history_db=lambda: self.history_db,
        )

    def test_task_auto_reprice_updates_store_and_records_history(self):
        imported_store = BatchItemStore()
        imported_store.extend([
            BatchItem(
                product_id="p10",
                qc_code="qc10",
                title="iPhone 14",
                model="iPhone 14",
                condition="95新",
                capacity="128G",
                color="黑色",
                current_price=3200,
                cost_price=2600,
                account_name="shop-a",
                status=ProductStatus.ON_SALE,
                listed_time=datetime.now() - timedelta(days=5),
            )
        ])
        _FakeImeiService.details_by_qc["qc10"] = ProductDetail(
            product_id="p10",
            qc_code="qc10",
            title="iPhone 14",
            current_price=3200,
            status=ProductStatus.ON_SALE,
            model="iPhone 14",
            condition="95新",
            capacity="128G",
            color="黑色",
            listed_time=datetime.now() - timedelta(days=5),
            settle_price=3100,
        )
        sold_cache = _StubSoldCache([
            SoldRecord("s10", "iPhone 14", 3450, datetime.now() - timedelta(days=1), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s11", "iPhone 14", 3380, datetime.now() - timedelta(days=2), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s12", "iPhone 14", 3420, datetime.now() - timedelta(days=3), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
        ])

        with self._patch_task_runtime():
            result = task_auto_reprice(self.account_store, sold_cache, {}, None, imported_store)

        item = imported_store.get("p10")
        self.assertEqual(result["ok"], 1)
        self.assertEqual(result["fail"], 0)
        self.assertTrue(item.reprice_ok)
        self.assertEqual(item.op_status, "已改价")
        self.assertIsNotNone(item.suggested_price)
        self.assertGreater(item.current_price, 3200)
        self.assertEqual(_FakeImeiService.changed_prices[0][0], "p10")
        self.assertEqual(len(self.history_db.records), 1)

    def test_task_stale_drop_marks_manual_review_when_stage_price_below_cost(self):
        imported_store = BatchItemStore()
        imported_store.extend([
            BatchItem(
                product_id="p20",
                qc_code="qc20",
                title="test",
                model="test",
                current_price=3200,
                cost_price=3000,
                account_name="shop-a",
                status=ProductStatus.ON_SALE,
                listed_time=datetime.now() - timedelta(days=20),
            )
        ])
        _FakeImeiService.details_by_qc["qc20"] = ProductDetail(
            product_id="p20",
            qc_code="qc20",
            title="test",
            current_price=3200,
            status=ProductStatus.ON_SALE,
            model="test",
            listed_time=datetime.now() - timedelta(days=20),
            settle_price=3100,
        )
        sold_cache = _StubSoldCache([
            SoldRecord("s20", "test", 2450, datetime.now() - timedelta(days=1), model="test"),
            SoldRecord("s21", "test", 2500, datetime.now() - timedelta(days=2), model="test"),
            SoldRecord("s22", "test", 2520, datetime.now() - timedelta(days=3), model="test"),
        ])

        with self._patch_task_runtime(), \
             patch.dict(
                 "zhuanzhuan_pricing.automation.tasks.cfg._data",
                 {
                     "stale_stage1_days": 7,
                     "stale_stage2_days": 15,
                     "stale_stage2_drop_pct": 20,
                 },
                 clear=False,
             ):
            result = task_stale_drop(self.account_store, sold_cache, None, imported_store)

        item = imported_store.get("p20")
        self.assertEqual(result["ok"], 0)
        self.assertEqual(result["skip"], 1)
        self.assertEqual(item.op_status, "待确认")
        self.assertIn("需人工确认", item.op_message)
        self.assertTrue(item.suggested_price < item.current_price)
        self.assertEqual(_FakeImeiService.changed_prices, [])

    def test_task_auto_list_uses_pipeline_and_marks_item_listed(self):
        imported_store = BatchItemStore()
        imported_store.extend([
            BatchItem(
                product_id="p30",
                qc_code="qc30",
                title="iPhone 14",
                model="iPhone 14",
                condition="95新",
                capacity="128G",
                color="黑色",
                current_price=0,
                cost_price=2600,
                account_name="shop-a",
                status=ProductStatus.NOT_LISTED,
                import_source="manual",
                listing_eligible=True,
            )
        ])
        _FakeImeiService.details_by_qc["qc30"] = ProductDetail(
            product_id="p30",
            qc_code="qc30",
            title="iPhone 14",
            current_price=0,
            status=ProductStatus.NOT_LISTED,
            model="iPhone 14",
            condition="95新",
            capacity="128G",
            color="黑色",
            settle_price=0,
        )
        sold_cache = _StubSoldCache([
            SoldRecord("s30", "iPhone 14", 3450, datetime.now() - timedelta(days=1), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s31", "iPhone 14", 3380, datetime.now() - timedelta(days=2), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s32", "iPhone 14", 3420, datetime.now() - timedelta(days=3), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
        ])

        with self._patch_task_runtime():
            result = task_auto_list(self.account_store, None, sold_cache, None, imported_store)

        item = imported_store.get("p30")
        self.assertEqual(result["ok"], 1)
        self.assertEqual(result["fail"], 0)
        self.assertTrue(item.reprice_ok)
        self.assertEqual(item.op_status, "已上架")
        self.assertEqual(item.status, ProductStatus.ON_SALE)
        self.assertIsNotNone(item.suggested_price)
        self.assertEqual(_FakeImeiService.listed_prices[0][0], "p30")
        self.assertEqual(len(self.history_db.records), 1)


class PricingEngineTurnoverOptimizationTest(unittest.TestCase):
    def test_turnover_optimization_prefers_higher_price_when_speed_is_similar(self):
        from zhuanzhuan_pricing.core.pricing_engine import PricingEngine

        now = datetime.now()
        records = [
            SoldRecord("a1", "Model A", 3000, now - timedelta(days=1), hours_to_sell=18, model="Model A"),
            SoldRecord("a2", "Model A", 3000, now - timedelta(days=2), hours_to_sell=20, model="Model A"),
            SoldRecord("a3", "Model A", 3000, now - timedelta(days=3), hours_to_sell=19, model="Model A"),
            SoldRecord("a4", "Model A", 2950, now - timedelta(days=1), hours_to_sell=18, model="Model A"),
            SoldRecord("a5", "Model A", 2950, now - timedelta(days=2), hours_to_sell=21, model="Model A"),
            SoldRecord("a6", "Model A", 2900, now - timedelta(days=2), hours_to_sell=20, model="Model A"),
            SoldRecord("a7", "Model A", 2850, now - timedelta(days=1), hours_to_sell=19, model="Model A"),
            SoldRecord("a8", "Model A", 2800, now - timedelta(days=2), hours_to_sell=20, model="Model A"),
        ]

        result = PricingEngine().calculate(records, "Model A")

        self.assertIsNotNone(result.recommended_price)
        self.assertGreaterEqual(result.recommended_price, 2950)
        self.assertIn("周转优选", result.warning)

    def test_turnover_optimization_is_stable_when_hours_to_sell_missing(self):
        from zhuanzhuan_pricing.core.pricing_engine import PricingEngine

        now = datetime.now()
        records = [
            SoldRecord("b1", "Model B", 3100, now - timedelta(days=1), hours_to_sell=None, model="Model B"),
            SoldRecord("b2", "Model B", 3050, now - timedelta(days=2), hours_to_sell=None, model="Model B"),
            SoldRecord("b3", "Model B", 3000, now - timedelta(days=3), hours_to_sell=None, model="Model B"),
            SoldRecord("b4", "Model B", 2950, now - timedelta(days=4), hours_to_sell=None, model="Model B"),
        ]

        result = PricingEngine().calculate(records, "Model B")

        self.assertIsNotNone(result.recommended_price)
        self.assertTrue(2950 <= result.recommended_price <= 3100)
        self.assertIn("周转优选", result.warning)


class Tail8NormalizationTest(unittest.TestCase):
    def test_normalize_tail8_price_never_increases_raw_candidate(self):
        from zhuanzhuan_pricing.services.reprice_service import normalize_tail8_price

        self.assertEqual(normalize_tail8_price(2400.9), 2398.0)
        self.assertEqual(normalize_tail8_price(2398.1), 2398.0)
        self.assertEqual(normalize_tail8_price(2398.0), 2398.0)


if __name__ == "__main__":
    unittest.main()
