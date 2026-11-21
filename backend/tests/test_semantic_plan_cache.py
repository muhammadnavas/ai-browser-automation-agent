import unittest
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
for path in [str(BACKEND_DIR), str(ROOT_DIR)]:
    if path not in sys.path:
        sys.path.insert(0, path)

from services.semantic_plan_cache import SemanticPlanCache


class SemanticPlanCacheSimilarityTests(unittest.TestCase):
    def test_cosine_similarity_identical_vectors(self):
        self.assertAlmostEqual(
            SemanticPlanCache.cosine_similarity([1.0, 0.0], [1.0, 0.0]),
            1.0,
        )

    def test_cosine_similarity_orthogonal_vectors(self):
        self.assertAlmostEqual(
            SemanticPlanCache.cosine_similarity([1.0, 0.0], [0.0, 1.0]),
            0.0,
        )

    def test_cosine_similarity_rejects_dimension_mismatch(self):
        self.assertEqual(
            SemanticPlanCache.cosine_similarity([1.0, 0.0], [1.0]),
            0.0,
        )

    def test_lexical_overlap_accepts_paraphrase_with_same_entities(self):
        score = SemanticPlanCache.lexical_overlap(
            "Find Python tutorials on YouTube",
            "Search YouTube for Python tutorials",
        )
        self.assertGreaterEqual(score, 0.70)

    def test_lexical_overlap_rejects_different_target_entities(self):
        score = SemanticPlanCache.lexical_overlap(
            "Find Python tutorials on YouTube",
            "Find React tutorials on YouTube",
        )
        self.assertLess(score, 0.70)


if __name__ == "__main__":
    unittest.main()
