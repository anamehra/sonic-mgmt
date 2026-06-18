#!/usr/bin/env python3
"""
Generate HBM occupancy vs frame size plot as SVG.

Reads the hbm_result.txt output file and produces an SVG chart.
No external dependencies required (works on Python 3.6+).

Usage:
    python3 plot_hbm.py <hbm_result.txt> [output.svg]

The plot shows frame size on X-axis, HBM occupancy (MB) on Y-axis,
with one line per congestion rate (100G and 150G).
"""
import sys
import os
import re


def parse_hbm_results(filepath):
    """Parse hbm_result.txt and return frame_size -> [(tx_gbps, hbm_mb), ...] ordered list.

    Expected table format per frame size:
      TxRate   RxRate        RxPPS     RxL1   HBM MB        Drops Result
        99.0G    97.1G    9,123,456    99.2G    123.4  1,234,567 PASS
    """
    data = {}  # {frame_size: [(tx_gbps, hbm_mb), ...]}
    current_frame_size = None

    with open(filepath) as f:
        for line in f:
            m = re.match(r'.*FRAME SIZE (\d+)', line)
            if m:
                current_frame_size = int(m.group(1))
                data[current_frame_size] = []
                continue

            # Match data lines: TxRate(G) RxRate(G) RxPPS RxL1(G) HBM_MB Drops Result
            m = re.match(
                r'\s*([\d.]+)G\s+([\d.]+)G\s+([\d,]+)\s+([\d.]+)G\s+([\d.]+)\s+([\d,]+)\s+(\S+)',
                line)
            if m and current_frame_size is not None:
                tx_gbps = float(m.group(1))
                hbm_mb = float(m.group(5))
                data[current_frame_size].append((tx_gbps, hbm_mb))

    return data


