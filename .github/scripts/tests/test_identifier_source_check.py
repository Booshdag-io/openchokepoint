# Handling: Unclassified — public
# Tests run offline: every fetch goes through a fake fetcher.
import socket
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import identifier_source_check as c  # noqa: E402

SRC_A = "https://example.com/a"
SRC_B = "https://example.com/b"


def fake(pages):
    def fetcher(url):
        page = pages.get(url)
        if isinstance(page, Exception):
            raise page
        if page is None:
            raise c.FetchError("HTTP 404")
        return c.page_text(page)
    return fetcher


def comp(part, sources, synthetic=False, **extra):
    obj = dict(type="Component", id="cmp-x", part_number=part, sources=sources, synthetic=synthetic)
    obj.update(extra)
    return obj


class Matching(unittest.TestCase):
    def test_found_in_source_passes(self):
        ch = c.Checker(fake({SRC_A: "<p>The RX-100 pump housing</p>"}))
        self.assertEqual(ch.check_object(comp("RX-100", [SRC_A]))[0], "pass")

    def test_invented_identifier_fails(self):
        ch = c.Checker(fake({SRC_A: "<p>Thirteen engines on the first stage.</p>"}))
        status, detail = ch.check_object(comp("TR-PROP-STAGE1", [SRC_A]))
        self.assertEqual(status, "fail")
        self.assertIn("does not appear", detail)

    def test_synthetic_record_is_skipped(self):
        ch = c.Checker(fake({}))
        self.assertEqual(ch.check_object(comp("ANY-1", [SRC_A], synthetic=True))[0], "skip")

    def test_missing_synthetic_flag_is_treated_as_real(self):
        obj = comp("ANY-1", [SRC_A])
        del obj["synthetic"]
        self.assertEqual(c.Checker(fake({SRC_A: "nothing"})).check_object(obj)[0], "fail")

    def test_case_dash_and_entity_normalization(self):
        page = "<div>Serial rx&ndash;100&nbsp;B</div>"
        ch = c.Checker(fake({SRC_A: page}))
        self.assertEqual(ch.check_object(comp("RX-100 b", [SRC_A]))[0], "pass")

    def test_identifier_split_by_inline_markup(self):
        ch = c.Checker(fake({SRC_A: "<p>Part RX-<b>100</b></p>"}))
        self.assertEqual(ch.check_object(comp("RX-100", [SRC_A]))[0], "pass")

    def test_script_text_does_not_count(self):
        ch = c.Checker(fake({SRC_A: "<script>var p='RX-100';</script><p>no id here</p>"}))
        self.assertEqual(ch.check_object(comp("RX-100", [SRC_A]))[0], "fail")

    def test_second_source_can_confirm(self):
        ch = c.Checker(fake({SRC_A: "nothing", SRC_B: "RX-100"}))
        self.assertEqual(ch.check_object(comp("RX-100", [SRC_A, SRC_B])), ("pass", SRC_B))

    def test_citation_counts_as_a_source(self):
        obj = dict(type="SerialItem", id="ser-x", serial="SN-0042", sources=[SRC_A],
                   citation=SRC_B, synthetic=False)
        ch = c.Checker(fake({SRC_A: "nothing", SRC_B: "serial SN-0042"}))
        self.assertEqual(ch.check_object(obj)[0], "pass")

    def test_material_lot_checked(self):
        obj = dict(type="MaterialLot", id="lot-x", lot_number="L-77", sources=[SRC_A], synthetic=False)
        self.assertEqual(c.Checker(fake({SRC_A: "nothing"})).check_object(obj)[0], "fail")

    def test_other_types_ignored(self):
        obj = dict(type="Entity", id="ent-x", name="X", sources=[SRC_A], synthetic=False)
        self.assertEqual(c.Checker(fake({})).check_object(obj)[0], "skip")


class FailClosed(unittest.TestCase):
    def test_unreadable_source_fails(self):
        ch = c.Checker(fake({SRC_A: c.FetchError("timed out")}))
        status, detail = ch.check_object(comp("RX-100", [SRC_A]))
        self.assertEqual(status, "fail")
        self.assertIn("could be read", detail)

    def test_no_sources_fails(self):
        self.assertEqual(c.Checker(fake({})).check_object(comp("RX-100", []))[0], "fail")

    def test_fetch_cap(self):
        calls = []

        def fetcher(url):
            calls.append(url)
            return "nothing"
        ch = c.Checker(fetcher, max_fetches=1)
        ch.check_object(comp("RX-100", [SRC_A, SRC_B]))
        self.assertEqual(calls, [SRC_A])
        self.assertIsInstance(ch.cache[SRC_B], c.FetchError)

    def test_each_url_fetched_once(self):
        calls = []

        def fetcher(url):
            calls.append(url)
            return "RX-100"
        ch = c.Checker(fetcher)
        ch.check_object(comp("RX-100", [SRC_A]))
        ch.check_object(comp("RX-100", [SRC_A]))
        self.assertEqual(calls, [SRC_A])


class UrlSafety(unittest.TestCase):
    @staticmethod
    def resolver(ip):
        return lambda host, port, proto=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def test_public_https_allowed(self):
        self.assertTrue(c.is_public_https("https://example.com/x", self.resolver("93.184.216.34")))

    def test_http_refused(self):
        self.assertFalse(c.is_public_https("http://example.com/x", self.resolver("93.184.216.34")))

    def test_private_and_metadata_addresses_refused(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "0.0.0.0"):
            self.assertFalse(c.is_public_https("https://evil.test/x", self.resolver(ip)), ip)

    def test_credentials_in_url_refused(self):
        self.assertFalse(c.is_public_https("https://u:p@example.com/", self.resolver("93.184.216.34")))


class FileLevel(unittest.TestCase):
    def test_file_reports_line_and_skips_synthetic(self):
        content = textwrap.dedent(f"""\
            objects:
              - type: Component
                id: cmp-real
                part_number: "INVENTED-1"
                sources: ["{SRC_A}"]
                synthetic: false
              - type: SerialItem
                id: ser-fake
                serial: "SN-FAKE"
                sources: ["{SRC_A}"]
                synthetic: true
            """)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "trace.yaml"
            p.write_text(content, encoding="utf-8")
            passes, failures = c.Checker(fake({SRC_A: "nothing"})).check_file(p)
        self.assertEqual(passes, [])
        self.assertEqual([f["id"] for f in failures], ["cmp-real"])
        self.assertEqual(failures[0]["line"], 4)

    def test_main_ignores_provenance_files(self):
        self.assertEqual(c.main(["data/x/provenance.yaml"]), 0)


if __name__ == "__main__":
    unittest.main()
