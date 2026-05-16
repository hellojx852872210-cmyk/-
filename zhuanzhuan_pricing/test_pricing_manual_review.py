import argparse
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from zhuanzhuan_pricing.automation.tasks import (
    STATUS_NOTIFY_DEDUPE_KEY,
    _match_detail_for_erp_item,
    apply_manual_review_decision,
    build_next_run_module_preview,
    build_reprice_decision,
    task_auto_list,
    task_auto_reprice,
    task_erp_sync,
    task_probe_perturbation,
    task_refresh_imported_items_status,
    task_sales_report,
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
from zhuanzhuan_pricing.services.erp_service import ErpItem
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

    def query_official_reference_price(self, **kwargs):
        return None


class ImeiServiceBatchLookupTest(unittest.TestCase):

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

    def test_query_by_imei_prefers_largest_qc_code(self):
        svc = ImeiService("shop-a", "cookie-a")

        def fake_list(query):
            if query.get("imeis") == ["354324419674599"]:
                return {
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "productId": "old-product",
                                "qcCode": "996494951",
                                "imei": "354324419674599",
                                "status": "0",
                                "title": "old",
                                "price": 4208,
                            },
                            {
                                "productId": "new-product",
                                "qcCode": "997695854",
                                "imei": "354324419674599",
                                "status": "0",
                                "title": "new",
                                "price": 4308,
                            },
                        ]
                    },
                }
            return {"code": 0, "data": {"list": []}}

        svc._merchant_product_list = fake_list

        detail = svc.query_by_imei("354324419674599")

        self.assertIsNotNone(detail)
        self.assertEqual(detail.qc_code, "997695854")
        self.assertEqual(detail.product_id, "new-product")

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

    def test_check_cookie_valid_returns_true_when_merchant_code_is_success(self):
        svc = ImeiService("shop-a", "cookie-a")

        class _Resp:
            status_code = 200

        with patch("zhuanzhuan_pricing.services.zhuanzhuan_api._post_json_with_retry", return_value=(_Resp(), {"code": 0, "data": {"list": []}})):
            ok, msg = svc.check_cookie_valid()

        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_check_cookie_valid_returns_false_when_merchant_code_is_error(self):
        svc = ImeiService("shop-a", "cookie-a")

        class _Resp:
            status_code = 200

        with patch("zhuanzhuan_pricing.services.zhuanzhuan_api._post_json_with_retry", return_value=(_Resp(), {"code": 10000, "msg": "未登录"})):
            ok, msg = svc.check_cookie_valid()

        self.assertFalse(ok)
        self.assertIn("商家接口返回错误", msg)

    def test_check_cookie_valid_returns_false_when_body_missing_code(self):
        svc = ImeiService("shop-a", "cookie-a")

        class _Resp:
            status_code = 200

        with patch("zhuanzhuan_pricing.services.zhuanzhuan_api._post_json_with_retry", return_value=(_Resp(), {})):
            ok, msg = svc.check_cookie_valid()

        self.assertFalse(ok)
        self.assertIn("缺少状态码", msg)

    def test_check_cookie_valid_returns_false_for_http_401(self):
        svc = ImeiService("shop-a", "cookie-a")

        class _Resp:
            status_code = 401

        with patch("zhuanzhuan_pricing.services.zhuanzhuan_api._post_json_with_retry", return_value=(_Resp(), {"msg": "登录已失效"})):
            ok, msg = svc.check_cookie_valid()

        self.assertFalse(ok)
        self.assertIn("登录已失效", msg)

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