def generate_svg(data, title="HBM Occupancy vs Frame Size"):
    """Generate SVG chart from parsed data. Groups by rate index (position)."""
    frame_sizes = sorted(data.keys())
    num_rates = max(len(v) for v in data.values()) if data else 0

    if num_rates == 0:
        return "<svg><text>No data found</text></svg>"

    # Separate congestion rates (significant HBM) from no-congestion
    # Only plot a rate if its max HBM exceeds 10% of overall max
    overall_max = max((data[fs][ri][1] for fs in frame_sizes for ri in range(num_rates)
                       if ri < len(data[fs])), default=0)
    threshold = overall_max * 0.10

    congestion_indices = []
    nocongestion_indices = []
    for ri in range(num_rates):
        max_for_rate = max((data[fs][ri][1] for fs in frame_sizes if ri < len(data[fs])),
                           default=0)
        if max_for_rate > threshold:
            congestion_indices.append(ri)
        else:
            nocongestion_indices.append(ri)

    # Build labels for plotted rates as % of egress speed
    # Assume egress = 100G; rates are in Gbps so percentage = rate_gbps / 100 * 100
    egress_speed = 100.0
    rate_labels = {}
    for ri in congestion_indices:
        vals = [data[fs][ri][0] for fs in frame_sizes if ri < len(data[fs])]
        if all(v == vals[0] for v in vals):
            pct = vals[0] / egress_speed * 100
            rate_labels[ri] = f'{pct:.0f}% TxRate'
        else:
            lo_pct = min(vals) / egress_speed * 100
            hi_pct = max(vals) / egress_speed * 100
            rate_labels[ri] = f'{lo_pct:.0f}-{hi_pct:.0f}% TxRate'

    colors_map = ['#1976D2', '#D32F2F', '#388E3C', '#F57C00', '#7B1FA2']
    dash_patterns = ['', '6,3']

    # Chart dimensions
    margin_left = 70
    margin_right = 200
    margin_top = 50
    margin_bottom = 65
    width = 900
    height = 370
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    # Scale - only congestion data
    hbm_vals = [data[fs][ri][1] for fs in frame_sizes for ri in congestion_indices
                if ri < len(data[fs])]
    max_hbm = max(hbm_vals) if hbm_vals else 100
    max_hbm = int((max_hbm / 20) + 1) * 20

    def x_pos(i):
        if len(frame_sizes) == 1:
            return margin_left + plot_w / 2
        return margin_left + i * plot_w / (len(frame_sizes) - 1)

    def y_pos(val):
        return margin_top + plot_h - (val / max_hbm * plot_h)

    svg = []
    svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
               f'font-family="Arial, sans-serif" font-size="12">')
    svg.append(f'<rect width="{width}" height="{height}" fill="white"/>')

    # Title
    svg.append(f'<text x="{margin_left + plot_w/2}" y="28" text-anchor="middle" font-size="14" '
               f'font-weight="bold">{title}</text>')

    # Grid lines
    num_gridlines = 5
    for i in range(num_gridlines + 1):
        val = max_hbm * i / num_gridlines
        y = y_pos(val)
        svg.append(f'<line x1="{margin_left}" y1="{y}" x2="{margin_left+plot_w}" '
                   f'y2="{y}" stroke="#ddd" stroke-width="0.7"/>')
        svg.append(f'<text x="{margin_left-8}" y="{y+4}" text-anchor="end" '
                   f'font-size="10" fill="#444">{val:.0f}</text>')

    # Axes
    svg.append(f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" '
               f'y2="{margin_top+plot_h}" stroke="#333" stroke-width="1.2"/>')
    svg.append(f'<line x1="{margin_left}" y1="{margin_top+plot_h}" '
               f'x2="{margin_left+plot_w}" y2="{margin_top+plot_h}" stroke="#333" stroke-width="1.2"/>')

    # X-axis labels
    for i, fs in enumerate(frame_sizes):
        x = x_pos(i)
        svg.append(f'<text x="{x}" y="{margin_top+plot_h+16}" text-anchor="middle" '
                   f'font-size="10" fill="#333">{fs}</text>')

    # Axis titles
    svg.append(f'<text x="{margin_left + plot_w/2}" y="{height-8}" text-anchor="middle" '
               f'font-size="11" fill="#333">Frame Size (bytes)</text>')
    svg.append(f'<text x="16" y="{margin_top + plot_h/2}" text-anchor="middle" '
               f'font-size="11" fill="#333" transform="rotate(-90,16,{margin_top + plot_h/2})">'
               f'HBM Occupancy (MB)</text>')

    # Plot only congestion lines
    for ci, ri in enumerate(congestion_indices):
        color = colors_map[ci % len(colors_map)]
        dash = dash_patterns[ci % len(dash_patterns)]
        points = []
        hbm_values = []
        for i, fs in enumerate(frame_sizes):
            if ri < len(data[fs]):
                hbm = data[fs][ri][1]
                points.append((x_pos(i), y_pos(hbm)))
                hbm_values.append(hbm)

        if len(points) >= 2:
            path = ' '.join(f'{x:.1f},{y:.1f}' for x, y in points)
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ''
            svg.append(f'<polyline points="{path}" fill="none" stroke="{color}" '
                       f'stroke-width="2.5"{dash_attr}/>')

        # Data points with value labels
        for idx, (x, y) in enumerate(points):
            svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>')
            val = hbm_values[idx]
            # Offset labels: first line above, second line below to avoid overlap
            label_y = y - 9 if ci == 0 else y + 15
            svg.append(f'<text x="{x:.1f}" y="{label_y}" text-anchor="middle" '
                       f'font-size="8" fill="{color}">{val:.0f}</text>')

    # Legend (right side panel)
    lx = margin_left + plot_w + 25
    ly = margin_top + 10
    svg.append(f'<text x="{lx}" y="{ly}" font-size="11" font-weight="bold" fill="#333">'
               f'Legend</text>')
    for ci, ri in enumerate(congestion_indices):
        color = colors_map[ci % len(colors_map)]
        dash = dash_patterns[ci % len(dash_patterns)]
        item_y = ly + 20 + ci * 24
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ''
        svg.append(f'<line x1="{lx}" y1="{item_y}" x2="{lx+28}" y2="{item_y}" '
                   f'stroke="{color}" stroke-width="2.5"{dash_attr}/>')
        svg.append(f'<circle cx="{lx+14}" cy="{item_y}" r="3" fill="{color}"/>')
        svg.append(f'<text x="{lx+36}" y="{item_y+4}" font-size="10" fill="#333">'
                   f'{rate_labels[ri]}</text>')

    # Annotation for no-congestion rates
    if nocongestion_indices:
        note_y = ly + 20 + len(congestion_indices) * 24 + 18
        svg.append(f'<line x1="{lx}" y1="{note_y-6}" x2="{lx+160}" y2="{note_y-6}" '
                   f'stroke="#ccc" stroke-width="0.5"/>')
        svg.append(f'<text x="{lx}" y="{note_y+10}" font-size="9" fill="#666">'
                   f'Below threshold:</text>')
        svg.append(f'<text x="{lx}" y="{note_y+24}" font-size="9" fill="#666">'
                   f'max_l2-1, max_l2 TxRate</text>')
        svg.append(f'<text x="{lx}" y="{note_y+38}" font-size="9" fill="#666">'
                   f'= 0 MB HBM, 0 drops</text>')

    svg.append('</svg>')
    return '\n'.join(svg)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <hbm_result.txt> [output.svg]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else input_file.replace('.txt', '.svg')
    if output_file == input_file:
        output_file = input_file + '.svg'

    data = parse_hbm_results(input_file)
    if not data:
        print(f"No data found in {input_file}")
        sys.exit(1)

    print(f"Parsed {len(data)} frame sizes: {sorted(data.keys())}")
    svg = generate_svg(data)

    with open(output_file, 'w') as f:
        f.write(svg)
    print(f"Plot saved to {output_file}")


if __name__ == '__main__':
    main()
