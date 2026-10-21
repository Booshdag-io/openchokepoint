# Handling: Unclassified — public
# Offline tests: issue lookups go through a fake.
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import closing_keyword_check as k  # noqa: E402

REPO = "Element-Ventures-Canada-Ltd/openchokepoint"
ISSUES = {
    3: {"title": "[Priority] Open call: digital-thread and critical-items register schema", "labels": []},
    21: {"title": "[Programme] Digital thread for rocket turbopump supply chains", "labels": []},
    19: {"title": "[Slice D] Standards crosswalk", "labels": [{"name": "bounty"}]},
    20: {"title": "[Slice E] Comparative real-world traces", "labels": [{"name": "keep-open"}]},
    7: {"title": "Fix validator typo", "labels": []},
    46: {"title": "ci: something", "labels": [], "pull_request": {}},
}


def check(*texts):
    sources = [(f"src{i}", t) for i, t in enumerate(texts)]
    return k.find_violations(sources, REPO, ISSUES.get)


class Keywords(unittest.TestCase):
    def test_all_nine_keywords_any_case(self):
        for word in ("close", "closes", "closed", "fix", "fixes", "fixed",
                     "resolve", "resolves", "resolved", "CLOSES", "Fixes"):
            self.assertEqual([n for n, _ in k.closing_refs(f"{word} #3", REPO)], [3], word)

    def test_colon_form(self):
        self.assertEqual([n for n, _ in k.closing_refs("Closes: #3", REPO)], [3])

    def test_same_repo_qualified_and_url_forms(self):
        text = (f"Fixes {REPO}#3\nResolves https://github.com/{REPO}/issues/21")
        self.assertEqual([n for n, _ in k.closing_refs(text, REPO)], [3, 21])

    def test_other_repo_ignored(self):
        self.assertEqual(k.closing_refs("Closes other-org/other-repo#3", REPO), [])

    def test_refs_and_plain_mentions_ignored(self):
        self.assertEqual(k.closing_refs("Refs #3. See #3. Part of #21. (#3)", REPO), [])

    def test_keyword_inside_word_ignored(self):
        self.assertEqual(k.closing_refs("prefixes #3 and enclosed #3", REPO), [])

    def test_multiple_in_one_line(self):
        self.assertEqual([n for n, _ in k.closing_refs("Closes #3, closes #19", REPO)], [3, 19])


class Protection(unittest.TestCase):
    def test_open_call_blocked(self):
        v = check("Closes #3")
        self.assertEqual([(x["num"], x["phrase"]) for x in v], [(3, "Closes #3")])

    def test_programme_blocked(self):
        self.assertEqual([x["num"] for x in check("fixes #21")], [21])

    def test_keep_open_label_blocked(self):
        self.assertEqual([x["num"] for x in check("Resolves #20")], [20])

    def test_slice_and_task_issues_allowed(self):
        self.assertEqual(check("Closes #19", "Fixes #7"), [])

    def test_unknown_issue_and_pull_request_allowed(self):
        self.assertEqual(check("Closes #999", "Closes #46"), [])

    def test_commit_message_caught_even_when_description_is_clean(self):
        v = k.find_violations([("pull request description", "Refs #3"),
                               ("commit abc1234", "feat: trace\n\nCloses #3")], REPO, ISSUES.get)
        self.assertEqual([x["where"] for x in v], ["commit abc1234"])

    def test_each_issue_looked_up_once(self):
        calls = []

        def get(n):
            calls.append(n)
            return ISSUES.get(n)
        k.find_violations([("a", "Closes #3"), ("b", "Fixes #3")], REPO, get)
        self.assertEqual(calls, [3])

    def test_lookup_failure_propagates(self):
        def get(n):
            raise k.LookupError_("HTTP 500")
        with self.assertRaises(k.LookupError_):
            k.find_violations([("a", "Closes #3")], REPO, get)


if __name__ == "__main__":
    unittest.main()
