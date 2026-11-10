"""Paper figure style (see .agents/skills/paper-figure-style/SKILL.md).

Imports only stdlib and Matplotlib: `plot` copies this file verbatim into each
exported figure script, which must run without the package.
"""

import math
from pathlib import Path
import re

PALETTE = ('#64A5D3', '#F1AD54', '#94A65D', '#BC5D5A')
CAPTION_COLOR = '#8B0002'  # in-axis subplot labels/titles only
# Primary first gets vertical lines so the horizontal median line stays visible.
HATCHES = ('|||||', '/////', '\\\\\\\\\\', '-----')
MARKERS = ('o', '^', 'v', 's')
LINE_WIDTH = 1.0
MARKER_SIZE = 3.5
ERROR_CAP = 2.0
RC = {
    'font.family': 'sans-serif',
    'font.sans-serif': ['Arial'],
    'font.size': 9,
    'axes.labelsize': 9,
    'axes.titlesize': 9,
    'xtick.labelsize': 9,
    'ytick.labelsize': 9,
    'legend.fontsize': 9,
    'axes.linewidth': 0.8,
    'axes.grid': False,
    'hatch.linewidth': 0.5,
    'pdf.fonttype': 42,
    'ps.fonttype': 42,
    'axes.formatter.useoffset': False,
    'axes.formatter.limits': (-9, 9),
}
MARGIN = 0.01  # inches, outer figure margin
LEGEND_GAP = 0.0025  # inches, plot area to top legend
PNG_DPI = 600


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _number(value: float) -> str:
    return f'{value:.0f}' if float(value).is_integer() else f'{value:g}'


