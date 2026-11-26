"""
Logic Synthesis Studio & Industrial VCD Waveform Viewer — Pure-Python EDA Core.

Written strictly with Python Standard Library modules:
  `itertools`, `collections`, `re`, `dataclasses`, `json`

Capabilities:
1. Boolean Expression & Minterm/Don't-Care Parser (1 to 8 variables).
2. Exact Two-Level Logic Minimization via Quine-McCluskey + Petrick's Method.
3. Technology Mapping (CMOS NOT/AND/OR & Universal NOT/NAND), PPA Area/Gate
   Estimation, and Static Timing Analysis (Critical Path & Fmax).
4. Synthesizable Verilog-2001 (.v) + Self-Checking VCD Testbench (_tb.v) Codegen.
5. Gate-Delay Event-Driven VCD Waveform Generator & IEEE 1364 Regex VCD Parser.
"""

from __future__ import annotations

import collections
import dataclasses
import itertools
import json
import re


# -----------------------------------------------------------------------------
# Technology Library Constants (ns & Gate-Equivalent Area)
# -----------------------------------------------------------------------------

GATE_DELAY_NS: dict[str, float] = {
    "NOT": 1.0,
    "NAND": 1.5,
    "AND": 2.0,
    "OR": 2.5,
    "BUF": 0.0,
}

GATE_AREA_GE: dict[str, float] = {
    "NOT": 1.0,
    "NAND": 1.5,
    "AND": 2.0,
    "OR": 2.5,
    "BUF": 0.0,
}


# -----------------------------------------------------------------------------
# Data Models
# -----------------------------------------------------------------------------

@dataclasses.dataclass
class ImplicantInfo:
    index: int
    value_bits: int
    mask_bits: int
    pattern: str
    algebraic: str
    covered_minterms: list[int]
    literal_count: int
    is_essential: bool
    is_selected: bool


@dataclasses.dataclass
class NetlistNode:
    id: str
    kind: str  # "INPUT" | "CONST" | "NOT" | "AND" | "OR" | "NAND" | "OUTPUT"
    label: str
    layer: int
    x: float
    y: float
    delay_ns: float
    arrival_ns: float
    inputs: list[str]
    is_critical: bool


@dataclasses.dataclass
class NetlistWire:
    id: str
    source: str
    target: str
    pin_index: int
    pin_count: int
    label: str
    arrival_ns: float
    is_critical: bool


@dataclasses.dataclass
class PpaReport:
    tech_mode: str
    pre_gate_count: int
    post_gate_count: int
    gate_saving_pct: float
    pre_area_ge: float
    post_area_ge: float
    area_saving_pct: float
    pre_literals: int
    post_literals: int
    pre_critical_ns: float
    critical_path_ns: float
    fmax_mhz: float
    gate_breakdown: dict[str, int]
    critical_path_nodes: list[str]
    critical_path_summary: list[str]


# -----------------------------------------------------------------------------
# 1. Boolean Expression & Minterm Parser (1..8 Variables)
# -----------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\s*(?:([01])|([A-Za-z][0-9]*)|([+'|*&.^~!()]))")
_LHS_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*([A-Za-z0-9_,\s]+)\s*\))?\s*=\s*(.+)$",
    re.DOTALL,
)
_MINTERM_CALL_RE = re.compile(r"(?:∑|Σ|sum\s*)?[mM]\s*\(\s*([0-9,\s\-]*)\s*\)")
_DONTCARE_CALL_RE = re.compile(r"[dD]\s*\(\s*([0-9,\s\-]*)\s*\)")


class SynthesisError(ValueError):
    """Raised on malformed Boolean expressions, minterm specs, or VCD files."""


def _parse_int_list(spec: str) -> set[int]:
    result: set[int] = set()
    cleaned = spec.strip()
    if not cleaned:
        return result
    for part in cleaned.split(","):
        tok = part.strip()
        if not tok:
            continue
        if "-" in tok:
            bounds = tok.split("-", 1)
            lo, hi = int(bounds[0].strip()), int(bounds[1].strip())
            if lo > hi:
                lo, hi = hi, lo
            for idx in range(lo, hi + 1):
                result.add(idx)
        else:
            result.add(int(tok))
    return result


