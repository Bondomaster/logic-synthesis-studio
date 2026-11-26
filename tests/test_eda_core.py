import ast
import json
from pathlib import Path
import unittest

from src import eda_core


class TestLogicSynthesisAndVcdCore(unittest.TestCase):
    def test_strict_python_stdlib_imports_only(self) -> None:
        core_path = Path(eda_core.__file__).resolve()
        tree = ast.parse(core_path.read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module != "__future__":
                    imports.add(node.module.split(".")[0])

        self.assertEqual(
            imports,
            {"itertools", "collections", "re", "dataclasses", "json"},
        )

    def test_default_boolean_expression_ppa_and_critical_path(self) -> None:
        raw = eda_core.synthesize_expression_json(
            "F = A'B'C + A'BC + AB'C' + AB'C + ABC",
            "",
            "",
            "cmos",
            "synth_logic",
        )
        res = json.loads(raw)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["var_names"], ["A", "B", "C"])
        self.assertEqual(res["minterms"], [1, 3, 4, 5, 7])
        self.assertEqual(res["minimized_expr"], "C + AB'")

        ppa = res["ppa"]
        self.assertEqual(ppa["pre_gate_count"], 9)
        self.assertEqual(ppa["post_gate_count"], 3)
        self.assertAlmostEqual(ppa["critical_path_ns"], 5.5, places=2)
        self.assertAlmostEqual(ppa["fmax_mhz"], 181.82, places=2)
        self.assertGreater(ppa["area_saving_pct"], 60.0)

    def test_minterm_plus_dont_care_reduction(self) -> None:
        raw = eda_core.synthesize_expression_json("m(1,3,4,5,7) + d(6)")
        res = json.loads(raw)
        self.assertTrue(res["ok"], res)
        # With d(6) = 110, minterms {4,5,6,7} collapse into 'A' and {1,3,5,7} into 'C'
        self.assertEqual(set(res["selected_pis"]), {"A", "C"})
        self.assertEqual(res["ppa"]["post_gate_count"], 1)  # Single OR2 gate!
        self.assertAlmostEqual(res["ppa"]["critical_path_ns"], 2.5, places=2)

    def test_petrick_method_on_cyclic_prime_implicant_chart(self) -> None:
        # Classic cyclic function m(0,1,2,5,6,7): 6 PIs, 0 Essential PIs, minimum cover = 3 PIs
        raw = eda_core.synthesize_expression_json("F(A,B,C) = m(0,1,2,5,6,7)")
        res = json.loads(raw)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(res["prime_implicants"]), 6)
        self.assertEqual(res["essential_pis"], [])
        self.assertEqual(len(res["selected_pis"]), 3)

        # Verify minimized expression evaluates identically to m(0,1,2,5,6,7)
        check = json.loads(eda_core.synthesize_expression_json(f"F(A,B,C) = {res['minimized_expr']}"))
        self.assertEqual(check["minterms"], [0, 1, 2, 5, 6, 7])

    def test_eight_variable_support(self) -> None:
        raw = eda_core.synthesize_expression_json(
            "Y(A,B,C,D,E,F,G,H) = m(0,1,2,3,252,253,254,255) + d(4,5)"
        )
        res = json.loads(raw)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(res["var_names"]), 8)
        self.assertEqual(len(res["truth_table"]), 256)
        self.assertEqual(len(res["selected_pis"]), 2)

    def test_nand_technology_mapping_and_verilog_codegen(self) -> None:
        raw = eda_core.synthesize_expression_json(
            "F = A'B'C + A'BC + AB'C' + AB'C + ABC",
            "",
            "",
            "nand",
            "demo_nand",
        )
        res = json.loads(raw)
        self.assertTrue(res["ok"], res)
        bd = res["ppa"]["gate_breakdown"]
        self.assertEqual(bd["AND"], 0)
        self.assertEqual(bd["OR"], 0)
        self.assertGreater(bd["NAND"], 0)

        self.assertIn("module demo_nand (", res["verilog_module"])
        self.assertIn("$dumpfile(\"demo_nand.vcd\");", res["verilog_tb"])
        self.assertIn("$dumpvars(0, demo_nand_tb);", res["verilog_tb"])

    def test_ieee_1364_vcd_regex_parser_external_sample(self) -> None:
        sample_vcd = """
        $date Today $end
        $version Icarus Verilog $end
        $timescale 1ns $end
        $scope module top_tb $end
        $var wire 8 ! data_bus [7:0] $end
        $var wire 1 # clk $end
        $var wire 1 % tri_out $end
        $scope module dut $end
        $var wire 1 & y_out $end
        $upscope $end
        $upscope $end
        $enddefinitions $end
        $dumpvars
        bx !
        0#
        z%
        x&
        $end
        #10
        1#
        b10100101 !
        0&
        #20
        0#
        1%
        1&
        #30
        b11110000 !
        """
        parsed_res = json.loads(eda_core.parse_vcd_json(sample_vcd))
        self.assertTrue(parsed_res["ok"], parsed_res)
        wave = parsed_res["waveform"]
        self.assertEqual(wave["timescale"], "1ns")
        self.assertEqual(wave["signal_count"], 4)
        self.assertEqual(wave["end_time_ns"], 30.0)

        bus_sig = wave["signals"][0]
        self.assertEqual(bus_sig["width"], 8)
        labels = [t["label"] for t in bus_sig["transitions"]]
        self.assertEqual(labels, ["0xXX", "0xA5", "0xF0"])

        tri_states = [t["state"] for t in wave["signals"][2]["transitions"]]
        self.assertEqual(tri_states, ["z", "1"])

    def test_frontend_dom_id_and_relative_path_integrity(self) -> None:
        root = Path(eda_core.__file__).resolve().parent.parent
        html = (root / "index.html").read_text(encoding="utf-8")
        css = (root / "style.css").read_text(encoding="utf-8")
        js = (root / "app.js").read_text(encoding="utf-8")

        import re
        html_ids = set(re.findall(r'id="([^"]+)"', html))
        js_ids = set(re.findall(r'getElementById\("([^"]+)"\)', js))
        self.assertEqual(js_ids - html_ids, set())
        self.assertIn("./src/eda_core.py", js)
        self.assertIn("#121417", css)
        self.assertIn("#00FF88", css)


if __name__ == "__main__":
    unittest.main()
