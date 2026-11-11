"""categories.yaml must fail loudly at load time, never silently at apply time."""

import tempfile
import unittest
from pathlib import Path

from finanzas import categories


def load_text(text: str):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(text)
    return categories.load(Path(f.name))


class LoadCategories(unittest.TestCase):
    def test_real_file_loads(self):
        cats = categories.load()
        self.assertIn("supermercado", cats)
        self.assertEqual(cats["salidas-fiesta"].type, "expense")      # inherited
        self.assertEqual(cats["internos-inversion"].type, "internal")
        self.assertTrue(all(c.parent is None or cats[c.parent].parent is None
                            for c in cats.values()))

    def test_duplicate_id(self):
        with self.assertRaises(ValueError):
            load_text("- {id: a, name: A, type: expense}\n- {id: a, name: B, type: income}\n")

    def test_unresolved_parent(self):
        with self.assertRaises(ValueError):
            load_text("- {id: a, name: A, parent: nope, type: expense}\n")

    def test_bad_type(self):
        with self.assertRaises(ValueError):
            load_text("- {id: a, name: A, type: gasto}\n")

    def test_child_contradicting_parent_type(self):
        with self.assertRaises(ValueError):
            load_text("- {id: a, name: A, type: expense}\n- {id: b, name: B, parent: a, type: income}\n")

    def test_three_levels_rejected(self):
        with self.assertRaises(ValueError):
            load_text("- {id: a, name: A, type: expense}\n- {id: b, name: B, parent: a}\n"
                      "- {id: c, name: C, parent: b}\n")

    def test_require_unknown_category(self):
        cats = categories.load()
        with self.assertRaises(ValueError):
            categories.require(cats, "supermecado", "rules.yaml rule 'mercadona'")


if __name__ == "__main__":
    unittest.main()
