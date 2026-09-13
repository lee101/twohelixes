import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("builder", Path(__file__).parents[1] / "scripts/build_dataset.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class RankingTests(unittest.TestCase):
    def rows(self):
        return [dict(school_id=s,year_level="3",assessment_year="2025",domain=d,mean_score=str(v))
                for s,v in [("a",400),("b",500),("c",500)] for d in builder.WEIGHTS]

    def test_order_and_ties(self):
        result=builder.rank_results(self.rows())
        self.assertEqual(result["b"]["rank"],result["c"]["rank"])
        self.assertEqual(result["a"]["rank"],3)
        self.assertGreater(result["b"]["achievement_score"], result["a"]["achievement_score"])

    def test_suppression_and_duplicates(self):
        rows=self.rows()
        rows[0]["mean_score"]="*"
        self.assertNotIn("a",builder.rank_results(rows))
        with self.assertRaises(ValueError): builder.rank_results(rows+[rows[0]])

    def test_release_and_nonfinite(self):
        rows=self.rows(); rows[0]["assessment_year"]="2022"
        with self.assertRaises(ValueError): builder.rank_results(rows)
        rows=self.rows(); rows[0]["mean_score"]="nan"
        with self.assertRaises(ValueError): builder.rank_results(rows)
