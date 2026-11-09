---
name: paper-figure-style
description: Create or revise system-experiment figures for papers. Use when plotting experiment data, updating Matplotlib scripts, or checking figure PDFs so that outputs follow the project style: vector PDF, 9pt fonts, compact single-column layout by default, ordered legends with the primary system first, consistent colors/markers, boxed subplots, shared axis labels, and no internal grid lines.
---

# Paper Figure Style

Use this skill when drawing or updating system-experiment figures for papers.

## Project Implementation

- `agent_fs_bench/figure.py` holds the shared style (`RC`, `PALETTE`, `HATCHES`, inch-based layout). It must import only stdlib and Matplotlib: its source is copied verbatim into every exported figure script.
- `python -m agent_fs_bench plot <run-dir>...` (also run automatically at the end of `run`) writes `<stem>.pdf`, `<stem>.png` (600 dpi) and `<stem>.py` (self-contained: style code + raw samples) for each default figure in the workload module's `FIGURES`, or for one custom figure given `--table --x --y`. Rerun `python <stem>.py` to regenerate both images without the CSVs.
- `--targets a,b,c` fixes legend order and filters targets; the first target is the primary system. Line figures sort x numerically; box figures keep first-appearance order. `--xlabel`/`--ylabel` set labels (default: column names). `--size W H` sets inches per figure (default `3.5 1.5`).
- Legend entries are target aliases; name targets as they should appear in the paper.
- All figure text is English, so only Arial is required; SimHei rules below do not apply to this project.
- Hatch order puts vertical lines on the primary system so the horizontal box median stays visible.
- Figure size and figure-specific choices may differ per experiment; the shared style rules below do not.

## Workflow

1. Read the source data and any existing plotting script before editing.
2. Generate a vector PDF and keep the plotting script reproducible and self-contained.
3. Verify the PDF page size after generation.
4. Render a temporary preview when possible and check spacing, labels, legend order, and overlap.
5. Keep generated figures under the requested output path; do not change raw data.

## Required Output

- Format: vector PDF (plus a PNG preview/export in this project).
- For each figure, generate a self-contained Python script that includes the raw data and the plotting code.
- Default size: single column, `3.5in x 1.5in`.
- Use double column, `7in x 1.5in`, only when explicitly requested or clearly needed.
- Use 9pt fonts. Use Arial for English and SimHei/黑体 for Chinese. Do not silently fall back to substitute fonts; if the required font is unavailable, stop and tell the user to install it, e.g. on Ubuntu: `sudo apt install ttf-mscorefonts-installer` for Arial.
- Embed editable text in PDFs when possible, e.g. Matplotlib `pdf.fonttype = 42` and `ps.fonttype = 42`.

## Layout

- If subplots share the same x-axis meaning, use one shared x-axis label instead of repeating the label under every subplot.
- Put the legend above the plots.
- Put the primary system first in the legend. If the user does not explicitly identify the primary system, treat the first system given in the prompt as the primary system.
- Use boxed axes for every subplot: all four spines visible.
- Put subplot labels and titles inside each subplot, e.g. `(a) SIFT`, `(b) DEEP`, `(c) SPACEV`.
- Place subplot labels and titles at the same in-axis position across all subplots unless a figure-specific requirement makes that impossible.
- Place subplot labels and titles only in one of the four in-axis corners: upper-left, upper-right, lower-left, or lower-right.
- If subplot labels or titles would cover data in the chosen corner, expand the x-axis or y-axis limits to create empty space rather than moving labels to a non-corner position.
- If standard tick labels would overlap or touch subplot/figure boundaries, expand the axis limits instead of dropping the tick labels, e.g. use `0.55` to `1.05` for ticks spanning `0.6` to `1.0`.
- Use a single y-axis label on the left for one-y-axis figures, rotated counter-clockwise.
- For an additional y-axis, place the label on the right and rotate clockwise.
- Use approximately `0.0025in` spacing between plot area and the top legend, and approximately `0.005in` spacing between plot area and the shared bottom axis label.
- Use approximately `0.04in` spacing between subplots.
- Use approximately `0.01in` outer margins unless readability requires a small adjustment.
- Do not use gray grid lines or other internal value-guide lines unless the user explicitly asks for them.

## Colors And Patterns

- Use this palette in order, with the first color reserved for the primary system:
  `#64A5D3`, `#F1AD54`, `#94A65D`, `#BC5D5A`.
- If the user explicitly asks for a breakdown figure that starts from a baseline and progressively adds designs, use this sequential palette:
  `#C7DFEC`, `#7BBBDD`, `#3B94C8`, `#0065AB`.
- For line charts, use solid lines for all series.
- Differentiate every series for black-and-white printing using markers or patterns.
- For line charts, use filled markers such as circle, triangle, inverted triangle, square, or diamond.
- For bar charts, use distinct hatches such as horizontal, vertical, forward diagonal, and backward diagonal.

## Matplotlib Defaults

Prefer these defaults unless a figure has a specific reason to differ:

```python
plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": [
        "Arial",
        "SimHei",
    ],
    "font.size": 9,
    "axes.labelsize": 9,
    "axes.titlesize": 9,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.fontsize": 9,
    "axes.linewidth": 0.8,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
})
```

For compact multi-subplot single-column figures, define layout in inches rather than relying only on `tight_layout`, so legend, subplot gaps, and margins are controllable.

## Validation Checklist

- PDF is vector and has the requested physical size.
- Legend is above the plots and the primary system is listed first.
- Subplot labels/titles are inside axes and do not cover important data.
- Shared x-axis label is used when subplots have the same x-axis.
- All subplots are boxed.
- No gray grid/value-guide lines appear inside the plot area.
- Curves are solid and series remain distinguishable in grayscale.
- Text, ticks, and markers do not overlap at the final PDF size.