class TaskErpSyncFlowTest(unittest.TestCase):

    class _FakeErpFetcher:
        on_sale_items = []
        in_stock_items = []
        fail_on_sale = False
        fail_in_stock = False

        def __init__(self, _config):
            pass

        @classmethod
        def reset(cls):
            cls.on_sale_items = []
            cls.in_stock_items = []
            cls.fail_on_sale = False
            cls.fail_in_stock = False

        def check_and_refresh_token(self):
            return True

        def fetch_on_sale_items(self):
            if self.fail_on_sale:
                raise RuntimeError("on-sale boom")
            return list(self.on_sale_items)

        def fetch_in_stock_items(self):
            if self.fail_in_stock:
                raise RuntimeError("in-stock boom")
            return list(self.in_stock_items)

        def build_cost_map(self, items):
            return {
                str(item.product_id): float(item.cost_price)
                for item in items
                if getattr(item, "product_id", "") and float(getattr(item, "cost_price", 0.0) or 0.0) > 0
            }

    class _FakeImeiServiceForSync:
        detail_by_code = {}
        login_result_by_account = {}

        def __init__(self, account_name, cookie):
            self.account_name = account_name
            self.cookie = cookie

        @classmethod
        def reset(cls):
            cls.detail_by_code = {}
            cls.login_result_by_account = {}

        def check_cookie_valid(self):
            return self.login_result_by_account.get(self.account_name, (True, ""))

        def fetch_by_codes(self, codes, batch_size=30):
            details = []
            missing = []
            for code in codes:
                detail = self.detail_by_code.get(code)
                if detail is None:
                    missing.append(code)
                else:
                    details.append(detail)
            return details, missing

        def query_by_qc_code(self, code):
            return self.detail_by_code.get(code)

        def query_by_imei(self, imei):
            return self.detail_by_code.get(imei)

    class _FakeDataFetcherForSync:
        records_by_account = {}
        raise_by_account = {}

        def __init__(self, account_name, cookie):
            self.account_name = account_name
            self.cookie = cookie

        @classmethod
        def reset(cls):
            cls.records_by_account = {}
            cls.raise_by_account = {}

        def fetch_all_sold(self, since=None):
            if self.raise_by_account.get(self.account_name):
                raise RuntimeError(self.raise_by_account[self.account_name])
            return list(self.records_by_account.get(self.account_name, []))

    class _StubSoldCacheStore:
        def __init__(self):
            self.calls = []

        def upsert(self, records):
            records = list(records)
            self.calls.append(records)
            return len(records), 0

    def setUp(self):
        self._FakeErpFetcher.reset()
        self._FakeImeiServiceForSync.reset()
        self._FakeDataFetcherForSync.reset()
        self.account_store = _StubAccountStore([Account(name="shop-a", cookie="cookie-a")])

    def _patch_sync_runtime(self):
        return patch.multiple(
            "zhuanzhuan_pricing.automation.tasks",
            ErpFetcher=self._FakeErpFetcher,
            ImeiService=self._FakeImeiServiceForSync,
            DataFetcher=self._FakeDataFetcherForSync,
        )

    def test_task_erp_sync_emits_done_progress_event(self):
        erp_item = ErpItem(
            product_id="p-sync-1",
            qc_code="qc-sync-1",
            imei="",
            title="sync item",
            cost_price=2888,
            status="0",
        )
        self._FakeErpFetcher.on_sale_items = [erp_item]
        self._FakeImeiServiceForSync.detail_by_code["qc-sync-1"] = ProductDetail(
            product_id="p-sync-1",
            qc_code="qc-sync-1",
            title="sync item",
            current_price=3200,
            status=ProductStatus.ON_SALE,
        )

        imported_store = BatchItemStore()
        events = []

        with self._patch_sync_runtime():
            result = task_erp_sync(
                erp_config=None,
                cost_map={},
                account_store=self.account_store,
                imported_store=imported_store,
                on_progress_event=events.append,
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["missed"], 0)
        self.assertTrue(events)
        self.assertEqual(events[-1].get("stage"), "done")
        self.assertIn("同步成本价", events[-1].get("message", ""))

    def test_task_erp_sync_keeps_partial_success_when_one_fetch_channel_fails(self):
        self._FakeErpFetcher.fail_on_sale = True
        in_stock_item = ErpItem(
            product_id="p-sync-2",
            qc_code="qc-sync-2",
            imei="",
            title="in-stock item",
            cost_price=2666,
            status="60",
        )
        self._FakeErpFetcher.in_stock_items = [in_stock_item]
        self._FakeImeiServiceForSync.detail_by_code["qc-sync-2"] = ProductDetail(
            product_id="p-sync-2",
            qc_code="qc-sync-2",
            title="in-stock item",
            current_price=0,
            status=ProductStatus.NOT_LISTED,
        )

        imported_store = BatchItemStore()
        with self._patch_sync_runtime():
            result = task_erp_sync(
                erp_config=None,
                cost_map={},
                account_store=self.account_store,
                imported_store=imported_store,
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["missed"], 0)
        self.assertIn("ERP 拉取异常", result["summary"])
        self.assertIn("ERP 在售拉取失败", result["error"])

    def test_match_detail_respects_fallback_budget_limit(self):
        erp_item = ErpItem(
            product_id="p-sync-3",
            qc_code="qc-sync-3",
            imei="",
            title="budget item",
            cost_price=0,
            status="0",
        )
        detail, reason = _match_detail_for_erp_item(
            self._FakeImeiServiceForSync("shop-a", "cookie-a"),
            erp_item,
            detail_by_code={},
            missing_codes=None,
            lookup_cache={},
            fallback_budget=[0],
        )

        self.assertIsNone(detail)
        self.assertIn("预算已耗尽", reason)
    def test_task_erp_sync_skips_cookie_failed_account_and_continues(self):
        account_store = _StubAccountStore([
            Account(name="shop-a", cookie="cookie-a"),
            Account(name="shop-b", cookie="cookie-b"),
        ])
        self._FakeImeiServiceForSync.login_result_by_account = {
            "shop-a": (False, "登录已失效"),
            "shop-b": (True, ""),
        }
        self._FakeErpFetcher.on_sale_items = [
            ErpItem(
                product_id="p-sync-4",
                qc_code="qc-sync-4",
                imei="",
                title="sync item b",
                cost_price=2999,
                status="0",
            )
        ]
        self._FakeImeiServiceForSync.detail_by_code["qc-sync-4"] = ProductDetail(
            product_id="p-sync-4",
            qc_code="qc-sync-4",
            title="sync item b",
            current_price=3500,
            status=ProductStatus.ON_SALE,
        )

        with self._patch_sync_runtime():
            result = task_erp_sync(
                erp_config=None,
                cost_map={},
                account_store=account_store,
                imported_store=BatchItemStore(),
            )

        self.assertEqual(result["matched"], 1)
        self.assertIn("Cookie 失败", result["summary"])
        self.assertIn("shop-a", result["error"])

    def test_task_erp_sync_continues_when_sales_sync_fails(self):
        self._FakeImeiServiceForSync.login_result_by_account = {"shop-a": (True, "")}
        self._FakeDataFetcherForSync.raise_by_account = {"shop-a": "sold fetch boom"}
        self._FakeErpFetcher.on_sale_items = [
            ErpItem(
                product_id="p-sync-5",
                qc_code="qc-sync-5",
                imei="",
                title="sync item sales fail",
                cost_price=2555,
                status="0",
            )
        ]
        self._FakeImeiServiceForSync.detail_by_code["qc-sync-5"] = ProductDetail(
            product_id="p-sync-5",
            qc_code="qc-sync-5",
            title="sync item sales fail",
            current_price=3300,
            status=ProductStatus.ON_SALE,
        )

        with self._patch_sync_runtime():
            result = task_erp_sync(
                erp_config=None,
                cost_map={},
                account_store=self.account_store,
                imported_store=BatchItemStore(),
                sold_cache=self._StubSoldCacheStore(),
                sold_sync_days=30,
            )

        self.assertEqual(result["matched"], 1)
        self.assertIn("成交同步异常", result["summary"])
        self.assertIn("sold fetch boom", result["error"])

    def test_task_erp_sync_syncs_sales_records_with_30_days_window(self):
        sold_cache = self._StubSoldCacheStore()
        now = datetime.now()
        self._FakeImeiServiceForSync.login_result_by_account = {"shop-a": (True, "")}
        self._FakeDataFetcherForSync.records_by_account = {
            "shop-a": [
                SoldRecord(
                    product_id="sold-1",
                    title="sold item",
                    sold_price=3200,
                    sold_time=now - timedelta(days=1),
                    model="M1",
                )
            ]
        }
        self._FakeErpFetcher.on_sale_items = [
            ErpItem(
                product_id="p-sync-6",
                qc_code="qc-sync-6",
                imei="",
                title="sync item sales ok",
                cost_price=2444,
                status="0",
            )
        ]
        self._FakeImeiServiceForSync.detail_by_code["qc-sync-6"] = ProductDetail(
            product_id="p-sync-6",
            qc_code="qc-sync-6",
            title="sync item sales ok",
            current_price=3200,
            status=ProductStatus.ON_SALE,
        )

        with self._patch_sync_runtime():
            result = task_erp_sync(
                erp_config=None,
                cost_map={},
                account_store=self.account_store,
                imported_store=BatchItemStore(),
                sold_cache=sold_cache,
                sold_sync_days=30,
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(len(sold_cache.calls), 1)
        self.assertEqual(len(sold_cache.calls[0]), 1)
        self.assertIn("成交同步账号 1 个", result["summary"])

    def test_task_erp_sync_uses_later_account_when_earlier_account_is_off_sale(self):
        imported_store = BatchItemStore()
        self._FakeErpFetcher.on_sale_items = [
            ErpItem(
                product_id="erp-new-p",
                qc_code="new-qc-final",
                imei="imei-migrate-1",
                title="new item",
                cost_price=2666,
                status="0",
            )
        ]

        def _detail(product_id, qc_code, status, account_name):
            return ProductDetail(
                product_id=product_id,
                qc_code=qc_code,
                title="new item",
                current_price=3500,
                status=status,
                imei="imei-migrate-1",
                account_name=account_name,
            )

        self._FakeImeiServiceForSync.detail_by_code["new-qc"] = _detail(
            "new-p-YY",
            "new-qc",
            ProductStatus.OFF_SALE,
            "YY",
        )
        self._FakeImeiServiceForSync.detail_by_code["new-qc-final"] = _detail(
            "new-p-WJK",
            "new-qc-final",
            ProductStatus.ON_SALE,
            "wjk",
        )

        def fake_fetch_by_codes(self, codes, batch_size=30):
            details = []
            for code in codes:
                if code == "imei-migrate-1":
                    details.extend([
                        self.detail_by_code["new-qc"],
                        self.detail_by_code["new-qc-final"],
                    ])
            return details, []

        with self._patch_sync_runtime(), patch.object(self._FakeImeiServiceForSync, "fetch_by_codes", fake_fetch_by_codes):
            result = task_erp_sync(
                erp_config=None,
                cost_map={},
                account_store=self.account_store,
                imported_store=imported_store,
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(imported_store.count(), 1)
        self.assertEqual(imported_store.get("new-p-WJK").qc_code, "new-qc-final")

    def test_task_erp_sync_does_not_migrate_when_old_item_is_on_sale(self):
        imported_store = BatchItemStore()
        imported_store.extend([
            BatchItem(
                product_id="old-p-on-sale",
                qc_code="old-qc-on-sale",
                title="old on sale",
                current_price=3000,
                cost_price=2500,
                account_name="shop-a",
                status=ProductStatus.ON_SALE,
                imei="imei-no-migrate-on-sale",
            )
        ])
        self._FakeErpFetcher.on_sale_items = [
            ErpItem(
                product_id="erp-new-p-on-sale",
                qc_code="new-qc-on-sale",
                imei="imei-no-migrate-on-sale",
                title="new item",
                cost_price=2666,
                status="0",
            )
        ]
        self._FakeImeiServiceForSync.detail_by_code["new-qc-on-sale"] = ProductDetail(
            product_id="new-p-on-sale",
            qc_code="new-qc-on-sale",
            title="new item",
            current_price=3500,
            status=ProductStatus.ON_SALE,
            imei="imei-no-migrate-on-sale",
        )

        with self._patch_sync_runtime():
            result = task_erp_sync(
                erp_config=None,
                cost_map={},
                account_store=self.account_store,
                imported_store=imported_store,
            )

        self.assertIsNotNone(imported_store.get("old-p-on-sale"))
        self.assertIsNotNone(imported_store.get("new-p-on-sale"))
        self.assertEqual(imported_store.count(), 2)


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
        self.assertEqual(pipeline["suggested_settled_price"], pipeline["decision"]["final_settled_price"])

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

    def test_pipeline_without_samples_uses_official_reference_as_fallback_and_marks_manual_review(self):
        item = BatchItem(
            product_id="p3-official",
            qc_code="qc3-official",
            title="Unknown Phone",
            model="Unknown Phone",
            condition="95新",
            current_price=2000,
            cost_price=1200,
        )

        pipeline = run_reprice_pipeline(
            item,
            _StubSoldCache([]),
            apply_rules=False,
            official_reference_fetcher=lambda _item: {
                "reference_price": 2100,
                "reference_settle_price": 1700,
                "grade_name": "95·A",
                "sku_id": "sku-official-1",
            },
        )

        decision = pipeline["decision"]
        self.assertEqual(pipeline["suggested_price"], 2098)
        self.assertTrue(decision["official_reference_used_as_anchor"])
        self.assertEqual(decision["official_reference_price"], 2100)
        self.assertTrue(decision["needs_manual_review"])
        self.assertIn("样本不足", decision["manual_review_reason"])

    def test_pipeline_marks_manual_review_when_official_deviation_exceeds_threshold(self):
        item = BatchItem(
            product_id="p-official-dev",
            qc_code="qc-official-dev",
            title="iPhone 14",
            model="iPhone 14",
            condition="95新",
            capacity="128G",
            color="黑色",
            current_price=3200,
            cost_price=1200,
            listed_time=datetime.now() - timedelta(days=5),
        )
        records = [
            SoldRecord("s-off-1", "iPhone 14", 3450, datetime.now() - timedelta(days=1), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s-off-2", "iPhone 14", 3380, datetime.now() - timedelta(days=2), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s-off-3", "iPhone 14", 3420, datetime.now() - timedelta(days=3), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s-off-4", "iPhone 14", 3440, datetime.now() - timedelta(days=4), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s-off-5", "iPhone 14", 3410, datetime.now() - timedelta(days=5), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
        ]

        pipeline = run_reprice_pipeline(
            item,
            _StubSoldCache(records),
            apply_rules=False,
            official_reference_fetcher=lambda _item: {"reference_price": 3000, "grade_name": "95·A", "sku_id": "sku-official-2"},
        )

        decision = pipeline["decision"]
        self.assertTrue(decision["official_risk_triggered"])
        self.assertTrue(decision["needs_manual_review"])
        self.assertIsNotNone(decision["official_deviation_pct"])
        self.assertIn("偏离", decision["manual_review_reason"])

    def test_pipeline_does_not_trigger_official_manual_review_when_deviation_small(self):
        item = BatchItem(
            product_id="p-official-safe",
            qc_code="qc-official-safe",
            title="iPhone 14",
            model="iPhone 14",
            condition="95新",
            capacity="128G",
            color="黑色",
            current_price=3200,
            cost_price=1200,
            listed_time=datetime.now() - timedelta(days=5),
        )
        records = [
            SoldRecord("s-off-safe-1", "iPhone 14", 3450, datetime.now() - timedelta(days=1), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s-off-safe-2", "iPhone 14", 3380, datetime.now() - timedelta(days=2), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s-off-safe-3", "iPhone 14", 3420, datetime.now() - timedelta(days=3), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s-off-safe-4", "iPhone 14", 3440, datetime.now() - timedelta(days=4), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s-off-safe-5", "iPhone 14", 3410, datetime.now() - timedelta(days=5), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
        ]

        pipeline = run_reprice_pipeline(
            item,
            _StubSoldCache(records),
            apply_rules=False,
            official_reference_fetcher=lambda _item: {"reference_price": 3400, "grade_name": "95·A", "sku_id": "sku-official-3"},
        )

        decision = pipeline["decision"]
        self.assertFalse(decision["official_risk_triggered"])
        self.assertFalse(decision["needs_manual_review"])

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
        self.assertIn("预计结算价", preview)
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
        self.assertEqual(item.manual_review_state, "pending")
        self.assertEqual(item.manual_review_source, "stale_drop")
        self.assertEqual(item.manual_review_target_action, "change_price")
        self.assertIsNotNone(item.manual_review_target_price)

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

    def test_apply_manual_review_decision_accept_change_price(self):
        imported_store = BatchItemStore()
        imported_store.extend([
            BatchItem(
                product_id="p40",
                qc_code="qc40",
                title="manual reprice",
                current_price=3200,
                cost_price=3000,
                account_name="shop-a",
                status=ProductStatus.ON_SALE,
                suggested_price=3098,
                new_price=3098,
                manual_review_state="pending",
                manual_review_source="auto_reprice",
                manual_review_target_action="change_price",
                manual_review_target_price=3098,
                op_status="待确认",
            )
        ])
        _FakeImeiService.details_by_qc["qc40"] = ProductDetail(
            product_id="p40",
            qc_code="qc40",
            title="manual reprice",
            current_price=3200,
            status=ProductStatus.ON_SALE,
            settle_price=3100,
        )

        with self._patch_task_runtime():
            ok, message = apply_manual_review_decision(self.account_store, imported_store, "p40", "accept")

        item = imported_store.get("p40")
        self.assertTrue(ok)
        self.assertIn("成功", message)
        self.assertEqual(item.op_status, "已改价")
        self.assertEqual(item.manual_review_state, "accepted")
        self.assertEqual(_FakeImeiService.changed_prices[0][0], "p40")

    def test_apply_manual_review_decision_accept_with_custom_price_override(self):
        imported_store = BatchItemStore()
        imported_store.extend([
            BatchItem(
                product_id="p40-override",
                qc_code="qc40-override",
                title="manual reprice override",
                current_price=3200,
                cost_price=3000,
                account_name="shop-a",
                status=ProductStatus.ON_SALE,
                suggested_price=3098,
                new_price=3098,
                manual_review_state="pending",
                manual_review_source="auto_reprice",
                manual_review_target_action="change_price",
                manual_review_target_price=3098,
                op_status="待确认",
            )
        ])
        _FakeImeiService.details_by_qc["qc40-override"] = ProductDetail(
            product_id="p40-override",
            qc_code="qc40-override",
            title="manual reprice override",
            current_price=3200,
            status=ProductStatus.ON_SALE,
            settle_price=3100,
        )

        with self._patch_task_runtime():
            ok, message = apply_manual_review_decision(
                self.account_store,
                imported_store,
                "p40-override",
                "accept",
                target_price_override=3150,
            )

        item = imported_store.get("p40-override")
        self.assertTrue(ok)
        self.assertIn("成功", message)
        self.assertEqual(item.op_status, "已改价")
        self.assertEqual(item.manual_review_state, "accepted")
        self.assertEqual(item.suggested_price, 3150.0)
        self.assertEqual(_FakeImeiService.changed_prices[0][1], 3150.0)

    def test_apply_manual_review_decision_reject_sets_permanent_state(self):
        imported_store = BatchItemStore()
        imported_store.extend([
            BatchItem(
                product_id="p41",
                qc_code="qc41",
                title="manual reject",
                current_price=3200,
                cost_price=3000,
                account_name="shop-a",
                status=ProductStatus.ON_SALE,
                suggested_price=3098,
                manual_review_state="pending",
                manual_review_source="stale_drop",
                manual_review_target_action="change_price",
                manual_review_target_price=3098,
                op_status="待确认",
            )
        ])

        with self._patch_task_runtime():
            ok, message = apply_manual_review_decision(self.account_store, imported_store, "p41", "reject")
            rerun = task_auto_reprice(self.account_store, _StubSoldCache([]), {}, None, imported_store)

        item = imported_store.get("p41")
        self.assertTrue(ok)
        self.assertIn("永久拒绝", message)
        self.assertEqual(item.manual_review_state, "rejected")
        self.assertFalse(item.ignored)
        self.assertEqual(rerun["total"], 0)

    def test_apply_manual_review_decision_reject_and_ignore(self):
        imported_store = BatchItemStore()
        imported_store.extend([
            BatchItem(
                product_id="p43",
                qc_code="qc43",
                title="manual reject ignore",
                current_price=3200,
                cost_price=3000,
                account_name="shop-a",
                status=ProductStatus.ON_SALE,
                suggested_price=3098,
                manual_review_state="pending",
                manual_review_source="stale_drop",
                manual_review_target_action="change_price",
                manual_review_target_price=3098,
                op_status="待确认",
            )
        ])

        with self._patch_task_runtime():
            ok, message = apply_manual_review_decision(self.account_store, imported_store, "p43", "reject_and_ignore")

        item = imported_store.get("p43")
        self.assertTrue(ok)
        self.assertIn("移入不处理区", message)
        self.assertEqual(item.manual_review_state, "rejected_ignored")
        self.assertTrue(item.ignored)
    def test_task_auto_reprice_sets_pending_when_official_guard_triggers(self):
        imported_store = BatchItemStore()
        imported_store.extend([
            BatchItem(
                product_id="p10-official",
                qc_code="qc10-official",
                title="iPhone 14",
                model="iPhone 14",
                condition="95新",
                capacity="128G",
                color="黑色",
                current_price=3200,
                cost_price=1200,
                account_name="shop-a",
                status=ProductStatus.ON_SALE,
                listed_time=datetime.now() - timedelta(days=5),
            )
        ])
        _FakeImeiService.details_by_qc["qc10-official"] = ProductDetail(
            product_id="p10-official",
            qc_code="qc10-official",
            title="iPhone 14",
            current_price=3200,
            status=ProductStatus.ON_SALE,
            model="iPhone 14",
            condition="95新",
            capacity="128G",
            color="黑色",
            settle_price=3100,
            category_id=101,
            brand_id=10530,
            model_id=2188,
            product_params="6651:511434;",
        )
        sold_cache = _StubSoldCache([
            SoldRecord("s10o1", "iPhone 14", 3450, datetime.now() - timedelta(days=1), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s10o2", "iPhone 14", 3380, datetime.now() - timedelta(days=2), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s10o3", "iPhone 14", 3420, datetime.now() - timedelta(days=3), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s10o4", "iPhone 14", 3440, datetime.now() - timedelta(days=4), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
            SoldRecord("s10o5", "iPhone 14", 3410, datetime.now() - timedelta(days=5), model="iPhone 14", condition="95新", capacity="128G", color="黑色"),
        ])

        with self._patch_task_runtime(), patch.object(
            _FakeImeiService,
            "query_official_reference_price",
            return_value={"reference_price": 3000, "grade_name": "95·A", "sku_id": "sku-task-official"},
        ):
            result = task_auto_reprice(self.account_store, sold_cache, {}, None, imported_store)

        item = imported_store.get("p10-official")
        self.assertEqual(result["ok"], 0)
        self.assertEqual(result["skip"], 1)
        self.assertEqual(item.op_status, "待确认")
        self.assertIn("偏离", item.reprice_msg)
        self.assertEqual(item.manual_review_state, "pending")
        self.assertEqual(item.manual_review_source, "auto_reprice")
        self.assertEqual(_FakeImeiService.changed_prices, [])


class _StubNotifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.messages = []

    def send_text(self, content):
        self.messages.append(content)
        return self.ok


class _StubWxAppClient:
    def __init__(self, ok=True):
        self.ok = ok
        self.messages = []

    def broadcast_text(self, content):
        self.messages.append(content)
        return self.ok


class ProbePerturbationTaskTest(unittest.TestCase):
    def setUp(self):
        _FakeImeiService.reset()
        self.account_store = _StubAccountStore([Account(name="shop-a", cookie="cookie-a")])

    def _build_store_item(self, **kwargs):
        imported_store = BatchItemStore()
        payload = {
            "product_id": "p-probe-1",
            "qc_code": "qc-probe-1",
            "title": "probe item",
            "current_price": 3000,
            "cost_price": 2000,
            "account_name": "shop-a",
            "status": ProductStatus.ON_SALE,
            "listed_time": datetime.now() - timedelta(days=8),
            "op_status": "待处理",
        }
        payload.update(kwargs)
        imported_store.extend([BatchItem(**payload)])
        return imported_store

    def test_probe_task_yields_to_main_task_when_item_already_changed(self):
        imported_store = self._build_store_item(op_status="已改价")
        _FakeImeiService.details_by_qc["qc-probe-1"] = ProductDetail(
            product_id="p-probe-1",
            qc_code="qc-probe-1",
            title="probe item",
            current_price=3000,
            status=ProductStatus.ON_SALE,
        )

        with patch("zhuanzhuan_pricing.automation.tasks.ImeiService", _FakeImeiService), \
             patch.dict("zhuanzhuan_pricing.automation.tasks.cfg._data", {"probe_enabled": True}, clear=False):
            result = task_probe_perturbation(self.account_store, _StubSoldCache([]), None, imported_store)

        item = imported_store.get("p-probe-1")
        self.assertEqual(result["ok"], 0)
        self.assertEqual(result["skip"], 1)
        self.assertEqual(item.op_status, "跳过")
        self.assertIn("让路主任务", item.op_message)
        self.assertEqual(_FakeImeiService.changed_prices, [])

    def test_probe_task_respects_interval_and_daily_cap(self):
        now = datetime.now()
        imported_store = self._build_store_item(
            probe_anchor_price=3000,
            probe_last_side="up",
            probe_last_at=now - timedelta(minutes=30),
            probe_day=now.strftime("%Y-%m-%d"),
            probe_day_count=6,
        )
        _FakeImeiService.details_by_qc["qc-probe-1"] = ProductDetail(
            product_id="p-probe-1",
            qc_code="qc-probe-1",
            title="probe item",
            current_price=3000,
            status=ProductStatus.ON_SALE,
        )

        with patch("zhuanzhuan_pricing.automation.tasks.ImeiService", _FakeImeiService), \
             patch.dict("zhuanzhuan_pricing.automation.tasks.cfg._data", {
                 "probe_enabled": True,
                 "probe_interval_minutes": 240,
                 "probe_daily_max": 6,
             }, clear=False):
            result = task_probe_perturbation(self.account_store, _StubSoldCache([]), None, imported_store)

        item = imported_store.get("p-probe-1")
        self.assertEqual(result["ok"], 0)
        self.assertEqual(result["skip"], 1)
        self.assertIn("未到探针周期", item.reprice_msg)
        self.assertEqual(_FakeImeiService.changed_prices, [])


class AgentRunnerManualReviewControlTest(unittest.TestCase):
    def test_resolve_pending_manual_review_applies_source_filter(self):
        from zhuanzhuan_pricing.automation.agent_runner import _resolve_pending_manual_review

        class _Runtime:
            def __init__(self):
                self.account_store = object()
                self.imported_store = BatchItemStore()

        runtime = _Runtime()
        runtime.imported_store.extend([
            BatchItem(
                product_id="p-filter-probe",
                qc_code="qc-filter-probe",
                title="probe pending",
                account_name="shop-a",
                manual_review_state="pending",
                manual_review_source="probe_perturbation",
                op_status="待确认",
            ),
            BatchItem(
                product_id="p-filter-auto",
                qc_code="qc-filter-auto",
                title="auto pending",
                account_name="shop-a",
                manual_review_state="pending",
                manual_review_source="auto_reprice",
                op_status="待确认",
            ),
        ])

        captured = {}

        def fake_batch_decision(account_store, imported_store, product_ids, action, on_progress=None):
            captured["ids"] = list(product_ids)
            captured["action"] = action
            return {"ok": len(product_ids), "fail": 0}

        with patch("zhuanzhuan_pricing.automation.agent_runner.apply_manual_review_batch_decision", side_effect=fake_batch_decision):
            code = _resolve_pending_manual_review(
                "reject_and_ignore",
                runtime=runtime,
                source_filter={"probe_perturbation"},
            )

        self.assertEqual(code, 0)
        self.assertEqual(captured["ids"], ["p-filter-probe"])
        self.assertEqual(captured["action"], "reject_and_ignore")

    def test_main_forwards_post_manual_review_off_and_source_filter(self):
        from zhuanzhuan_pricing.automation import agent_runner

        args = argparse.Namespace(
            command="run",
            tasks="erp_sync,probe_perturbation",
            interval_seconds=321,
            once=True,
            max_cycles=2,
            post_manual_review_mode="off",
            post_manual_review_source_filter="probe_perturbation",
        )

        with patch("zhuanzhuan_pricing.automation.agent_runner._parse_args", return_value=args), \
             patch("zhuanzhuan_pricing.automation.agent_runner.run_loop", return_value=0) as run_loop_mock:
            code = agent_runner.main()

        self.assertEqual(code, 0)
        self.assertTrue(run_loop_mock.called)
        _, kwargs = run_loop_mock.call_args
        self.assertEqual(kwargs["post_manual_review_mode"], "off")
        self.assertEqual(kwargs["post_manual_review_source_filter"], "probe_perturbation")


class SalesStatusReportTest(unittest.TestCase):
    def setUp(self):
        _FakeImeiService.reset()
        self.account_store = _StubAccountStore([Account(name="shop-a", cookie="cookie-a")])

    def _build_imported_store(self):
        imported_store = BatchItemStore()
        imported_store.extend([
            BatchItem(
                product_id="p-status-1",
                qc_code="qc-status-1",
                title="status item",
                current_price=3000,
                account_name="shop-a",
                status=ProductStatus.IN_QC,
                import_source="erp",
            )
        ])
        return imported_store

    def _seed_latest_detail(self, *, status=ProductStatus.NOT_LISTED, current_price=3000):
        _FakeImeiService.details_by_qc["qc-status-1"] = ProductDetail(
            product_id="p-status-1",
            qc_code="qc-status-1",
            title="status item",
            current_price=current_price,
            status=status,
            settle_price=current_price - 100,
        )

    def test_refresh_imported_status_returns_structured_status_change_records(self):
        imported_store = self._build_imported_store()
        self._seed_latest_detail(status=ProductStatus.NOT_LISTED)

        with patch("zhuanzhuan_pricing.automation.tasks.ImeiService", _FakeImeiService):
            result = task_refresh_imported_items_status(self.account_store, imported_store)

        self.assertEqual(result["status_changed"], 1)
        self.assertEqual(len(result["status_change_records"]), 1)
        record = result["status_change_records"][0]
        self.assertEqual(record["product_id"], "p-status-1")
        self.assertEqual(record["old_status"], "质检中")
        self.assertEqual(record["new_status"], "未上架")

    def test_sales_report_notifies_only_imported_status_changes_and_dedupes(self):
        imported_store = self._build_imported_store()
        self._seed_latest_detail(status=ProductStatus.NOT_LISTED)
        notifier = _StubNotifier(ok=True)

        with patch("zhuanzhuan_pricing.automation.tasks.ImeiService", _FakeImeiService), \
             patch("zhuanzhuan_pricing.automation.tasks.cfg.save", return_value=None), \
             patch.dict("zhuanzhuan_pricing.automation.tasks.cfg._data", {}, clear=True):
            first = task_sales_report(
                self.account_store,
                notifier,
                imported_store=imported_store,
            )
            imported_store.update_item("p-status-1", status=ProductStatus.IN_QC)
            second = task_sales_report(
                self.account_store,
                notifier,
                imported_store=imported_store,
            )

        self.assertEqual(len(notifier.messages), 1)
        sent = notifier.messages[0]
        self.assertIn("质检中→未上架", sent)
        self.assertNotIn("当前价", sent)
        self.assertIn("质检中→未上架", first)
        self.assertIn("均已播报", second)


    def test_build_next_run_module_preview_covers_three_modules(self):
        item = BatchItem(
            product_id="p-preview-1",
            qc_code="qc-preview-1",
            title="preview",
            current_price=3200,
            cost_price=2600,
            account_name="shop-a",
            status=ProductStatus.NOT_LISTED,
            import_source="erp",
            listing_eligible=True,
        )
        decision = {
            "system_price": 3098,
            "needs_manual_review": False,
            "manual_review_reason": "",
        }

        preview = build_next_run_module_preview(
            item,
            decision,
            suggested_price=3098,
        )

        self.assertIn("跳过：当前状态 未上架", preview["stale_drop"])
        self.assertIn("预计按 ¥3,098 自动上架", preview["auto_list"])
        self.assertIn("预计改价到 ¥3,098", preview["auto_reprice"])


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
