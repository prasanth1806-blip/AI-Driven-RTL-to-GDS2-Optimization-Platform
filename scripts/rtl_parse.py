#!/usr/bin/env python3
"""Parse (System)Verilog source for its module list and port declarations.

Everything downstream of an upload needs to know the shape of the design:
the testbench generator needs every port's direction and width, the SDC
generator needs to know which input is the clock, and the flow needs to know
which module is the top so Yosys and OpenROAD are pointed at the right thing.
Before this existed each of those guessed independently, and the testbench
generator did not guess at all -- it emitted a fixed clk/rst/data_in/data_out
harness that only ever matched the built-in template design.

This is a pragmatic regex/scanner-based parser, not a full elaborator. It
covers what real RTL in this flow actually uses:

  * ANSI headers      module m #(parameter W=8) (input logic [W-1:0] d, ...);
  * non-ANSI headers  module m (a, b); input a; output [7:0] b;
  * parameter widths, including $clog2 and arithmetic on other parameters
  * `ifdef / comment / string noise, stripped before scanning
  * multiple modules per file, with the top inferred from instantiations

Used as a library (parse_file / parse_sources) and as a CLI that prints the
parse as JSON, which is how the Makefile and the Tcl scripts consume it.

Usage: rtl_parse.py <file.sv> [more.sv ...] [--top NAME]
"""
import glob
import json
import os
import re
import sys

# Port names treated as a clock, most specific first, matched case-insensitively.
CLOCK_NAMES = ("clk", "clock", "clk_i", "i_clk", "clk_in", "sysclk",
               "aclk", "hclk", "pclk", "ck")

# Port names treated as a reset. Active-low spellings are listed so the
# generated testbench and SDC can tell 1-is-reset from 0-is-reset.
RESET_NAMES = ("rst", "reset", "rst_n", "resetn", "reset_n", "rstn", "rst_i",
               "i_rst", "arst", "arst_n", "areset", "aresetn", "nrst",
               "rst_ni", "presetn", "hresetn")

ACTIVE_LOW_RESETS = ("rst_n", "resetn", "reset_n", "rstn", "nrst", "arst_n",
                     "aresetn", "rst_ni", "presetn", "hresetn")

# Verilog keywords that may appear between a direction and the identifier.
_TYPE_KEYWORDS = {
    "wire", "reg", "logic", "bit", "signed", "unsigned", "var",
    "byte", "shortint", "int", "longint", "integer", "time",
    "real", "shortreal", "realtime", "supply0", "supply1",
    "tri", "triand", "trior", "tri0", "tri1", "wand", "wor",
}

_DIRECTIONS = ("input", "output", "inout")


# ---------------------------------------------------------------------------
# Lexical cleanup
# ---------------------------------------------------------------------------