def _tokenize_boolean_expr(expr: str) -> list[tuple[str, str]]:
    normalized = (
        expr.replace("’", "'")
        .replace("‘", "'")
        .replace("`", "'")
        .replace("⊕", "^")
        .replace("·", "*")
        .replace("¬", "~")
    )
    # Convert standalone word operators while preserving single-letter variables
    normalized = re.sub(r"\bNOT\b", "!", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bAND\b", "&", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bXOR\b", "^", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bOR\b", "|", normalized, flags=re.IGNORECASE)

    raw_tokens: list[tuple[str, str]] = []
    pos = 0
    n = len(normalized)
    while pos < n:
        if normalized[pos:].isspace():
            break
        m = _TOKEN_RE.match(normalized, pos)
        if not m:
            snippet = normalized[pos : pos + 12]
            raise SynthesisError(f"Ký tự không hợp lệ trong biểu thức tại '{snippet}'")
        const_tok, ident_tok, op_tok = m.groups()
        if const_tok is not None:
            raw_tokens.append(("CONST", const_tok))
        elif ident_tok is not None:
            raw_tokens.append(("IDENT", ident_tok))
        else:
            raw_tokens.append(("OP", op_tok))
        pos = m.end()

    if not raw_tokens:
        raise SynthesisError("Biểu thức Boolean đang để trống.")

    # Insert explicit '&' for implicit juxtaposition: AB, A'B, (A+B)(C+D), A(B+C), A'~B
    out: list[tuple[str, str]] = []
    for tok in raw_tokens:
        if out:
            prev_kind, prev_val = out[-1]
            curr_kind, curr_val = tok
            prev_ends_term = prev_kind in ("IDENT", "CONST") or (
                prev_kind == "OP" and prev_val in (")", "'")
            )
            curr_starts_term = curr_kind in ("IDENT", "CONST") or (
                curr_kind == "OP" and curr_val in ("(", "~", "!")
            )
            if prev_ends_term and curr_starts_term:
                out.append(("OP", "&"))
        out.append(tok)
    return out


class _BooleanAstParser:
    """Recursive-descent Boolean expression parser returning nested tuple ASTs."""

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _consume(self) -> tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> tuple:
        node = self._parse_or()
        if self.pos < len(self.tokens):
            raise SynthesisError(f"Cú pháp dư thừa tại token '{self.tokens[self.pos][1]}'")
        return node

    def _parse_or(self) -> tuple:
        node = self._parse_xor()
        while True:
            nxt = self._peek()
            if nxt and nxt[0] == "OP" and nxt[1] in ("+", "|"):
                self._consume()
                rhs = self._parse_xor()
                node = ("OR", node, rhs)
            else:
                break
        return node

    def _parse_xor(self) -> tuple:
        node = self._parse_and()
        while True:
            nxt = self._peek()
            if nxt and nxt[0] == "OP" and nxt[1] == "^":
                self._consume()
                rhs = self._parse_and()
                node = ("XOR", node, rhs)
            else:
                break
        return node

    def _parse_and(self) -> tuple:
        node = self._parse_prefix()
        while True:
            nxt = self._peek()
            if nxt and nxt[0] == "OP" and nxt[1] in ("&", "*", "."):
                self._consume()
                rhs = self._parse_prefix()
                node = ("AND", node, rhs)
            else:
                break
        return node

    def _parse_prefix(self) -> tuple:
        nxt = self._peek()
        if nxt and nxt[0] == "OP" and nxt[1] in ("~", "!"):
            self._consume()
            return ("NOT", self._parse_prefix())
        return self._parse_postfix()

    def _parse_postfix(self) -> tuple:
        node = self._parse_primary()
        while True:
            nxt = self._peek()
            if nxt and nxt[0] == "OP" and nxt[1] == "'":
                self._consume()
                node = ("NOT", node)
            else:
                break
        return node

    def _parse_primary(self) -> tuple:
        nxt = self._peek()
        if nxt is None:
            raise SynthesisError("Biểu thức kết thúc đột ngột (thiếu toán hạng).")
        kind, val = self._consume()
        if kind == "CONST":
            return ("CONST", int(val))
        if kind == "IDENT":
            return ("VAR", val)
        if kind == "OP" and val == "(":
            inner = self._parse_or()
            closing = self._peek()
            if not closing or closing != ("OP", ")"):
                raise SynthesisError("Thiếu dấu đóng ngoặc ')'.")
            self._consume()
            return inner
        raise SynthesisError(f"Toán hạng không hợp lệ: '{val}'")


def _eval_bool_ast(node: tuple, env: dict[str, int]) -> int:
    op = node[0]
    if op == "CONST":
        return int(node[1])
    if op == "VAR":
        return env[node[1]]
    if op == "NOT":
        return 1 - _eval_bool_ast(node[1], env)
    if op == "AND":
        return _eval_bool_ast(node[1], env) & _eval_bool_ast(node[2], env)
    if op == "OR":
        return _eval_bool_ast(node[1], env) | _eval_bool_ast(node[2], env)
    if op == "XOR":
        return _eval_bool_ast(node[1], env) ^ _eval_bool_ast(node[2], env)
    return 0


def parse_specification(
    expr_or_minterms: str,
    dont_care_str: str = "",
    var_names_csv: str = "",
) -> tuple[str, list[str], set[int], set[int]]:
    """
    Parses either a Boolean algebraic expression or a minterm/don't-care list.
    Returns (output_name, var_names, minterms_set, dont_cares_set).
    """
    raw = expr_or_minterms.strip()
    if not raw:
        raise SynthesisError("Vui lòng nhập biểu thức Boolean hoặc danh sách Minterm.")

    output_name = "F"
    explicit_vars: list[str] = []
    rhs = raw

    lhs_match = _LHS_RE.match(raw)
    if lhs_match:
        output_name = lhs_match.group(1)
        if lhs_match.group(2):
            explicit_vars = [v.strip() for v in lhs_match.group(2).split(",") if v.strip()]
        rhs = lhs_match.group(3).strip()

    if var_names_csv.strip():
        explicit_vars = [v.strip() for v in var_names_csv.split(",") if v.strip()]

    # Parse extra don't-care string if supplied separately (e.g., "2, 6" or "d(2, 6)")
    extra_dc: set[int] = set()
    dc_clean = dont_care_str.strip()
    if dc_clean:
        dc_call = _DONTCARE_CALL_RE.search(dc_clean)
        if dc_call:
            extra_dc |= _parse_int_list(dc_call.group(1))
        else:
            extra_dc |= _parse_int_list(dc_clean)

    # Mode 1: Minterm notation m(...) + d(...)
    m_match = _MINTERM_CALL_RE.search(rhs)
    if m_match:
        minterms = _parse_int_list(m_match.group(1))
        dc_inline = _DONTCARE_CALL_RE.search(rhs)
        dont_cares = (_parse_int_list(dc_inline.group(1)) if dc_inline else set()) | extra_dc
        dont_cares -= minterms

        max_idx = max(minterms | dont_cares, default=0)
        inferred_n = max(1, max_idx.bit_length())
        if explicit_vars:
            var_names = explicit_vars
        else:
            if inferred_n > 8:
                raise SynthesisError(f"Chỉ số minterm {max_idx} vượt quá giới hạn 8 biến (0..255).")
            var_names = [chr(ord("A") + i) for i in range(inferred_n)]

        if not 1 <= len(var_names) <= 8:
            raise SynthesisError("Hệ thống hỗ trợ từ 1 đến tối đa 8 biến Boolean.")
        limit = 1 << len(var_names)
        out_of_range = [x for x in (minterms | dont_cares) if x < 0 or x >= limit]
        if out_of_range:
            raise SynthesisError(
                f"Minterm/Don't-care {out_of_range} vượt quá phạm vi {len(var_names)} biến (0..{limit - 1})."
            )
        return output_name, var_names, minterms, dont_cares

    # Mode 2: Algebraic Boolean Expression
    tokens = _tokenize_boolean_expr(rhs)
    discovered: list[str] = []
    for kind, val in tokens:
        if kind == "IDENT" and val not in discovered:
            discovered.append(val)

    if explicit_vars:
        var_names = list(explicit_vars)
        for v in discovered:
            if v not in var_names:
                var_names.append(v)
    elif discovered:
        var_names = sorted(discovered, key=lambda s: (s[0].upper(), len(s), s))
    else:
        var_names = ["A"]

    if not 1 <= len(var_names) <= 8:
        raise SynthesisError(f"Phát hiện {len(var_names)} biến ({', '.join(var_names)}); tối đa hỗ trợ 8 biến.")

    ast_root = _BooleanAstParser(tokens).parse()
    n_vars = len(var_names)
    limit = 1 << n_vars
    bad_dc = [x for x in extra_dc if x < 0 or x >= limit]
    if bad_dc:
        raise SynthesisError(f"Don't-care {bad_dc} nằm ngoài phạm vi 0..{limit - 1}.")

    minterms = set()
    for idx, bits in enumerate(itertools.product((0, 1), repeat=n_vars)):
        if idx in extra_dc:
            continue
        env = dict(zip(var_names, bits))
        if _eval_bool_ast(ast_root, env) == 1:
            minterms.add(idx)

    dont_cares = set(extra_dc)
    return output_name, var_names, minterms, dont_cares


# -----------------------------------------------------------------------------
# 2. Quine-McCluskey + Petrick's Method Exact Minimizer
# -----------------------------------------------------------------------------

def _format_implicant_pattern(val: int, mask: int, n_vars: int) -> str:
    chars: list[str] = []
    for bit_pos in range(n_vars - 1, -1, -1):
        if (mask >> bit_pos) & 1:
            chars.append("-")
        elif (val >> bit_pos) & 1:
            chars.append("1")
        else:
            chars.append("0")
    return "".join(chars)


def _format_implicant_algebraic(val: int, mask: int, var_names: list[str]) -> str:
    n_vars = len(var_names)
    parts: list[str] = []
    for i, name in enumerate(var_names):
        bit_pos = n_vars - 1 - i
        if (mask >> bit_pos) & 1:
            continue
        if (val >> bit_pos) & 1:
            parts.append(name)
        else:
            parts.append(f"{name}'")
    return "".join(parts) if parts else "1"


def _implicant_literal_count(mask: int, n_vars: int) -> int:
    return n_vars - mask.bit_count()


def _absorb_petrick_terms(terms: set[frozenset[int]]) -> list[frozenset[int]]:
    """Applies X + X*Y = X absorption law to keep Petrick SOP minimal."""
    ordered = sorted(terms, key=lambda s: (len(s), tuple(sorted(s))))
    kept: list[frozenset[int]] = []
    for cand in ordered:
        if not any(existing.issubset(cand) for existing in kept):
            kept.append(cand)
    return kept


def quine_mccluskey_minimize(
    var_names: list[str],
    minterms: set[int],
    dont_cares: set[int],
) -> tuple[str, list[ImplicantInfo], list[tuple[int, int]]]:
    """
    Runs full Quine-McCluskey Prime Implicant extraction followed by
    Essential Prime Implicant detection and Petrick's Method for cyclic cores.

    Returns (minimized_sop_str, prime_implicant_table, selected_implicant_tuples).
    """
    n_vars = len(var_names)
    total_rows = 1 << n_vars

    if not minterms:
        return "0", [], []

    universe = minterms | dont_cares
    if len(universe) == total_rows:
        pi_all = ImplicantInfo(
            index=0,
            value_bits=0,
            mask_bits=total_rows - 1,
            pattern="-" * n_vars,
            algebraic="1",
            covered_minterms=sorted(minterms),
            literal_count=0,
            is_essential=True,
            is_selected=True,
        )
        return "1", [pi_all], [(0, total_rows - 1)]

    # Step 1: Iterative bitmask grouping & 1-bit Hamming distance merging
    current_level: set[tuple[int, int]] = {(m, 0) for m in universe}
    prime_raw: set[tuple[int, int]] = set()

    while current_level:
        groups: dict[int, list[tuple[int, int]]] = collections.defaultdict(list)
        for val, mask in current_level:
            groups[val.bit_count()].append((val, mask))

        used: set[tuple[int, int]] = set()
        next_level: set[tuple[int, int]] = set()

        sorted_keys = sorted(groups.keys())
        for g in sorted_keys:
            if (g + 1) not in groups:
                continue
            for v1, m1 in groups[g]:
                for v2, m2 in groups[g + 1]:
                    if m1 != m2:
                        continue
                    diff = v1 ^ v2
                    if diff != 0 and (diff & (diff - 1)) == 0:
                        used.add((v1, m1))
                        used.add((v2, m2))
                        next_level.add((v1 & ~diff, m1 | diff))

        for item in current_level:
            if item not in used:
                prime_raw.add(item)

        current_level = next_level

    # Sort Prime Implicants deterministically (fewer literals first, then pattern)
    ordered_pis = sorted(
        prime_raw,
        key=lambda vm: (
            _implicant_literal_count(vm[1], n_vars),
            _format_implicant_pattern(vm[0], vm[1], n_vars),
        ),
    )

    # Filter out PIs that cover zero required minterms (only don't-cares)
    active_pis: list[tuple[int, int, list[int]]] = []
    for val, mask in ordered_pis:
        covered = sorted(mt for mt in minterms if (mt & ~mask) == val)
        if covered:
            active_pis.append((val, mask, covered))

    # Step 2: Essential Prime Implicant Chart
    minterm_to_pis: dict[int, list[int]] = collections.defaultdict(list)
    for pi_idx, (_val, _mask, covered) in enumerate(active_pis):
        for mt in covered:
            minterm_to_pis[mt].append(pi_idx)

    essential_indices: set[int] = set()
    for mt, covering in minterm_to_pis.items():
        if len(covering) == 1:
            essential_indices.add(covering[0])

    covered_by_epi: set[int] = set()
    for idx in essential_indices:
        covered_by_epi.update(active_pis[idx][2])

    uncovered = sorted(minterms - covered_by_epi)

    # Step 3: Petrick's Method on remaining uncovered minterms
    selected_indices: set[int] = set(essential_indices)
    if uncovered:
        # Build POS clauses P = prod(sum(PIs covering mt))
        clauses = [set(minterm_to_pis[mt]) for mt in uncovered]
        # Sort smallest clauses first so absorption prunes aggressively
        clauses.sort(key=len)

        product_terms: list[frozenset[int]] = [frozenset([pi]) for pi in clauses[0]]
        for clause in clauses[1:]:
            expanded: set[frozenset[int]] = set()
            for term in product_terms:
                if term & clause:
                    expanded.add(term)
                else:
                    for pi in clause:
                        expanded.add(term | frozenset([pi]))
            product_terms = _absorb_petrick_terms(expanded)
            # Cap extreme pathological cyclic expansions while preserving shortest covers
            if len(product_terms) > 600:
                product_terms = product_terms[:600]

        def _petrick_cost(candidate: frozenset[int]) -> tuple[int, int, tuple[int, ...]]:
            lit_sum = sum(_implicant_literal_count(active_pis[i][1], n_vars) for i in candidate)
            return (len(candidate), lit_sum, tuple(sorted(candidate)))

        best_extra = min(product_terms, key=_petrick_cost)
        selected_indices |= set(best_extra)

    pi_infos: list[ImplicantInfo] = []
    selected_tuples: list[tuple[int, int]] = []
    selected_terms_str: list[str] = []

    for idx, (val, mask, covered) in enumerate(active_pis):
        alg = _format_implicant_algebraic(val, mask, var_names)
        is_ess = idx in essential_indices
        is_sel = idx in selected_indices
        pi_infos.append(
            ImplicantInfo(
                index=idx,
                value_bits=val,
                mask_bits=mask,
                pattern=_format_implicant_pattern(val, mask, n_vars),
                algebraic=alg,
                covered_minterms=covered,
                literal_count=_implicant_literal_count(mask, n_vars),
                is_essential=is_ess,
                is_selected=is_sel,
            )
        )
        if is_sel:
            selected_tuples.append((val, mask))
            selected_terms_str.append(alg)

    sop_expr = " + ".join(selected_terms_str) if selected_terms_str else "0"
    return sop_expr, pi_infos, selected_tuples


# -----------------------------------------------------------------------------
# 3. Gate-Level Netlist Construction, Schematic Layout & PPA / STA Engine
# -----------------------------------------------------------------------------

def _estimate_canonical_pre_ppa(
    var_names: list[str],
    minterms: set[int],
) -> tuple[int, float, int, float]:
    """
    Computes unoptimized canonical SOP baseline:
    Returns (pre_gate_count, pre_area_ge, pre_literals, pre_critical_ns).
    """
    n_vars = len(var_names)
    if not minterms:
        return 0, 0.0, 0, 0.0

    # Which variables ever appear complemented in any minterm?
    inv_vars: set[int] = set()
    for mt in minterms:
        for i in range(n_vars):
            bit_pos = n_vars - 1 - i
            if ((mt >> bit_pos) & 1) == 0:
                inv_vars.add(i)

    not_count = len(inv_vars)
    and_count = len(minterms) if n_vars >= 2 else 0
    or_count = 1 if len(minterms) >= 2 else 0

    total_gates = not_count + and_count + or_count
    pre_literals = len(minterms) * n_vars

    and_unit_area = GATE_AREA_GE["AND"] + 0.5 * max(0, n_vars - 2)
    or_unit_area = GATE_AREA_GE["OR"] + 0.5 * max(0, len(minterms) - 2)
    total_area = (
        not_count * GATE_AREA_GE["NOT"]
        + and_count * and_unit_area
        + (or_unit_area if or_count else 0.0)
    )

    crit_ns = (
        (GATE_DELAY_NS["NOT"] if not_count > 0 else 0.0)
        + (GATE_DELAY_NS["AND"] if and_count > 0 else 0.0)
        + (GATE_DELAY_NS["OR"] if or_count > 0 else 0.0)
    )
    return total_gates, round(total_area, 2), pre_literals, round(crit_ns, 2)


def build_gate_netlist_and_ppa(
    output_name: str,
    var_names: list[str],
    minterms: set[int],
    selected_implicants: list[tuple[int, int]],
    tech_mode: str = "cmos",
) -> tuple[list[NetlistNode], list[NetlistWire], PpaReport]:
    """
    Maps minimized SOP implicants onto gate primitives (`cmos`: NOT/AND/OR,
    `nand`: NOT/NAND universal network), assigns clean schematic (x, y) coordinates,
    and performs topological Static Timing Analysis (Critical Path & Fmax).
    """
    n_vars = len(var_names)
    mode = "nand" if tech_mode.lower() == "nand" else "cmos"
    nodes: dict[str, NetlistNode] = {}
    node_order: list[str] = []
    wires: list[NetlistWire] = []

    def add_node(
        nid: str,
        kind: str,
        label: str,
        layer: int,
        inputs: list[str],
    ) -> str:
        delay = GATE_DELAY_NS.get(kind, 0.0)
        arr = (
            round(max((nodes[src].arrival_ns for src in inputs), default=0.0) + delay, 4)
            if kind not in ("INPUT", "CONST", "OUTPUT")
            else (nodes[inputs[0]].arrival_ns if inputs else 0.0)
        )
        nodes[nid] = NetlistNode(
            id=nid,
            kind=kind,
            label=label,
            layer=layer,
            x=0.0,
            y=0.0,
            delay_ns=delay,
            arrival_ns=arr,
            inputs=list(inputs),
            is_critical=False,
        )
        node_order.append(nid)
        return nid

    # Layer 0: Primary Inputs
    for v in var_names:
        add_node(f"in_{v}", "INPUT", v, 0, [])

    total_rows = 1 << n_vars
    # Extract term literal lists: list of list[(var_name, is_positive)]
    term_specs: list[list[tuple[str, bool]]] = []
    for val, mask in selected_implicants:
        lits: list[tuple[str, bool]] = []
        for i, v in enumerate(var_names):
            bit_pos = n_vars - 1 - i
            if (mask >> bit_pos) & 1:
                continue
            lits.append((v, bool((val >> bit_pos) & 1)))
        term_specs.append(lits)

    # Constant 0 or 1 cases
    if not selected_implicants:
        c_id = add_node("const_0", "CONST", "1'b0", 1, [])
        out_driver = c_id
    elif len(selected_implicants) == 1 and selected_implicants[0][1] == total_rows - 1:
        c_id = add_node("const_1", "CONST", "1'b1", 1, [])
        out_driver = c_id
    else:
        inv_map: dict[str, str] = {}

        def get_inverted_input(var_name: str) -> str:
            if var_name not in inv_map:
                inv_map[var_name] = add_node(
                    f"not_{var_name}",
                    "NOT",
                    f"NOT ({var_name}')",
                    1,
                    [f"in_{var_name}"],
                )
            return inv_map[var_name]

        if mode == "cmos":
            term_drivers: list[str] = []
            for t_idx, lits in enumerate(term_specs):
                lit_nets = [
                    f"in_{v}" if is_pos else get_inverted_input(v)
                    for v, is_pos in lits
                ]
                if len(lit_nets) == 1:
                    term_drivers.append(lit_nets[0])
                else:
                    alg = "".join(v if p else f"{v}'" for v, p in lits)
                    g_id = add_node(f"and_{t_idx}", "AND", f"AND ({alg})", 2, lit_nets)
                    term_drivers.append(g_id)

            if len(term_drivers) == 1:
                out_driver = term_drivers[0]
            else:
                out_driver = add_node("or_out", "OR", f"OR ({output_name})", 3, term_drivers)
        else:
            # Universal NAND-only mapping via De Morgan's Law
            if len(term_specs) == 1:
                lits = term_specs[0]
                lit_nets = [
                    f"in_{v}" if is_pos else get_inverted_input(v)
                    for v, is_pos in lits
                ]
                if len(lit_nets) == 1:
                    out_driver = lit_nets[0]
                else:
                    nand_mid = add_node("nand_0", "NAND", "NAND", 2, lit_nets)
                    out_driver = add_node("not_out", "NOT", "NOT (DeMorgan)", 3, [nand_mid])
            else:
                nand_stage_drivers: list[str] = []
                for t_idx, lits in enumerate(term_specs):
                    if len(lits) == 1:
                        v, is_pos = lits[0]
                        # De Morgan: single literal L enters output NAND as L'
                        nand_stage_drivers.append(get_inverted_input(v) if is_pos else f"in_{v}")
                    else:
                        lit_nets = [
                            f"in_{v}" if is_pos else get_inverted_input(v)
                            for v, is_pos in lits
                        ]
                        alg = "".join(v if p else f"{v}'" for v, p in lits)
                        nand_stage_drivers.append(
                            add_node(f"nand_{t_idx}", "NAND", f"NAND ({alg})", 2, lit_nets)
                        )
                out_driver = add_node("nand_out", "NAND", f"NAND ({output_name})", 3, nand_stage_drivers)

    out_node_id = add_node(f"out_{output_name}", "OUTPUT", output_name, 4, [out_driver])

    # Assign clean schematic coordinates by layer
    layer_x = {0: 85.0, 1: 280.0, 2: 530.0, 3: 790.0, 4: 995.0}
    layers: dict[int, list[str]] = collections.defaultdict(list)
    for nid in node_order:
        layers[nodes[nid].layer].append(nid)

    max_rows = max((len(v) for v in layers.values()), default=1)
    canvas_h = max(380.0, max_rows * 78.0 + 90.0)

    for lyr, nids in layers.items():
        cnt = len(nids)
        step_y = (canvas_h - 80.0) / max(1, cnt)
        for idx, nid in enumerate(nids):
            nodes[nid].x = layer_x.get(lyr, 500.0)
            nodes[nid].y = round(55.0 + step_y * (idx + 0.5), 1)

    # Backtrace worst-case Critical Path from output driver to primary input
    crit_nodes: set[str] = {out_node_id, out_driver}
    crit_edges: set[tuple[str, str]] = {(out_driver, out_node_id)}

    chain_ids: list[str] = [out_node_id]
    curr = out_driver
    chain_ids.append(curr)
    while nodes[curr].inputs:
        best_src = max(
            nodes[curr].inputs,
            key=lambda s: (nodes[s].arrival_ns, 1 if nodes[s].kind != "INPUT" else 0),
        )
        crit_edges.add((best_src, curr))
        crit_nodes.add(best_src)
        curr = best_src
        chain_ids.append(curr)

    chain_ids.reverse()
    for cid in crit_nodes:
        nodes[cid].is_critical = True

    # Build wire list
    wire_counter = 0
    for nid in node_order:
        nd = nodes[nid]
        pin_cnt = len(nd.inputs)
        for p_idx, src_id in enumerate(nd.inputs):
            wire_counter += 1
            wires.append(
                NetlistWire(
                    id=f"w_{wire_counter}",
                    source=src_id,
                    target=nid,
                    pin_index=p_idx,
                    pin_count=pin_cnt,
                    label=nodes[src_id].label,
                    arrival_ns=nodes[src_id].arrival_ns,
                    is_critical=(src_id, nid) in crit_edges,
                )
            )

    # Gate counts & PPA calculation
    gate_breakdown: dict[str, int] = {"NOT": 0, "AND": 0, "OR": 0, "NAND": 0}
    post_area = 0.0
    for nid in node_order:
        nd = nodes[nid]
        if nd.kind in gate_breakdown:
            gate_breakdown[nd.kind] += 1
            extra_pins = max(0, len(nd.inputs) - 2) if nd.kind in ("AND", "OR", "NAND") else 0
            post_area += GATE_AREA_GE[nd.kind] + 0.5 * extra_pins

    post_gates = sum(gate_breakdown.values())
    post_literals = sum(len(t) for t in term_specs)
    pre_gates, pre_area, pre_literals, pre_crit_ns = _estimate_canonical_pre_ppa(var_names, minterms)

    crit_ns = round(nodes[out_node_id].arrival_ns, 2)
    # Use 0.25ns pad-to-pad interconnect floor only if purely wire/const (0 gates) for Fmax display
    effective_period_ns = crit_ns if crit_ns > 0.0 else 0.5
    fmax_mhz = round(1000.0 / effective_period_ns, 2)

    gate_saving = (
        round((1.0 - post_gates / pre_gates) * 100.0, 1)
        if pre_gates > 0 and post_gates <= pre_gates
        else (0.0 if pre_gates == 0 else round((1.0 - post_gates / pre_gates) * 100.0, 1))
    )
    area_saving = (
        round((1.0 - post_area / pre_area) * 100.0, 1)
        if pre_area > 0
        else 0.0
    )

    crit_summary: list[str] = []
    for cid in chain_ids:
        nd = nodes[cid]
        if nd.kind in ("INPUT", "CONST"):
            crit_summary.append(f"{nd.label} (0.0ns)")
        elif nd.kind == "OUTPUT":
            crit_summary.append(f"{nd.label} ({nd.arrival_ns:.1f}ns)")
        else:
            crit_summary.append(f"{nd.kind}[+{nd.delay_ns:.1f}ns -> {nd.arrival_ns:.1f}ns]")

    ppa = PpaReport(
        tech_mode=mode.upper(),
        pre_gate_count=pre_gates,
        post_gate_count=post_gates,
        gate_saving_pct=gate_saving,
        pre_area_ge=pre_area,
        post_area_ge=round(post_area, 2),
        area_saving_pct=area_saving,
        pre_literals=pre_literals,
        post_literals=post_literals,
        pre_critical_ns=pre_crit_ns,
        critical_path_ns=crit_ns,
        fmax_mhz=fmax_mhz,
        gate_breakdown=gate_breakdown,
        critical_path_nodes=chain_ids,
        critical_path_summary=crit_summary,
    )

    return [nodes[nid] for nid in node_order], wires, ppa


# -----------------------------------------------------------------------------
# 4. Synthesizable Verilog-2001 (.v) & VCD Testbench (_tb.v) Codegen
# -----------------------------------------------------------------------------

def generate_verilog_2001(
    module_name: str,
    output_name: str,
    var_names: list[str],
    sop_expr: str,
    nodes: list[NetlistNode],
    ppa: PpaReport,
    minterms: set[int],
    dont_cares: set[int],
) -> tuple[str, str]:
    """Generates structural/RTL Verilog-2001 design (.v) and self-checking testbench (_tb.v)."""
    node_map = {n.id: n for n in nodes}

    def net_ident(nid: str) -> str:
        nd = node_map[nid]
        if nd.kind == "INPUT":
            return nd.label
        if nd.kind == "CONST":
            return nd.label
        if nd.kind == "OUTPUT":
            return output_name
        return f"w_{nid}"

    port_lines = [f"    input  wire {v}," for v in var_names]
    port_lines.append(f"    output wire {output_name}")

    internal_wires = [
        f"w_{n.id}"
        for n in nodes
        if n.kind in ("NOT", "AND", "OR", "NAND")
    ]

    prim_lines: list[str] = []
    out_node = next(n for n in nodes if n.kind == "OUTPUT")
    out_src_id = out_node.inputs[0]

    for n in nodes:
        if n.kind not in ("NOT", "AND", "OR", "NAND"):
            continue
        target_wire = output_name if n.id == out_src_id else f"w_{n.id}"
        in_wires = ", ".join(net_ident(src) for src in n.inputs)
        prim_kw = n.kind.lower()
        prim_lines.append(
            f"    {prim_kw:<4} #({n.delay_ns:.1f}) u_{n.id} ({target_wire}, {in_wires});"
        )

    # Filter out unused wire declaration for the gate driving output directly
    declared_wires = [w for w in internal_wires if w != f"w_{out_src_id}"]

    design_lines = [
        "// ============================================================================",
        "// Generated by Logic Synthesis Studio (Quine-McCluskey + Petrick's Method)",
        f"// Minimized SOP : {output_name} = {sop_expr}",
        f"// Technology    : {ppa.tech_mode} | Gates: {ppa.post_gate_count} (Saved {ppa.gate_saving_pct}%)",
        f"// Critical Path : {ppa.critical_path_ns:.2f} ns | Fmax: {ppa.fmax_mhz:.2f} MHz",
        "// ============================================================================",
        "`timescale 1ns / 100ps",
        "",
        f"module {module_name} (",
        "\n".join(port_lines),
        ");",
    ]

    if declared_wires:
        design_lines.append(f"    wire {', '.join(declared_wires)};")
        design_lines.append("")

    if prim_lines:
        design_lines.extend(prim_lines)
    else:
        # Pure wire or constant assignment
        design_lines.append(f"    assign {output_name} = {net_ident(out_src_id)};")

    design_lines.extend([
        "",
        "endmodule",
        "",
    ])
    design_v = "\n".join(design_lines)

    # Self-checking Verilog-2001 Testbench with $dumpfile & $dumpvars
    n_vars = len(var_names)
    total_vecs = 1 << n_vars
    concat_vars = ", ".join(var_names)
    dut_ports = ", ".join([f".{v}({v})" for v in var_names] + [f".{output_name}({output_name})"])
    settle_delay = max(2.0, round(ppa.critical_path_ns + 2.5, 1))
    hold_delay = max(2.0, round(20.0 - settle_delay, 1))

    tb_lines = [
        "// ============================================================================",
        f"// Self-Checking IEEE 1364 Testbench for {module_name}",
        "// Compatible with Icarus Verilog (iverilog + vvp) & ModelSim",
        "// ============================================================================",
        "`timescale 1ns / 100ps",
        "",
        f"module {module_name}_tb;",
        f"    reg  [{n_vars - 1}:0] stim_bus;",
        "    reg         bus_oe;",
        f"    wire        {', '.join(var_names)};",
        f"    wire        {output_name};",
        f"    wire        {output_name}_tri;",
        "    integer     i;",
        "    integer     errors;",
        "",
        f"    assign {{{concat_vars}}} = stim_bus;",
        f"    assign {output_name}_tri = bus_oe ? {output_name} : 1'bz;",
        "",
        f"    {module_name} dut (",
        f"        {dut_ports}",
        "    );",
        "",
        "    initial begin",
        f'        $dumpfile("{module_name}.vcd");',
        f"        $dumpvars(0, {module_name}_tb);",
        "        errors = 0;",
        "        bus_oe = 1'b0;",
        f"        stim_bus = {n_vars}'bx;",
        "        #10.0;",
        "        bus_oe = 1'b1;",
        "",
        f"        for (i = 0; i < {total_vecs}; i = i + 1) begin",
        f"            stim_bus = i[{n_vars - 1}:0];",
        f"            #{settle_delay:.1f};",
        f'            $display("t=%0t ns | stim=%b | {output_name}=%b", $time, stim_bus, {output_name});',
        f"            #{hold_delay:.1f};",
        "        end",
        "",
        "        bus_oe = 1'b0;",
        "        #10.0;",
        f'        $display("Simulation completed for {module_name} with %0d errors.", errors);',
        "        $finish;",
        "    end",
        "endmodule",
        "",
    ]
    tb_v = "\n".join(tb_lines)
    return design_v, tb_v


# -----------------------------------------------------------------------------
# 5. Gate-Delay VCD Waveform Simulator & IEEE 1364 Regex VCD Parser
# -----------------------------------------------------------------------------

def _eval_4val_not(a: str) -> str:
    if a == "0":
        return "1"
    if a == "1":
        return "0"
    return "x"


def _eval_4val_and(vals: list[str]) -> str:
    if "0" in vals:
        return "0"
    if all(v == "1" for v in vals):
        return "1"
    return "x"


def _eval_4val_or(vals: list[str]) -> str:
    if "1" in vals:
        return "1"
    if all(v == "0" for v in vals):
        return "0"
    return "x"


def _eval_4val_gate(kind: str, in_vals: list[str]) -> str:
    if kind == "NOT":
        return _eval_4val_not(in_vals[0])
    if kind == "AND":
        return _eval_4val_and(in_vals)
    if kind == "NAND":
        return _eval_4val_not(_eval_4val_and(in_vals))
    if kind == "OR":
        return _eval_4val_or(in_vals)
    return in_vals[0] if in_vals else "0"


def simulate_netlist_to_vcd(
    module_name: str,
    output_name: str,
    var_names: list[str],
    nodes: list[NetlistNode],
) -> str:
    """
    Runs a 4-valued ('0', '1', 'x', 'z') discrete-event gate simulation at 100ps
    resolution and emits a standards-compliant IEEE 1364 .vcd waveform file.
    """
    n_vars = len(var_names)
    num_vectors = min(1 << n_vars, 16)
    step_ticks = 200  # 20.0 ns per input vector (1 tick = 100 ps = 0.1 ns)
    boot_ticks = 100  # 10.0 ns power-on 'x'/'z' initialization phase

    # Assign compact ASCII VCD identifier codes ('!' .. '~')
    id_pool = [chr(c) for c in range(33, 126) if chr(c) not in ("$",)]
    sig_defs: list[tuple[str, int, str, str]] = []  # (key, width, vcd_id, ref_name)

    idx_gen = 0

    def alloc_sig(key: str, width: int, ref_name: str) -> str:
        nonlocal idx_gen
        code = id_pool[idx_gen % len(id_pool)]
        if idx_gen >= len(id_pool):
            code = id_pool[(idx_gen // len(id_pool)) - 1] + code
        idx_gen += 1
        sig_defs.append((key, width, code, ref_name))
        return code

    id_map: dict[str, str] = {}
    id_map["stim_bus"] = alloc_sig("stim_bus", max(2, n_vars), f"stim_bus[{max(2, n_vars) - 1}:0]")
    for v in var_names:
        id_map[f"in_{v}"] = alloc_sig(f"in_{v}", 1, v)

    gate_nodes = [n for n in nodes if n.kind in ("NOT", "AND", "OR", "NAND")]
    out_node = next(n for n in nodes if n.kind == "OUTPUT")
    out_driver_id = out_node.inputs[0]

    # Expose internal gates (up to 6 internal probes to keep VCD view crisp)
    exposed_gates = [g for g in gate_nodes if g.id != out_driver_id][:6]
    for g in exposed_gates:
        id_map[g.id] = alloc_sig(g.id, 1, f"u_{g.id}")

    id_map["out_main"] = alloc_sig("out_main", 1, output_name)
    id_map["out_tri"] = alloc_sig("out_tri", 1, f"{output_name}_tri")

    # Event queue: dict[tick, dict[node_key, new_val]]
    events: dict[int, dict[str, str]] = collections.defaultdict(dict)
    bus_width = max(2, n_vars)

    # t = 0: uninitialized 'x' startup and 'z' tri-state bus
    events[0]["stim_bus"] = "x" * bus_width
    events[0]["bus_oe"] = "0"
    for v in var_names:
        events[0][f"in_{v}"] = "x"

    # t = boot_ticks..: sweep input vectors
    for vec_i in range(num_vectors):
        t = boot_ticks + vec_i * step_ticks
        events[t]["bus_oe"] = "1"
        events[t]["stim_bus"] = format(vec_i, f"0{bus_width}b")
        for v_idx, v in enumerate(var_names):
            bit = (vec_i >> (n_vars - 1 - v_idx)) & 1
            events[t][f"in_{v}"] = "1" if bit else "0"

    end_vec_tick = boot_ticks + num_vectors * step_ticks
    events[end_vec_tick]["bus_oe"] = "0"
    max_sim_tick = end_vec_tick + 100

    # Current state of all netlist nodes
    state: dict[str, str] = {}
    for n in nodes:
        if n.kind == "CONST":
            state[n.id] = "1" if "1" in n.label else "0"
        else:
            state[n.id] = "x"
    state["stim_bus"] = "x" * bus_width
    state["bus_oe"] = "0"

    vcd_changes: dict[int, list[tuple[str, int, str]]] = collections.defaultdict(list)
    last_emitted: dict[str, str] = {}

    def record_sig(tick: int, sig_key: str, width: int, val: str) -> None:
        if sig_key not in id_map:
            return
        if last_emitted.get(sig_key) == val:
            return
        last_emitted[sig_key] = val
        vcd_changes[tick].append((id_map[sig_key], width, val))

    # Discrete-event simulation loop
    processed_ticks: set[int] = set()
    while True:
        pending = [t for t in events.keys() if t not in processed_ticks and t <= max_sim_tick]
        if not pending:
            break
        cur_t = min(pending)
        processed_ticks.add(cur_t)

        updates = events[cur_t]
        actually_changed: set[str] = set()
        for k, v in updates.items():
            if state.get(k) != v or cur_t == 0:
                state[k] = v
                actually_changed.add(k)

        # Schedule downstream gate evaluations only when an input net transitioned
        for g in gate_nodes:
            if any(src in actually_changed for src in g.inputs):
                in_vals = [state.get(src, "x") for src in g.inputs]
                target_val = _eval_4val_gate(g.kind, in_vals)
                delay_ticks = max(1, int(round(g.delay_ns * 10)))
                arrival_t = cur_t + delay_ticks
                if arrival_t <= max_sim_tick:
                    events[arrival_t][g.id] = target_val

        # Update primary output & tri-state buffer immediately from out_driver
        out_val = state.get(out_driver_id, "x")
        state["out_main"] = out_val
        tri_val = out_val if state.get("bus_oe") == "1" else "z"

        # Emit VCD changes at cur_t
        record_sig(cur_t, "stim_bus", bus_width, state["stim_bus"])
        for v in var_names:
            record_sig(cur_t, f"in_{v}", 1, state[f"in_{v}"])
        for g in exposed_gates:
            record_sig(cur_t, g.id, 1, state[g.id])
        record_sig(cur_t, "out_main", 1, out_val)
        record_sig(cur_t, "out_tri", 1, tri_val)

    # Assemble IEEE 1364 VCD text
    lines: list[str] = [
        "$date",
        "   Generated by Logic Synthesis Studio (WebAssembly Pyodide)",
        "$end",
        "$version",
        "   Logic Synthesis Studio IEEE-1364 VCD Simulator 1.0",
        "$end",
        "$timescale 100ps $end",
        f"$scope module {module_name}_tb $end",
    ]
    for _key, width, code, ref_name in sig_defs:
        vtype = "reg" if "stim" in ref_name else "wire"
        lines.append(f"$var {vtype} {width} {code} {ref_name} $end")
    lines.extend([
        "$upscope $end",
        "$enddefinitions $end",
    ])

    for tick in sorted(vcd_changes.keys()):
        lines.append(f"#{tick}")
        for code, width, val in vcd_changes[tick]:
            if width == 1:
                lines.append(f"{val}{code}")
            else:
                lines.append(f"b{val} {code}")
    lines.append(f"#{max_sim_tick}")
    return "\n".join(lines) + "\n"


# Regex patterns for IEEE 1364 VCD Parser
_VCD_TIMESCALE_RE = re.compile(r"\$timescale\s+(\d+)\s*(s|ms|us|ns|ps|fs)\s*\$end", re.IGNORECASE | re.DOTALL)
_VCD_SCOPE_PUSH_RE = re.compile(r"\$scope\s+\w+\s+(\S+)\s*\$end", re.IGNORECASE)
_VCD_SCOPE_POP_RE = re.compile(r"\$upscope\s*\$end", re.IGNORECASE)
_VCD_VAR_RE = re.compile(r"\$var\s+(\w+)\s+(\d+)\s+(\S+)\s+(.+?)\s*\$end", re.IGNORECASE | re.DOTALL)
_VCD_ENDDEF_RE = re.compile(r"\$enddefinitions\s*\$end", re.IGNORECASE)
_VCD_BLOCK_CMD_RE = re.compile(r"\$(?:comment|date|version)\b.*?\$end", re.IGNORECASE | re.DOTALL)
_VCD_DUMP_WRAP_RE = re.compile(r"\$(?:dumpvars|dumpon|dumpoff|dumpall|end)\b", re.IGNORECASE)
_VCD_TIME_TOKEN_RE = re.compile(r"^#(\d+)$")
_VCD_SCALAR_TOKEN_RE = re.compile(r"^([01xXzZ])(\S+)$")
_VCD_VECTOR_TOKEN_RE = re.compile(r"^[bB]([01xXzZ_]+)\s+(\S+)$")
_VCD_REAL_TOKEN_RE = re.compile(r"^[rR](\S+)\s+(\S+)$")


def _format_vector_state(raw_bits: str, width: int) -> tuple[str, str]:
    """
    Normalizes an IEEE 1364 binary vector into (kind, display_label)
    where kind in {'bus', 'x', 'z', '0', '1'}.
    """
    bits = raw_bits.replace("_", "").lower()
    if not bits:
        bits = "0"
    # IEEE 1364 left-extension rule
    if len(bits) < width:
        pad_char = bits[0] if bits[0] in ("x", "z") else "0"
        bits = pad_char * (width - len(bits)) + bits
    elif len(bits) > width:
        bits = bits[-width:]

    if width == 1:
        b = bits[0]
        return b, b.upper() if b in ("x", "z") else b

    if all(ch == "z" for ch in bits):
        return "z", "Hi-Z"
    if "x" in bits:
        return "x", "0x" + "X" * max(1, (width + 3) // 4)
    if "z" in bits:
        return "z", f"0b{bits}"

    hex_digits = max(1, (width + 3) // 4)
    int_val = int(bits, 2)
    return "bus", f"0x{int_val:0{hex_digits}X}"


def parse_vcd(vcd_content: str) -> dict[str, object]:
    """
    Regex-based IEEE 1364 Value Change Dump (.vcd) parser.
    Supports single-bit ('0', '1', 'x', 'z') and multi-bit ('b1010') buses
    from Icarus Verilog, ModelSim, Verilator, or the built-in gate simulator.
    """
    if not vcd_content or not vcd_content.strip():
        raise SynthesisError("Nội dung file .vcd trống.")

    ts_match = _VCD_TIMESCALE_RE.search(vcd_content)
    ts_mult = int(ts_match.group(1)) if ts_match else 1
    ts_unit = ts_match.group(2).lower() if ts_match else "ns"

    # Convert timescale to nanoseconds per tick for intuitive Δt display
    unit_to_ns = {
        "s": 1e9,
        "ms": 1e6,
        "us": 1e3,
        "ns": 1.0,
        "ps": 1e-3,
        "fs": 1e-6,
    }
    ns_per_tick = ts_mult * unit_to_ns.get(ts_unit, 1.0)

    enddef_match = _VCD_ENDDEF_RE.search(vcd_content)
    if not enddef_match:
        raise SynthesisError("File .vcd thiếu thẻ chuẩn '$enddefinitions $end'.")

    header_str = vcd_content[: enddef_match.start()]
    body_str = vcd_content[enddef_match.end() :]

    # Parse scopes & $var declarations in header order
    header_clean = _VCD_BLOCK_CMD_RE.sub(" ", header_str)
    token_iter = re.finditer(
        r"\$scope\s+\w+\s+(\S+)\s*\$end|\$upscope\s*\$end|\$var\s+(\w+)\s+(\d+)\s+(\S+)\s+(.+?)\s*\$end",
        header_clean,
        flags=re.IGNORECASE | re.DOTALL,
    )

    scope_stack: list[str] = []
    signals: list[dict[str, object]] = []
    code_to_sig_indices: dict[str, list[int]] = collections.defaultdict(list)

    for m in token_iter:
        if m.group(1) is not None:
            scope_stack.append(m.group(1))
        elif m.group(0).lower().startswith("$upscope"):
            if scope_stack:
                scope_stack.pop()
        else:
            var_type = m.group(2).lower()
            width = max(1, int(m.group(3)))
            code = m.group(4)
            ref_raw = " ".join(m.group(5).split())
            full_name = f"{'.'.join(scope_stack)}.{ref_raw}" if scope_stack else ref_raw
            sig_idx = len(signals)
            signals.append({
                "name": ref_raw,
                "full_name": full_name,
                "type": var_type,
                "width": width,
                "code": code,
                "transitions": [],
            })
            code_to_sig_indices[code].append(sig_idx)

    if not signals:
        raise SynthesisError("Không tìm thấy khai báo tín hiệu '$var' nào trong file .vcd.")

    # Clean comments & $dumpvars wrappers from body
    body_clean = _VCD_BLOCK_CMD_RE.sub("\n", body_str)
    body_clean = _VCD_DUMP_WRAP_RE.sub("\n", body_clean)

    current_tick = 0
    max_tick = 0

    for raw_line in body_clean.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Handle multiple tokens on one line when scalar/time tokens share a line
        t_match = _VCD_TIME_TOKEN_RE.match(line)
        if t_match:
            current_tick = int(t_match.group(1))
            if current_tick > max_tick:
                max_tick = current_tick
            continue

        vec_match = _VCD_VECTOR_TOKEN_RE.match(line)
        if vec_match:
            bits_raw, code = vec_match.group(1), vec_match.group(2)
            for s_idx in code_to_sig_indices.get(code, ()):
                sig = signals[s_idx]
                w = int(sig["width"])  # type: ignore[arg-type]
                kind, label = _format_vector_state(bits_raw, w)
                trans: list[dict[str, object]] = sig["transitions"]  # type: ignore[assignment]
                if trans and trans[-1]["tick"] == current_tick:
                    trans[-1] = {"tick": current_tick, "time_ns": round(current_tick * ns_per_tick, 4), "state": kind, "label": label}
                elif not trans or trans[-1]["label"] != label or trans[-1]["state"] != kind:
                    trans.append({
                        "tick": current_tick,
                        "time_ns": round(current_tick * ns_per_tick, 4),
                        "state": kind,
                        "label": label,
                    })
            continue

        sc_match = _VCD_SCALAR_TOKEN_RE.match(line)
        if sc_match:
            val_ch, code = sc_match.group(1).lower(), sc_match.group(2)
            for s_idx in code_to_sig_indices.get(code, ()):
                sig = signals[s_idx]
                w = int(sig["width"])  # type: ignore[arg-type]
                kind, label = _format_vector_state(val_ch, w)
                trans = sig["transitions"]  # type: ignore[assignment]
                if trans and trans[-1]["tick"] == current_tick:
                    trans[-1] = {"tick": current_tick, "time_ns": round(current_tick * ns_per_tick, 4), "state": kind, "label": label}
                elif not trans or trans[-1]["label"] != label or trans[-1]["state"] != kind:
                    trans.append({
                        "tick": current_tick,
                        "time_ns": round(current_tick * ns_per_tick, 4),
                        "state": kind,
                        "label": label,
                    })
            continue

        real_match = _VCD_REAL_TOKEN_RE.match(line)
        if real_match:
            r_val, code = real_match.group(1), real_match.group(2)
            for s_idx in code_to_sig_indices.get(code, ()):
                sig = signals[s_idx]
                trans = sig["transitions"]  # type: ignore[assignment]
                trans.append({
                    "tick": current_tick,
                    "time_ns": round(current_tick * ns_per_tick, 4),
                    "state": "bus",
                    "label": r_val,
                })

    if max_tick == 0:
        max_tick = max(
            (int(t["tick"]) for s in signals for t in s["transitions"]),  # type: ignore[union-attr]
            default=100,
        ) + 20

    end_time_ns = round(max_tick * ns_per_tick, 4)
    if end_time_ns <= 0:
        end_time_ns = 20.0

    return {
        "timescale": f"{ts_mult}{ts_unit}",
        "ns_per_tick": ns_per_tick,
        "max_tick": max_tick,
        "end_time_ns": end_time_ns,
        "signal_count": len(signals),
        "signals": signals,
    }


# -----------------------------------------------------------------------------
# 6. Unified JSON Endpoints for Pyodide WebAssembly Frontend
# -----------------------------------------------------------------------------

def _build_full_synthesis_payload(
    output_name: str,
    var_names: list[str],
    minterms: set[int],
    dont_cares: set[int],
    tech_mode: str = "cmos",
    module_name: str = "synth_logic",
) -> dict[str, object]:
    n_vars = len(var_names)
    sop_expr, pi_list, selected_tuples = quine_mccluskey_minimize(var_names, minterms, dont_cares)
    nodes, wires, ppa = build_gate_netlist_and_ppa(
        output_name=output_name,
        var_names=var_names,
        minterms=minterms,
        selected_implicants=selected_tuples,
        tech_mode=tech_mode,
    )
    verilog_v, verilog_tb = generate_verilog_2001(
        module_name=module_name,
        output_name=output_name,
        var_names=var_names,
        sop_expr=sop_expr,
        nodes=nodes,
        ppa=ppa,
        minterms=minterms,
        dont_cares=dont_cares,
    )
    vcd_raw = simulate_netlist_to_vcd(
        module_name=module_name,
        output_name=output_name,
        var_names=var_names,
        nodes=nodes,
    )
    vcd_parsed = parse_vcd(vcd_raw)

    truth_table: list[str] = []
    for idx in range(1 << n_vars):
        if idx in dont_cares:
            truth_table.append("X")
        elif idx in minterms:
            truth_table.append("1")
        else:
            truth_table.append("0")

    canonical_terms = [
        _format_implicant_algebraic(m, 0, var_names)
        for m in sorted(minterms)
    ]
    canonical_expr = " + ".join(canonical_terms) if canonical_terms else "0"

    return {
        "ok": True,
        "output_name": output_name,
        "module_name": module_name,
        "var_names": var_names,
        "minterms": sorted(minterms),
        "dont_cares": sorted(dont_cares),
        "truth_table": truth_table,
        "canonical_expr": canonical_expr,
        "minimized_expr": sop_expr,
        "prime_implicants": [dataclasses.asdict(pi) for pi in pi_list],
        "essential_pis": [pi.algebraic for pi in pi_list if pi.is_essential],
        "selected_pis": [pi.algebraic for pi in pi_list if pi.is_selected],
        "ppa": dataclasses.asdict(ppa),
        "schematic": {
            "nodes": [dataclasses.asdict(n) for n in nodes],
            "wires": [dataclasses.asdict(w) for w in wires],
        },
        "verilog_module": verilog_v,
        "verilog_tb": verilog_tb,
        "vcd_raw": vcd_raw,
        "waveform": vcd_parsed,
    }


def synthesize_expression_json(
    expr_or_minterms: str,
    dont_care_str: str = "",
    var_names_csv: str = "",
    tech_mode: str = "cmos",
    module_name: str = "synth_logic",
) -> str:
    """JSON endpoint: parses Boolean expression or minterm spec and runs full synthesis."""
    try:
        out_name, var_names, minterms, dont_cares = parse_specification(
            expr_or_minterms=expr_or_minterms,
            dont_care_str=dont_care_str,
            var_names_csv=var_names_csv,
        )
        mod_clean = re.sub(r"[^A-Za-z0-9_]", "_", module_name.strip()) or "synth_logic"
        if mod_clean[0].isdigit():
            mod_clean = "m_" + mod_clean
        payload = _build_full_synthesis_payload(
            output_name=out_name,
            var_names=var_names,
            minterms=minterms,
            dont_cares=dont_cares,
            tech_mode=tech_mode,
            module_name=mod_clean,
        )
        return json.dumps(payload)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def synthesize_truth_table_json(
    var_names_json: str,
    states_json: str,
    output_name: str = "F",
    tech_mode: str = "cmos",
    module_name: str = "synth_logic",
) -> str:
    """JSON endpoint: synthesizes directly from interactive Truth Table ('0', '1', 'X')."""
    try:
        var_names = [str(v).strip() for v in json.loads(var_names_json) if str(v).strip()]
        states = [str(s).strip().upper() for s in json.loads(states_json)]
        if not 1 <= len(var_names) <= 8:
            raise SynthesisError("Bảng chân trị hỗ trợ từ 1 đến 8 biến.")
        expected = 1 << len(var_names)
        if len(states) != expected:
            raise SynthesisError(f"Bảng chân trị cần đúng {expected} dòng cho {len(var_names)} biến.")

        minterms: set[int] = set()
        dont_cares: set[int] = set()
        for idx, st in enumerate(states):
            if st == "1":
                minterms.add(idx)
            elif st == "X":
                dont_cares.add(idx)

        out_clean = re.sub(r"[^A-Za-z0-9_]", "_", output_name.strip()) or "F"
        mod_clean = re.sub(r"[^A-Za-z0-9_]", "_", module_name.strip()) or "synth_logic"
        payload = _build_full_synthesis_payload(
            output_name=out_clean,
            var_names=var_names,
            minterms=minterms,
            dont_cares=dont_cares,
            tech_mode=tech_mode,
            module_name=mod_clean,
        )
        return json.dumps(payload)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def parse_vcd_json(vcd_content: str) -> str:
    """JSON endpoint: parses uploaded IEEE 1364 .vcd file for GTKWave viewer."""
    try:
        parsed = parse_vcd(vcd_content)
        return json.dumps({"ok": True, "waveform": parsed})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})