def _log_ticks(low: float, high: float) -> list[float]:
    """Plain-number log ticks within [low, high]: decades, else 1-2-5, else 1-9 steps."""
    decades = range(math.floor(math.log10(low)), math.ceil(math.log10(high)) + 1)
    ticks: list[float] = []
    for steps, most in (((1,), 5), ((1, 2, 5), 5), (range(1, 10), 3)):
        ticks = [step * 10.0 ** decade for decade in decades for step in steps
                 if low <= step * 10.0 ** decade <= high]
        if len(ticks) >= 2:
            return ticks[::-(-len(ticks) // most)]
    return ticks or [math.sqrt(low * high)]


def _wrap(text: str) -> str:
    """Break a label into two lines at the `_`, `-`, `/` or space nearest its middle."""
    cuts = [match.end() for match in re.finditer(r'[_\-/ ]', text)]
    if not cuts:
        return text
    cut = min(cuts, key=lambda position: abs(2 * position - len(text)))
    return text[:cut].rstrip() + '\n' + text[cut:]


def render(data: dict, stem: Path) -> None:
    """Draw `data` (kind line: median + IQR per x; kind box: one box per x) to <stem>.pdf/.png."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.patches import Patch
    from matplotlib.ticker import MaxNLocator, NullFormatter

    series, xs, values = data['series'], data['x'], data['values']
    if len(series) > len(PALETTE):
        raise ValueError(f'at most {len(PALETTE)} series per figure, got {series}; '
                         'select them with plot --targets')
    try:
        font_manager.findfont('Arial', fallback_to_default=False)
    except ValueError:
        raise RuntimeError('font Arial is required; install it '
                           '(Ubuntu: sudo apt install ttf-mscorefonts-installer)') from None
    plt.rcParams.update(RC)
    width, height = data['size']
    figure = plt.figure(figsize=(width, height))
    axes = figure.add_axes((0, 0, 1, 1))

    low, high, handles = float('inf'), float('-inf'), []
    if data['kind'] == 'box':
        slot = 0.8 / len(series)
        for index, name in enumerate(series):
            offset = (index - (len(series) - 1) / 2) * slot
            present = [(position + offset, group)
                       for position, group in enumerate(values[name]) if group]
            axes.boxplot(
                [group for _, group in present], positions=[x for x, _ in present],
                widths=slot * 0.8, patch_artist=True, showmeans=True, manage_ticks=False,
                boxprops={'facecolor': PALETTE[index], 'edgecolor': 'black',
                          'hatch': HATCHES[index], 'linewidth': 0.6},
                medianprops={'color': 'black', 'linewidth': 0.8},
                whiskerprops={'linewidth': 0.6}, capprops={'linewidth': 0.6},
                meanprops={'marker': '^', 'markerfacecolor': 'white', 'markeredgecolor': 'black',
                           'markeredgewidth': 0.5, 'markersize': 3},
                flierprops={'marker': 'o', 'markerfacecolor': 'black', 'markeredgecolor': 'none',
                            'markersize': 2},
            )
            samples = [value for _, group in present for value in group]
            low, high = min(low, *samples), max(high, *samples)
            handles.append(Patch(facecolor=PALETTE[index], edgecolor='black',
                                 hatch=HATCHES[index], linewidth=0.6))
        axes.set_xticks(range(len(xs)), xs)
        axes.set_xlim(-0.5, len(xs) - 0.5)
    else:
        for index, name in enumerate(series):
            points = [(x, group) for x, group in zip(xs, values[name]) if group]
            medians = [_percentile(group, 0.5) for _, group in points]
            quartiles = [(_percentile(group, 0.25), _percentile(group, 0.75)) for _, group in points]
            low = min(low, *(q[0] for q in quartiles))
            high = max(high, *(q[1] for q in quartiles))
            line = axes.errorbar(
                [x for x, _ in points], medians,
                yerr=[[m - q[0] for m, q in zip(medians, quartiles)],
                      [q[1] - m for m, q in zip(medians, quartiles)]],
                color=PALETTE[index], marker=MARKERS[index], markersize=MARKER_SIZE,
                markerfacecolor=PALETTE[index], markeredgecolor='black', markeredgewidth=0.5,
                linewidth=LINE_WIDTH, elinewidth=0.7, capsize=ERROR_CAP, capthick=0.7,
            )
            handles.append(line.lines[0])
        # At most 7 labeled x ticks: every k-th value on log axes, round steps otherwise.
        if data['logx']:
            axes.set_xscale('log')
            axes.set_xlim(xs[0] / 1.15, xs[-1] * 1.15)
            ticks = xs[::-(-len(xs) // 7)]
        else:
            pad = (xs[-1] - xs[0]) * 0.05 or 0.5
            axes.set_xlim(xs[0] - pad, xs[-1] + pad)
            ticks = xs if len(xs) <= 7 else [
                tick for tick in MaxNLocator(nbins=6).tick_values(xs[0], xs[-1])
                if xs[0] - pad <= tick <= xs[-1] + pad]
        axes.set_xticks(ticks, [_number(x) for x in ticks])
        axes.xaxis.set_minor_formatter(NullFormatter())

    if data['logy']:
        axes.set_yscale('log')
        axes.set_ylim(low / 1.3, high * 1.3)
        # Explicit labels: the default log formatter uses mathtext, which is not Arial.
        ticks = _log_ticks(low / 1.14, high * 1.14)
        axes.set_yticks(ticks, [_number(tick) for tick in ticks])
        axes.yaxis.set_minor_formatter(NullFormatter())
    else:
        if low == high:
            low, high = low - (abs(low) * 0.1 or 1.0), high + (abs(high) * 0.1 or 1.0)
        # Outer ticks bracket the data; pad half a step so their labels clear the edges.
        ticks = list(MaxNLocator(nbins=4).tick_values(low, high))
        while len(ticks) > 2 and ticks[1] <= low:
            ticks.pop(0)
        while len(ticks) > 2 and ticks[-2] >= high:
            ticks.pop()
        step = ticks[1] - ticks[0]
        axes.set_yticks(ticks)
        axes.set_ylim(ticks[0] - step / 2, ticks[-1] + step / 2)
    if data['title']:
        # Open space above the data so the in-axis corner title covers nothing.
        bottom, top = axes.get_ylim()
        axes.set_ylim(bottom, top * (top / bottom) ** 0.3 if data['logy']
                      else top + (top - bottom) * 0.3)
        axes.text(0.02, 0.98, data['title'], transform=axes.transAxes, ha='left', va='top',
                  color=CAPTION_COLOR)
    axes.set_xlabel(data['xlabel'])
    axes.set_ylabel(data['ylabel'])

    renderer = figure.canvas.get_renderer()
    for columns in range(len(series), 0, -1):
        legend = figure.legend(
            handles=handles, labels=series, loc='lower center', ncols=columns, frameon=False,
            borderpad=0, borderaxespad=0, handlelength=1.6 if data['kind'] == 'box' else 1.8,
            handleheight=0.9 if data['kind'] == 'box' else 0.7,
            handletextpad=0.4, columnspacing=1.0,
        )
        figure.canvas.draw()
        if columns == 1 or legend.get_window_extent(renderer).width / figure.dpi <= width - 2 * MARGIN:
            break
        legend.remove()

    # Two-line y label when one line is taller than the figure (e.g. a long --ylabel).
    if axes.yaxis.label.get_window_extent(renderer).height > (height - 2 * MARGIN) * figure.dpi:
        axes.set_ylabel(_wrap(data['ylabel']))
        figure.canvas.draw()

    # Inch-based layout: measure decorations, then place axes and legend exactly.
    # Category labels that collide at the final width are wrapped, then rotated.
    dpi = figure.dpi
    for attempt in (1, 2, 3):
        box, tight = axes.get_window_extent(renderer), axes.get_tightbbox(renderer)
        left = MARGIN + max(0, box.x0 - tight.x0) / dpi
        bottom = MARGIN + max(0, box.y0 - tight.y0) / dpi
        right = width - MARGIN - max(0, tight.x1 - box.x1) / dpi
        top = height - MARGIN - legend.get_window_extent(renderer).height / dpi - LEGEND_GAP
        axes.set_position((left / width, bottom / height, (right - left) / width, (top - bottom) / height))
        legend.set_bbox_to_anchor(((left + right) / 2 / width, (top + LEGEND_GAP) / height),
                                  transform=figure.transFigure)
        figure.canvas.draw()
        labels = [label.get_window_extent(renderer) for label in axes.get_xticklabels()
                  if label.get_text()]
        if attempt == 3 or all(a.x1 < b.x0 for a, b in zip(labels, labels[1:])):
            break
        if attempt == 1:
            axes.set_xticks(axes.get_xticks(),
                            [_wrap(label.get_text()) for label in axes.get_xticklabels()])
        else:
            axes.tick_params(axis='x', labelrotation=30)
            for label in axes.get_xticklabels():
                label.set_horizontalalignment('right')
                label.set_rotation_mode('anchor')
        figure.canvas.draw()
    label = axes.yaxis.label
    label_box = label.get_window_extent(renderer)
    shift = max(0, MARGIN * dpi - label_box.y0) - max(0, label_box.y1 - (height - MARGIN) * dpi)
    if shift:
        x, y = label.get_position()
        label.set_position((x, y + shift / axes.get_window_extent(renderer).height))

    figure.savefig(stem.with_name(stem.name + '.pdf'))
    figure.savefig(stem.with_name(stem.name + '.png'), dpi=PNG_DPI)
    plt.close(figure)
