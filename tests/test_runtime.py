import ast
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import goodlinks_crosspoint as package


class RuntimeVersionTests(unittest.TestCase):
    def test_runtime_guard_precedes_relative_package_imports(self) -> None:
        source = (ROOT / "src/goodlinks_crosspoint/__init__.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        guard_calls = [
            index
            for index, statement in enumerate(tree.body)
            if isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "_check_runtime_version"
            and not statement.value.args
            and not statement.value.keywords
        ]
        relative_imports = [
            index
            for index, statement in enumerate(tree.body)
            if isinstance(statement, ast.ImportFrom) and statement.level > 0
        ]

        self.assertEqual(len(guard_calls), 1)
        self.assertTrue(relative_imports)
        self.assertLess(guard_calls[0], relative_imports[0])

    def test_runtime_minimum_matches_pyproject_requirement(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        requirement = project["project"]["requires-python"]

        self.assertEqual(package._MINIMUM_PYTHON, (3, 11))
        self.assertEqual(requirement, ">=3.11")
        self.assertEqual(
            requirement,
            ">=" + ".".join(map(str, package._MINIMUM_PYTHON)),
        )

    def test_supported_versions_are_accepted(self) -> None:
        for version in ((3, 11), (3, 12), (4, 0)):
            with self.subTest(version=version):
                self.assertIsNone(package._check_runtime_version(version))

    def test_unsupported_version_has_static_required_and_detected_diagnostic(
        self,
    ) -> None:
        with self.assertRaises(RuntimeError) as raised:
            package._check_runtime_version((3, 10))

        self.assertEqual(
            str(raised.exception),
            "goodlinks-to-crosspoint requires Python 3.11 or newer; "
            "detected Python 3.10.",
        )
        self.assertNotIn(str(ROOT), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