def strip_noise(text):
    """Remove comments and string literals.

    Done before any structural regex runs: a `//` comment containing an
    unbalanced parenthesis, or a $display string containing the word
    "module", is otherwise enough to derail the whole parse.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    # Keep the quotes' worth of whitespace so token boundaries survive.
    text = re.sub(r'"(?:[^"\\\n]|\\.)*"', ' "" ', text)
    return text


# ---------------------------------------------------------------------------
# Constant expression evaluation (parameters, $clog2, arithmetic)
# ---------------------------------------------------------------------------

_SAFE_EXPR_RE = re.compile(r"^[\d\s+\-*/%()<>|&^~,:!=clog2]*$")


def _clog2(value):
    value = int(value)
    return max(1, (value - 1).bit_length()) if value > 1 else (1 if value > 0 else 0)


def _normalize_literals(expr):
    """Rewrite Verilog sized literals into plain decimal integers."""
    expr = re.sub(r"(\d+)?'s?[dD]([0-9_]+)",
                  lambda m: str(int(m.group(2).replace("_", ""))), expr)
    expr = re.sub(r"(\d+)?'s?[hH]([0-9a-fA-F_]+)",
                  lambda m: str(int(m.group(2).replace("_", ""), 16)), expr)
    expr = re.sub(r"(\d+)?'s?[bB]([01_]+)",
                  lambda m: str(int(m.group(2).replace("_", ""), 2)), expr)
    expr = re.sub(r"(\d+)?'s?[oO]([0-7_]+)",
                  lambda m: str(int(m.group(2).replace("_", ""), 8)), expr)
    return expr.replace("_", "")


def eval_const(expr, params, _depth=0):
    """Evaluate a width/parameter expression to an int, or return None.

    None means "this width is not statically known here" -- an unresolved
    parameter from a package, or a construct this parser does not model. Every
    caller has to cope with that rather than inventing a number, because a
    wrong width silently produces a testbench that drives the wrong bits.
    """
    if expr is None or _depth > 8:
        return None
    expr = str(expr).strip()
    if not expr:
        return None

    # Substitute parameters by name, recursively -- parameters routinely
    # depend on other parameters (localparam AW = $clog2(DEPTH)).
    def substitute(match):
        name = match.group(0)
        if name == "clog2":
            return name
        if name in params:
            resolved = eval_const(params[name], params, _depth + 1)
            return f"({resolved})" if resolved is not None else name
        return name

    expr = expr.replace("$clog2", "clog2").replace("$bits", "clog2")
    expr = re.sub(r"[A-Za-z_]\w*", substitute, expr)
    expr = _normalize_literals(expr)

    if not _SAFE_EXPR_RE.match(expr):
        return None
    try:
        value = eval(expr, {"__builtins__": {}}, {"clog2": _clog2})  # noqa: S307
    except Exception:
        return None
    return int(value) if isinstance(value, (int, float)) else None


def _read_value(text, start):
    """Read a constant expression starting at `start`, stopping at the comma,
    semicolon or closing paren that ends it at nesting depth zero.

    Scanning rather than matching `[^,;)]+`: a default of `$clog2(DEPTH)` or
    `{4{1'b0}}` contains the very characters that terminate a declaration, so
    a non-greedy regex truncates it mid-expression and the width silently
    comes out unknown.
    """
    depth, index = 0, start
    while index < len(text):
        char = text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif char in ",;" and depth == 0:
            break
        index += 1
    return text[start:index].strip()


_PARAM_RE = re.compile(
    r"\b(?:parameter|localparam)\b"
    r"(?:\s+(?:signed|unsigned|int|integer|logic|bit|reg|byte|shortint|longint|time|real))*"
    r"(?:\s*\[[^\]]*\])?"
    r"\s*(\w+)\s*="
)


def collect_params(text):
    """Return {name: expression} for every `parameter`/`localparam` statement."""
    params = {}
    for match in _PARAM_RE.finditer(text):
        value = _read_value(text, match.end())
        if value:
            params.setdefault(match.group(1), value)
    return params


def parse_param_list(param_text):
    """Return {name: expression} for a module header's `#(...)` list.

    Entries after the first often omit the `parameter` keyword
    (`#(parameter W = 8, D = 16)`), so the list is split positionally instead
    of scanned for the keyword.
    """
    params = {}
    for entry in _split_top_level(param_text):
        if "=" not in entry:
            continue
        lhs, _, rhs = entry.partition("=")
        lhs = re.sub(r"\[[^\]]*\]", " ", lhs)
        lhs = re.sub(r"\b(?:parameter|localparam|type)\b", " ", lhs)
        name = _identifier(lhs)
        value = _read_value(rhs, 0)
        if name and value:
            params.setdefault(name, value)
    return params


# ---------------------------------------------------------------------------
# Port scanning
# ---------------------------------------------------------------------------

def _split_top_level(text, separator=","):
    """Split on `separator`, ignoring separators nested in (), [] or {}.

    A port list entry like `input [WIDTH-1:0][3:0] d` or a parameter default
    of `{4{1'b0}}` contains commas and brackets that must not be treated as
    entry boundaries.
    """
    parts, depth, current = [], 0, []
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == separator and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _parse_range(decl):
    """Pull the packed range off a declaration, returning (msb, lsb, rest)."""
    ranges = re.findall(r"\[([^\[\]]*)\]", decl)
    rest = re.sub(r"\[[^\[\]]*\]", " ", decl)
    if not ranges:
        return None, None, rest
    # The first packed range is the bit width; further ranges are array
    # dimensions, which the testbench treats as separate elements.
    bounds = _split_top_level(ranges[0], ":")
    if len(bounds) == 2:
        return bounds[0].strip(), bounds[1].strip(), rest
    return bounds[0].strip(), "0", rest


def _identifier(decl):
    tokens = [t for t in re.split(r"[\s]+", decl.strip()) if t]
    tokens = [t for t in tokens
              if t not in _TYPE_KEYWORDS and t not in _DIRECTIONS]
    # Drop an interface/struct type name preceding the identifier.
    return tokens[-1] if tokens else None


def _make_port(name, direction, msb, lsb, params):
    high = eval_const(msb, params)
    low = eval_const(lsb, params)
    width = None
    if high is not None and low is not None:
        width = abs(high - low) + 1
    elif msb is None and lsb is None:
        width = 1
    lowered = name.lower()
    return {
        "name": name,
        "direction": direction,
        "msb": msb,
        "lsb": lsb,
        "width": width,
        "vector": msb is not None,
        "is_clock": lowered in CLOCK_NAMES,
        "is_reset": lowered in RESET_NAMES,
        "active_low": lowered in ACTIVE_LOW_RESETS,
    }


def _parse_ansi_ports(port_text, params):
    """Ports declared inside the module header, with directions inline."""
    ports, direction, msb, lsb = [], None, None, None
    for entry in _split_top_level(port_text):
        entry = entry.strip()
        if not entry:
            continue
        leading = re.match(r"\b(input|output|inout)\b", entry)
        if leading:
            direction = leading.group(1)
            # A new direction resets the inherited range.
            msb, lsb, entry = _parse_range(entry[leading.end():])
        else:
            # `input [7:0] a, b` -- b inherits direction and range from a.
            new_msb, new_lsb, entry = _parse_range(entry)
            if new_msb is not None:
                msb, lsb = new_msb, new_lsb
        if direction is None:
            return []  # non-ANSI header: bare identifier list
        # Strip an initialiser (`output logic q = 1'b0`).
        entry = entry.split("=")[0]
        name = _identifier(entry)
        if name:
            ports.append(_make_port(name, direction, msb, lsb, params))
    return ports


def _parse_non_ansi_ports(name_list, body, params):
    """Ports named in the header and declared as statements in the body."""
    declared = {}
    pattern = re.compile(
        r"\b(input|output|inout)\b"
        r"((?:\s*(?:wire|reg|logic|bit|signed|unsigned|var))*)"
        r"((?:\s*\[[^\]]*\])*)"
        r"\s*([\w\s,]+?)\s*;"
    )
    for direction, _types, ranges, names in pattern.findall(body):
        msb, lsb, _ = _parse_range(ranges)
        for raw in names.split(","):
            ident = raw.strip()
            if ident:
                declared[ident] = (direction, msb, lsb)

    ports = []
    for raw in _split_top_level(name_list):
        ident = _identifier(raw)
        if not ident:
            continue
        direction, msb, lsb = declared.get(ident, ("input", None, None))
        ports.append(_make_port(ident, direction, msb, lsb, params))
    return ports


# ---------------------------------------------------------------------------
# Module scanning
# ---------------------------------------------------------------------------

_MODULE_START_RE = re.compile(r"\bmodule\s+(\w+)")


def _match_balanced(text, start):
    """Consume a balanced bracket group beginning at/after `start`.

    Returns (inner_text, index_after_group), or (None, start) when there is
    no group there. Used for a module's `#(...)` parameter list and `(...)`
    port list, both of which nest -- `$clog2(DEPTH)`, `[W-1:0]`, `'{0}` --
    so they cannot be delimited by a regex.
    """
    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != "(":
        return None, start

    depth, scan = 0, index
    while scan < len(text):
        char = text[scan]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[index + 1:scan], scan + 1
        scan += 1
    return None, start


def _skip_space(text, index):
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def scan_modules(clean):
    """Yield (name, param_text, port_text, body_start) for each module."""
    found = []
    for match in _MODULE_START_RE.finditer(clean):
        name = match.group(1)
        position = _skip_space(clean, match.end())

        param_text = ""
        if position < len(clean) and clean[position] == "#":
            inner, after = _match_balanced(clean, position + 1)
            if inner is not None:
                param_text, position = inner, after

        port_text, after = _match_balanced(clean, position)
        if port_text is not None:
            position = after

        position = _skip_space(clean, position)
        if position < len(clean) and clean[position] == ";":
            position += 1

        found.append((name, param_text, port_text, position))
    return found


def parse_text(text, path=None):
    """Return the list of modules declared in one source text."""
    clean = strip_noise(text)
    modules = []

    for name, param_text, port_text, body_start in scan_modules(clean):
        body_end = clean.find("endmodule", body_start)
        body = clean[body_start:body_end if body_end != -1 else len(clean)]

        params = parse_param_list(param_text)
        for key, value in collect_params(param_text).items():
            params.setdefault(key, value)
        for key, value in collect_params(body).items():
            params.setdefault(key, value)

        port_text = port_text or ""
        ports = _parse_ansi_ports(port_text, params)
        if not ports and port_text.strip():
            ports = _parse_non_ansi_ports(port_text, body, params)

        modules.append({
            "name": name,
            "file": path,
            "params": params,
            "ports": ports,
            "instantiates": sorted(find_instantiations(body)),
            "has_always": bool(re.search(r"\balways(_ff|_comb|_latch)?\b", body)),
            "has_memory": bool(re.search(r"\b(?:reg|logic|bit)\b\s*(?:\[[^\]]*\]\s*)?"
                                        r"\w+\s*\[[^\]]*\]\s*;", body)),
        })
    return modules


# Words that look like an instantiation but are language constructs.
_NOT_INSTANCES = {
    "if", "else", "case", "casex", "casez", "for", "while", "repeat",
    "begin", "end", "assign", "always", "always_ff", "always_comb",
    "always_latch", "initial", "final", "generate", "endgenerate",
    "module", "endmodule", "function", "endfunction", "task", "endtask",
    "posedge", "negedge", "wire", "reg", "logic", "input", "output",
    "inout", "parameter", "localparam", "typedef", "struct", "union",
    "enum", "return", "signed", "unsigned", "and", "or", "not", "nand",
    "nor", "xor", "xnor", "buf", "bufif0", "bufif1", "notif0", "notif1",
    "pullup", "pulldown", "tran", "genvar", "int", "integer", "real",
    "bit", "byte", "static", "automatic", "const", "import", "export",
}

# An instantiation with a parameter override: `ram_core #(.W(8)) u_ram (...)`.
_INST_PARAM_RE = re.compile(r"\b([A-Za-z_]\w*)\s*#\s*\(")

# A plain instantiation: `ram_core u_ram (...)`, optionally an instance array.
#
# The whitespace between the two identifiers is required, not optional. With
# `\s*` there, the pattern also matches *inside* a single identifier -- a port
# connection `.clk(clk)` splits into module "cl" plus instance "k" -- which
# reported phantom submodules and, through them, a phantom top module.
_INST_PLAIN_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)?\("
)


def find_instantiations(body):
    """Module names instantiated inside a body."""
    found = set()
    for module_name in _INST_PARAM_RE.findall(body):
        if module_name not in _NOT_INSTANCES:
            found.add(module_name)
    for module_name, instance_name in _INST_PLAIN_RE.findall(body):
        if module_name in _NOT_INSTANCES or instance_name in _NOT_INSTANCES:
            continue
        found.add(module_name)
    return found


def parse_sources(paths, top=None):
    """Parse several sources and identify the top module.

    The top is the module nothing else instantiates. When more than one
    module qualifies (a file with several independent blocks), the one
    declared in the file listed first wins, which matches the flow's
    convention of naming the design after its own file.
    """
    modules = []
    for path in paths:
        with open(path, errors="replace") as handle:
            modules.extend(parse_text(handle.read(), path))

    by_name = {module["name"]: module for module in modules}

    if top and top in by_name:
        chosen = by_name[top]
    else:
        instantiated = set()
        for module in modules:
            instantiated.update(module["instantiates"])
        candidates = [m for m in modules if m["name"] not in instantiated]
        if not candidates:
            candidates = modules
        chosen = None
        if top:
            # An explicit top that was not found is worth reporting rather
            # than silently substituting a different module.
            chosen = next((m for m in candidates if m["name"] == top), None)
        if chosen is None:
            chosen = candidates[0] if candidates else None

    return {
        "modules": modules,
        "top": chosen["name"] if chosen else None,
        "top_module": chosen,
        "requested_top": top,
        "top_found": bool(top and top in by_name) if top else None,
    }


# ---------------------------------------------------------------------------
# Convenience accessors used by the generators
# ---------------------------------------------------------------------------

def ports_by_direction(module, direction):
    return [p for p in module["ports"] if p["direction"] == direction]


def find_clock(module):
    for port in ports_by_direction(module, "input"):
        if port["is_clock"]:
            return port
    return None


def find_reset(module):
    for port in ports_by_direction(module, "input"):
        if port["is_reset"]:
            return port
    return None


def data_inputs(module):
    """Input ports that are neither clock nor reset."""
    return [p for p in ports_by_direction(module, "input")
            if not p["is_clock"] and not p["is_reset"]]


def parse_file(path, top=None):
    """Parse one file and return its top module dict (or None)."""
    return parse_sources([path], top=top)["top_module"]


def main():
    args = [a for a in sys.argv[1:]]
    top = None
    if "--top" in args:
        index = args.index("--top")
        top = args[index + 1] if index + 1 < len(args) else None
        del args[index:index + 2]

    paths = []
    for pattern in args:
        paths.extend(sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[")
                     else [pattern])
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        sys.exit("usage: rtl_parse.py <file.sv> [more.sv ...] [--top NAME]")

    result = parse_sources(paths, top=top)
    result.pop("top_module", None)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
