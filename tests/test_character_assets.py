from __future__ import annotations

import sqlite3
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


from factory import character_assets  # noqa: E402


def _database() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    character_assets.ensure_schema(database)
    return database


class CharacterAssetResolutionTests(unittest.TestCase):
    def test_api_exact_unique_active_asset_is_bound(self):
        database = _database()
        api = Mock()
        api.find_real_person_assets.return_value = [
            {
                "Id": "asset-202608120001-example",
                "Name": "小玲",
                "GroupId": "group-202608120001-example",
                "Status": "Active",
            }
        ]

        result = character_assets.resolve_character_asset(
            database,
            character_id="sales_xiaoling",
            display_name="小玲",
            api_client=api,
        )

        self.assertEqual(result.status, "resolved")
        self.assertEqual(
            result.asset_uri, "asset://asset-202608120001-example"
        )
        self.assertEqual(result.source, "api")
        saved = character_assets.get_binding(database, "sales_xiaoling")
        self.assertEqual(saved["asset_id"], "asset-202608120001-example")

    def test_api_unavailable_requests_asset_id_without_browser_fallback(self):
        database = _database()
        api = Mock()
        api.find_real_person_assets.side_effect = (
            character_assets.AssetApiUnavailable("advanced entitlement missing")
        )

        result = character_assets.resolve_character_asset(
            database,
            character_id="sales_xiaoling",
            display_name="小玲",
            api_client=api,
        )

        self.assertEqual(result.status, "asset_id_required")
        self.assertIn("assetId", result.user_prompt)
        self.assertEqual(result.reason, "api_unavailable")
        self.assertNotIn("browser", result.user_prompt.lower())
        self.assertNotIn("浏览器", result.user_prompt)

    def test_ambiguous_api_results_do_not_guess(self):
        database = _database()
        api = Mock()
        api.find_real_person_assets.return_value = [
            {"Id": "asset-first", "Name": "小玲", "Status": "Active"},
            {"Id": "asset-second", "Name": "小玲", "Status": "Active"},
        ]

        result = character_assets.resolve_character_asset(
            database,
            character_id="sales_xiaoling",
            display_name="小玲",
            api_client=api,
        )

        self.assertEqual(result.status, "asset_id_required")
        self.assertEqual(result.reason, "ambiguous_api_match")
        self.assertIsNone(character_assets.get_binding(database, "sales_xiaoling"))

    def test_manual_asset_id_is_normalized_and_reused(self):
        database = _database()

        first = character_assets.bind_manual_asset(
            database,
            character_id="sales_xiaoling",
            display_name="小玲",
            asset_id="asset://asset-202608120001-example",
        )
        second = character_assets.resolve_character_asset(
            database,
            character_id="sales_xiaoling",
            display_name="小玲",
            api_client=None,
        )

        self.assertEqual(first.asset_id, "asset-202608120001-example")
        self.assertEqual(second.asset_uri, first.asset_uri)
        self.assertEqual(second.source, "manual")

    def test_invalid_manual_asset_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "assetId"):
            character_assets.bind_manual_asset(
                _database(),
                character_id="sales_xiaoling",
                display_name="小玲",
                asset_id="not-an-asset",
            )

    def test_cached_binding_is_injected_into_runtime_character(self):
        database = _database()
        character_assets.bind_manual_asset(
            database,
            character_id="sales_xiaoling",
            display_name="小玲",
            asset_id="asset-202608120001-example",
        )
        characters = [{"id": "sales_xiaoling", "name": "小玲"}]

        changed = character_assets.apply_cached_bindings(database, characters)

        self.assertTrue(changed)
        self.assertEqual(
            characters[0]["seedance_asset_uri"],
            "asset://asset-202608120001-example",
        )

    def test_inactive_cached_binding_is_not_reused_without_api_validation(self):
        database = _database()
        character_assets.bind_manual_asset(
            database,
            character_id="sales_xiaoling",
            display_name="灏忕幉",
            asset_id="asset-202608120001-example",
        )
        database.execute(
            "UPDATE character_asset_bindings SET status='Inactive' "
            "WHERE character_id='sales_xiaoling'"
        )
        database.commit()

        result = character_assets.resolve_character_asset(
            database,
            character_id="sales_xiaoling",
            display_name="灏忕幉",
            api_client=None,
        )

        self.assertEqual(result.status, "asset_id_required")
        self.assertIsNone(result.asset_uri)
        self.assertEqual(result.reason, "api_unavailable")


if __name__ == "__main__":
    unittest.main()
