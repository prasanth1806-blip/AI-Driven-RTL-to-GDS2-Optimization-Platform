#!/usr/bin/env python3
"""Render a VCD to a timing-diagram PNG, headless.

The Makefile used to do this with `gtkwave -T -o <png> <vcd>`, which cannot
work: gtkwave's -o is --optimize (recode VCD to FST) and -T takes a Tcl init
*file*, so the PNG path was consumed as -T's argument and the .vcd became the
save file. gtkwave has no CLI image export at all. The failure was masked
because the caller treated the waveform PNG as best-effort, so `make wave`
reported an error nobody chased and no PNG was ever produced.

Drawing it here instead removes the whole class of problem: no X display, no
GTK, and none of the Snap module-path breakage that makes gtkwave fragile
when launched from an IDE terminal. gtkwave is still the right tool for
*interactive* browsing, which is what the dashboard's "Open in GTKWave"
button does.

Buses are drawn as value-annotated bands and x/z is drawn as a hatched band,
so an undefined signal is visible as undefined rather than as a flat line at
zero.

Usage:
  vcd_png.py <input.vcd> <output.png> [--max-signals N] [--scope NAME]
                                      [--width IN] [--dpi N]
"""
import argparse
import re
import sys

TIME_UNITS = {"s": 1e0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9,
              "ps": 1e-12, "fs": 1e-15}

# Drawing geometry, in row-relative units.
ROW_LOW, ROW_HIGH = 0.22, 0.78

COLOR_LINE = "#1f6feb"
COLOR_BUS = "#8250df"
COLOR_UNDEF = "#d1242f"
COLOR_GRID = "#e6e8eb"
COLOR_TEXT = "#1c2430"

# A very long simulation produces more transitions than a PNG can show. Both
# caps exist so a pathological VCD degrades into a readable picture instead of
# a black smear or an out-of-memory renderer.
MAX_CHANGES = 400_000
MAX_SEGMENTS_PER_SIGNAL = 4000


# ---------------------------------------------------------------------------
# VCD parsing
# ---------------------------------------------------------------------------

def parse_vcd(path):
    """Parse a VCD into signal declarations and per-signal value changes.

    Returns (signals, changes, end_time, timescale) where `signals` is a list
    of dicts in declaration order and `changes` maps an identifier code to a
    list of (time, value_string).
    """
    signals = []
    by_code = {}
    changes = {}
    scope = []
    timescale = (1.0, "ns")
    time = 0
    end_time = 0
    in_header = True
    total_changes = 0
    truncated = False

    with open(path, errors="replace") as handle:
        tokens = _tokenize(handle)
        pending = None
        for token in tokens:
            if in_header:
                if token == "$var":
                    parts = _collect(tokens)
                    # $var wire 8 ! dout [7:0] $end
                    if len(parts) >= 4:
                        kind, width, code, name = parts[0], parts[1], parts[2], parts[3]
                        bits = parts[4] if len(parts) > 4 else ""
                        try:
                            width = int(width)
                        except ValueError:
                            width = 1
                        if code not in by_code:
                            entry = {
                                "code": code, "name": name, "kind": kind,
                                "width": width, "scope": ".".join(scope),
                                "bits": bits,
                            }
                            signals.append(entry)
                            by_code[code] = entry
                            changes[code] = []
                elif token == "$scope":
                    parts = _collect(tokens)
                    if len(parts) >= 2:
                        scope.append(parts[1])
                elif token == "$upscope":
                    _collect(tokens)
                    if scope:
                        scope.pop()
                elif token == "$timescale":
                    parts = _collect(tokens)
                    timescale = _parse_timescale(" ".join(parts))
                elif token == "$enddefinitions":
                    _collect(tokens)
                    in_header = False
                elif token.startswith("$"):
                    _collect(tokens)
                continue

            # ---- value change section ----
            if token.startswith("#"):
                try:
                    time = int(token[1:])
                except ValueError:
                    continue
                end_time = max(end_time, time)
            elif token[0] in "01xXzZ" and len(token) > 1:
                value, code = token[0], token[1:]
                if code in changes:
                    _record(changes, code, time, value)
                    total_changes += 1
            elif token[0] in "bB":
                pending = token[1:]
            elif token[0] in "rR":
                pending = token[1:]
            elif pending is not None:
                if token in changes:
                    _record(changes, token, time, pending)
                    total_changes += 1
                pending = None
            elif token.startswith("$"):
                _collect(tokens)

            if total_changes > MAX_CHANGES:
                truncated = True
                break

    return {
        "signals": signals,
        "changes": changes,
        "end_time": end_time or 1,
        "timescale": timescale,
        "truncated": truncated,
    }


