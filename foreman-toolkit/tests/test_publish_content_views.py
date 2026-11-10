import importlib.util
import io
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "publish_content_views.py"
SPEC = importlib.util.spec_from_file_location("publish_content_views", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
import sys
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        # Agents9 and EPEL9 are shared by RHEL9 and Rocky9.
        self.views = [
            module.ContentView(1, "AgentsRH9", False),
            module.ContentView(2, "EPEL_RHEL9", False),
            module.ContentView(3, "BaseRHEL9", False),
            module.ContentView(4, "BaseRocky9", False),
            module.ContentView(10, "RHEL9", True),
            module.ContentView(11, "Rocky9", True),
        ]
        self.components = {
            10: [
                module.Component(100, 1, True),
                module.Component(101, 2, True),
                module.Component(102, 3, True),
            ],
            11: [
                module.Component(110, 1, True),
                module.Component(111, 2, True),
                module.Component(112, 4, False),
            ],
        }
        self.catalog = module.Catalog(self.views, self.components)

    def names(self, selected):
        return {self.catalog.by_id[item].name for item in selected}

    def test_simple_root_adds_all_dependent_composites_and_their_dependencies(self):
        selected = self.catalog.select(["AgentsRH9"])
        self.assertEqual(
            self.names(selected),
            {"AgentsRH9", "EPEL_RHEL9", "BaseRHEL9", "BaseRocky9", "RHEL9", "Rocky9"},
        )

    def test_composite_root_does_not_expand_to_sibling_composite(self):
        selected = self.catalog.select(["RHEL9"])
        self.assertEqual(
            self.names(selected), {"AgentsRH9", "EPEL_RHEL9", "BaseRHEL9", "RHEL9"}
        )

    def test_order_has_every_simple_before_composites(self):
        selected = self.catalog.select(["AgentsRH9"])
        ordered = self.catalog.ordered(selected)
        composite_indexes = [index for index, view in enumerate(ordered) if view.composite]
        simple_indexes = [index for index, view in enumerate(ordered) if not view.composite]
        self.assertLess(max(simple_indexes), min(composite_indexes))

    def test_unknown_name_fails_before_changes(self):
        with self.assertRaises(module.PublishError):
            self.catalog.select(["DoesNotExist"])


class HelpersTests(unittest.TestCase):
    def test_missing_field_error_is_in_english(self):
        with self.assertRaisesRegex(
            module.PublishError, "Field 'Name' is missing from the Hammer response"
        ):
            module._field({"Id": 1}, "Name")

    def test_latest_version_uses_numeric_order(self):
        versions = [module.Version(1, "9.0"), module.Version(2, "10.0"), module.Version(3, "2.8")]
        self.assertEqual(module.latest_version(versions), module.Version(2, "10.0"))

    def test_requested_names_support_commas_and_deduplicate(self):
        self.assertEqual(
            module.parse_requested(["RHEL9, Rocky9", "RHEL9"], None), ["RHEL9", "Rocky9"]
        )

    def test_hammer_uses_numeric_pagination_until_the_last_page(self):
        hammer = module.Hammer("hammer", "ACME", page_size=2)
        hammer.json = Mock(
            side_effect=[
                [{"Id": 1}, {"Id": 2}],
                [{"Id": 3}],
            ]
        )

        self.assertEqual(
            hammer.paginated_json("content-view", "list"),
            [{"Id": 1}, {"Id": 2}, {"Id": 3}],
        )
        self.assertEqual(
            hammer.json.call_args_list[0].args,
            ("content-view", "list", "--page", "1", "--per-page", "2"),
        )
        self.assertEqual(
            hammer.json.call_args_list[1].args,
            ("content-view", "list", "--page", "2", "--per-page", "2"),
        )

    def test_component_listing_does_not_pass_pagination_options(self):
        hammer = module.Hammer("hammer", "ACME", page_size=100)
        hammer.json = Mock(return_value=[])

        self.assertEqual(hammer.list_components(52), [])
        self.assertEqual(
            hammer.json.call_args.args,
            (
                "content-view",
                "component",
                "list",
                "--composite-content-view-id",
                "52",
                "--organization",
                "ACME",
            ),
        )

    def test_hammer_dates_are_normalized_to_utc(self):
        local_timezone = datetime.now().astimezone().tzinfo
        expected = datetime(
            2026, 11, 10, 12, 30, tzinfo=local_timezone
        ).astimezone(timezone.utc)
        self.assertEqual(
            module._parse_datetime("2026/11/10 12:30:00"),
            expected,
        )


class PublicationPlanTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        self.component = module.ContentView(
            1, "Component", False, self.now - timedelta(days=3)
        )
        self.composite = module.ContentView(
            10, "Composite", True, self.now - timedelta(days=4)
        )
        self.catalog = module.Catalog(
            [self.component, self.composite],
            {10: [module.Component(100, 1, True)]},
        )
        self.versions = {
            1: module.Version(101, "2.0"),
            10: module.Version(110, "3.0"),
        }

    def build_plan(self, explicit, force=False):
        return module.build_publication_plan(
            ordered=[self.component, self.composite],
            catalog=self.catalog,
            explicitly_requested=explicit,
            current_versions=self.versions,
            max_age=timedelta(hours=24),
            force_publish=force,
            now=self.now,
        )

    def test_discovered_component_newer_than_composite_is_reused(self):
        plan = self.build_plan(explicit={10})
        self.assertFalse(plan[0].publish)
        self.assertIn("newer than every selected dependent composite", plan[0].reason)
        self.assertTrue(plan[1].publish)

    def test_recent_explicit_view_is_reused(self):
        recent = module.ContentView(1, "Component", False, self.now - timedelta(hours=12))
        catalog = module.Catalog([recent], {})
        plan = module.build_publication_plan(
            ordered=[recent],
            catalog=catalog,
            explicitly_requested={1},
            current_versions={1: module.Version(101, "2.0")},
            max_age=timedelta(hours=24),
            force_publish=False,
            now=self.now,
        )
        self.assertFalse(plan[0].publish)
        self.assertIn("freshness window", plan[0].reason)

    def test_force_publish_overrides_reuse_rules(self):
        plan = self.build_plan(explicit={10}, force=True)
        self.assertTrue(all(item.publish for item in plan))

    def test_newer_component_forces_recent_composite_publication(self):
        component = module.ContentView(
            1, "Component", False, self.now - timedelta(minutes=30)
        )
        composite = module.ContentView(
            10, "Composite", True, self.now - timedelta(hours=1)
        )
        catalog = module.Catalog(
            [component, composite],
            {10: [module.Component(100, 1, True)]},
        )
        plan = module.build_publication_plan(
            ordered=[component, composite],
            catalog=catalog,
            explicitly_requested={10},
            current_versions=self.versions,
            max_age=timedelta(hours=24),
            force_publish=False,
            now=self.now,
        )
        self.assertFalse(plan[0].publish)
        self.assertTrue(plan[1].publish)
        self.assertIn("components are newer", plan[1].reason)


class SequentialExecutionTests(unittest.TestCase):
    def test_run_waits_for_each_simple_before_the_composite(self):
        class FakeHammer:
            instance = None

            def __init__(self, executable, organization, page_size, verbose):
                self.events = []
                self.versions = {1: [], 2: [], 10: []}
                FakeHammer.instance = self

            def list_content_views(self):
                return [
                    module.ContentView(1, "Agents", False),
                    module.ContentView(2, "Base", False),
                    module.ContentView(10, "Composite", True),
                ]

            def list_components(self, composite_id):
                return [
                    module.Component(101, 1, True),
                    module.Component(102, 2, False),
                ]

            def list_environments(self):
                return [module.Environment(20, "NonProd"), module.Environment(30, "Prod")]

            def list_versions(self, content_view_id):
                return list(self.versions[content_view_id])

            def disable_auto_publish(self, view):
                self.events.append(("disable-auto", view.name))

            def publish(self, view, description):
                self.events.append(("publish", view.name))
                self.versions[view.id].append(module.Version(100 + view.id, "1.0"))

            def promote(self, version, environment):
                self.events.append(("promote", version.id, environment.name))

            def pin_component(self, composite, component, version):
                self.events.append(("pin", composite.name, component.content_view_id, version.id))

        args = Namespace(
            content_views=["Composite"],
            organization="ACME",
            environments=["NonProd", "Prod"],
            from_file=None,
            description="test",
            dry_run=False,
            keep_auto_publish=False,
            hammer="hammer",
            page_size=100,
            max_age_hours=24.0,
            force_publish=False,
            verbose=False,
        )
        with patch.object(module, "Hammer", FakeHammer), patch.object(
            module.shutil, "which", return_value="/usr/bin/hammer"
        ), redirect_stdout(io.StringIO()):
            module.run(args)

        self.assertEqual(
            FakeHammer.instance.events,
            [
                ("disable-auto", "Composite"),
                ("publish", "Agents"),
                ("promote", 101, "NonProd"),
                ("promote", 101, "Prod"),
                ("publish", "Base"),
                ("promote", 102, "NonProd"),
                ("promote", 102, "Prod"),
                ("pin", "Composite", 2, 102),
                ("publish", "Composite"),
                ("promote", 110, "NonProd"),
                ("promote", 110, "Prod"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
