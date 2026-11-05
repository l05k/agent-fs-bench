"""Paper figure style (see .agents/skills/paper-figure-style/SKILL.md).

Imports only stdlib and Matplotlib: `plot` copies this file verbatim into each
exported figure script, which must run without the package.
"""

from pathlib import Path

PALETTE = ('#64A5D3', '#F1AD54', '#94A65D', '#BC5D5A')
# Primary first gets vertical lines so the horizontal median line stays visible.
HATCHES = ('|||||', '/////', '\\\\\\\\\\', '-----')
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
}
MARGIN = 0.01  # inches, outer figure margin
LEGEND_GAP = 0.0025  # inches, plot area to top legend
PNG_DPI = 600


def require_arial() -> None:
    """Fail instead of letting Matplotlib substitute a different font."""
    from matplotlib import font_manager

    try:
        font_manager.findfont('Arial', fallback_to_default=False)
    except ValueError:
        raise RuntimeError('font Arial is required; install it '
                           '(Ubuntu: sudo apt install ttf-mscorefonts-installer)') from None


def render(data: dict, stem: Path) -> None:
    """Draw grouped box plots (x = case, series = target) to <stem>.pdf and <stem>.png."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    from matplotlib.ticker import MaxNLocator

    cases, targets, samples = data['cases'], data['targets'], data['samples']
    if len(targets) > len(PALETTE):
        raise ValueError(f'at most {len(PALETTE)} targets per figure')
    require_arial()
    plt.rcParams.update(RC)
    width, height = data['size']
    figure = plt.figure(figsize=(width, height))
    axes = figure.add_axes((0, 0, 1, 1))

    slot = 0.8 / len(targets)
    for index, target in enumerate(targets):
        offset = (index - (len(targets) - 1) / 2) * slot
        present = [(position + offset, samples[target][case])
                   for position, case in enumerate(cases) if samples[target].get(case)]
        positions, groups = zip(*present)
        axes.boxplot(
            groups, positions=positions, widths=slot * 0.8, patch_artist=True,
            showmeans=True, manage_ticks=False,
            boxprops={'facecolor': PALETTE[index], 'edgecolor': 'black',
                      'hatch': HATCHES[index], 'linewidth': 0.6},
            medianprops={'color': 'black', 'linewidth': 0.8},
            whiskerprops={'linewidth': 0.6}, capprops={'linewidth': 0.6},
            meanprops={'marker': '^', 'markerfacecolor': 'white', 'markeredgecolor': 'black',
                       'markeredgewidth': 0.5, 'markersize': 3},
            flierprops={'marker': 'o', 'markerfacecolor': 'black', 'markeredgecolor': 'none',
                        'markersize': 2},
        )

    axes.set_xticks(range(len(cases)), cases)
    axes.set_xlim(-0.5, len(cases) - 0.5)
    # Pad half a tick step beyond the outer ticks so their labels stay off the box edges.
    values = [value for target in targets for group in samples[target].values() for value in group]
    low, high = min(values), max(values)
    ticks = MaxNLocator(nbins=4).tick_values(low, high)
    first, last = max(t for t in ticks if t <= low), min(t for t in ticks if t >= high)
    ticks = [tick for tick in ticks if first <= tick <= last]
    step = ticks[1] - ticks[0]
    axes.set_yticks(ticks)
    axes.set_ylim(ticks[0] - step / 2, ticks[-1] + step / 2)
    axes.set_ylabel(data['ylabel'])

    legend = figure.legend(
        handles=[Patch(facecolor=PALETTE[index], edgecolor='black', hatch=HATCHES[index],
                       linewidth=0.6, label=target) for index, target in enumerate(targets)],
        loc='lower center', ncols=len(targets), frameon=False, borderpad=0, borderaxespad=0,
        handlelength=1.6, handleheight=0.9, handletextpad=0.4, columnspacing=1.0,
    )

    # Inch-based layout: measure decorations once, then place axes and legend exactly.
    renderer = figure.canvas.get_renderer()
    figure.canvas.draw()
    dpi = figure.dpi
    box, tight = axes.get_window_extent(renderer), axes.get_tightbbox(renderer)
    left = MARGIN + max(0, box.x0 - tight.x0) / dpi
    bottom = MARGIN + max(0, box.y0 - tight.y0) / dpi
    right = width - MARGIN - max(0, tight.x1 - box.x1) / dpi
    top = height - MARGIN - legend.get_window_extent(renderer).height / dpi - LEGEND_GAP
    axes.set_position((left / width, bottom / height, (right - left) / width, (top - bottom) / height))
    legend.set_bbox_to_anchor(((left + right) / 2 / width, (top + LEGEND_GAP) / height),
                              transform=figure.transFigure)

    figure.savefig(stem.with_name(stem.name + '.pdf'))
    figure.savefig(stem.with_name(stem.name + '.png'), dpi=PNG_DPI)
    plt.close(figure)