def _tokenize(handle):
    for line in handle:
        for token in line.split():
            yield token


def _collect(tokens):
    """Consume tokens up to and including $end, returning the ones before it."""
    parts = []
    for token in tokens:
        if token == "$end":
            break
        parts.append(token)
    return parts


def _record(changes, code, time, value):
    series = changes[code]
    if series and series[-1][0] == time:
        series[-1] = (time, value)   # last write at a timestamp wins
    else:
        series.append((time, value))


def _parse_timescale(text):
    match = re.match(r"\s*(\d+)\s*([munpf]?s)", text)
    if not match:
        return 1.0, "ns"
    return float(match.group(1)), match.group(2)


# ---------------------------------------------------------------------------
# Signal selection
# ---------------------------------------------------------------------------

def choose_signals(parsed, limit, scope_filter=None):
    """Pick which signals to draw, shallowest scope first.

    A testbench dumps its own registers, the DUT's ports and every internal
    net below them. The interesting ones are nearly always the shallowest --
    the harness signals and the DUT interface -- so depth is the primary sort
    key, and constant signals are dropped before the cap bites.
    """
    signals = parsed["signals"]
    if scope_filter:
        wanted = scope_filter.lower()
        signals = [s for s in signals if wanted in (s["scope"] or "").lower()]

    changing, constant = [], []
    for signal in signals:
        (changing if len(parsed["changes"].get(signal["code"], [])) > 1
         else constant).append(signal)

    changing.sort(key=lambda s: ((s["scope"] or "").count("."), s["scope"], s["name"]))
    constant.sort(key=lambda s: ((s["scope"] or "").count("."), s["scope"], s["name"]))

    chosen = changing[:limit]
    if len(chosen) < limit:
        chosen += constant[:limit - len(chosen)]
    return chosen, len(signals) - len(chosen)


def segments_for(parsed, signal, end_time):
    """Value changes turned into [(t_start, t_end, value)] spans."""
    series = parsed["changes"].get(signal["code"], [])
    if not series:
        return [(0, end_time, "x")]

    spans = []
    if series[0][0] > 0:
        spans.append((0, series[0][0], "x"))
    for index, (time, value) in enumerate(series):
        stop = series[index + 1][0] if index + 1 < len(series) else end_time
        if stop > time:
            spans.append((time, stop, value))
    if len(spans) > MAX_SEGMENTS_PER_SIGNAL:
        spans = spans[:MAX_SEGMENTS_PER_SIGNAL]
    return spans


def is_undefined(value):
    return any(char in "xXzZ" for char in value)


