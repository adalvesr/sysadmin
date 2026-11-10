import importlib.util
import io
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


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


class SequentialExecutionTests(unittest.TestCase):
    def test_run_waits_for_each_simple_before_the_composite(self):
        class FakeHammer:
            instance = None

            def __init__(self, executable, organization, verbose):
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