def format_value(value, width):
    """Render a bus value as hex, keeping x/z visible."""
    if is_undefined(value):
        return value.upper()[:12]
    try:
        number = int(value, 2)
    except ValueError:
        return value[:12]
    digits = max(1, (width + 3) // 4)
    return f"{number:0{digits}X}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(parsed, chosen, hidden, out_path, width_in, dpi, title):
    import matplotlib
    matplotlib.use("Agg")   # no display; must be set before pyplot
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    end_time = parsed["end_time"]
    rows = len(chosen)
    height_in = max(2.0, 0.36 * rows + 1.1)

    figure, axes = plt.subplots(figsize=(width_in, height_in), dpi=dpi)

    for index, signal in enumerate(chosen):
        base = rows - index - 1          # first chosen signal at the top
        spans = segments_for(parsed, signal, end_time)
        bus = signal["width"] > 1

        axes.axhline(base, color=COLOR_GRID, lw=0.6, zorder=0)

        previous_level = None
        for start, stop, value in spans:
            if is_undefined(value):
                axes.add_patch(Rectangle(
                    (start, base + ROW_LOW), stop - start, ROW_HIGH - ROW_LOW,
                    facecolor=COLOR_UNDEF, alpha=0.16, edgecolor=COLOR_UNDEF,
                    lw=0.7, hatch="///", zorder=2))
                previous_level = None
                continue

            if bus:
                axes.add_patch(Rectangle(
                    (start, base + ROW_LOW), stop - start, ROW_HIGH - ROW_LOW,
                    facecolor="none", edgecolor=COLOR_BUS, lw=1.1, zorder=2))
                label = format_value(value, signal["width"])
                # Only label a span wide enough to hold the text, or the
                # picture turns into overlapping ink.
                if (stop - start) > end_time * 0.018 * max(1, len(label)) / 2:
                    axes.text((start + stop) / 2, base + 0.5, label,
                              ha="center", va="center", fontsize=6.5,
                              color=COLOR_BUS, zorder=3)
            else:
                level = base + (ROW_HIGH if value in "1" else ROW_LOW)
                axes.plot([start, stop], [level, level],
                          color=COLOR_LINE, lw=1.3, solid_capstyle="butt",
                          zorder=2)
                if previous_level is not None and previous_level != level:
                    axes.plot([start, start], [previous_level, level],
                              color=COLOR_LINE, lw=1.3, zorder=2)
                previous_level = level

    unit = parsed["timescale"][1]
    axes.set_xlim(0, end_time)
    axes.set_ylim(-0.15, rows)
    axes.set_yticks([rows - i - 1 + 0.5 for i in range(rows)])
    axes.set_yticklabels(
        [_label(signal) for signal in chosen], fontsize=7, color=COLOR_TEXT)
    axes.set_xlabel(f"time ({unit})", fontsize=8, color=COLOR_TEXT)
    axes.tick_params(axis="x", labelsize=7, colors=COLOR_TEXT)
    axes.grid(axis="x", color=COLOR_GRID, lw=0.6)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(COLOR_GRID)

    notes = []
    if hidden > 0:
        notes.append(f"{hidden} more signal(s) not shown")
    if parsed["truncated"]:
        notes.append("VCD truncated for rendering")
    subtitle = "  ·  ".join(notes)
    axes.set_title(title + (f"\n{subtitle}" if subtitle else ""),
                   fontsize=9.5, color=COLOR_TEXT, loc="left")

    figure.tight_layout()
    figure.savefig(out_path, facecolor="white")
    plt.close(figure)


def _label(signal):
    name = signal["name"]
    if signal["width"] > 1:
        name = f"{name}[{signal['width'] - 1}:0]"
    scope = signal["scope"] or ""
    # Keep only the innermost scope: the full path is usually too wide to fit
    # and the tail is the part that distinguishes one signal from another.
    if scope:
        name = f"{scope.split('.')[-1]}.{name}"
    return name[:38]


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vcd")
    parser.add_argument("png")
    parser.add_argument("--max-signals", type=int, default=24)
    parser.add_argument("--scope", default=None)
    parser.add_argument("--width", type=float, default=12.0)
    parser.add_argument("--dpi", type=int, default=140)
    args = parser.parse_args()

    parsed = parse_vcd(args.vcd)
    if not parsed["signals"]:
        sys.exit(f"{args.vcd}: no signals declared -- nothing to draw. "
                 f"The testbench needs a $dumpvars call.")

    chosen, hidden = choose_signals(parsed, args.max_signals, args.scope)
    if not chosen:
        sys.exit(f"{args.vcd}: no signals matched"
                 + (f" scope '{args.scope}'" if args.scope else ""))

    import os
    title = os.path.basename(args.vcd)
    render(parsed, chosen, hidden, args.png, args.width, args.dpi, title)

    print(f"Wrote {args.png}: {len(chosen)} signal(s), "
          f"0-{parsed['end_time']} {parsed['timescale'][1]}"
          + (f", {hidden} not shown" if hidden else ""))


if __name__ == "__main__":
    main()
