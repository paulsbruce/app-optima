import json
import pandas as pd
from pathlib import Path

RESULTS_DIR = Path("results")
SUMMARY_FILE = RESULTS_DIR / "summary.json"
ITERATIONS_FILE = RESULTS_DIR / "iterations.csv"
OUTPUT_HTML = RESULTS_DIR / "report.html"


def load_data():
    summary = json.loads(SUMMARY_FILE.read_text())
    df = pd.read_csv(ITERATIONS_FILE)
    return summary, df


def indicator(value, baseline, higher_is_better=True):
    if pd.isna(value) or pd.isna(baseline):
        return "●", "neutral"

    if higher_is_better:
        if value > baseline:
            return "▲", "better"
        elif value < baseline:
            return "▼", "worse"
    else:
        if value < baseline:
            return "▲", "better"
        elif value > baseline:
            return "▼", "worse"

    return "●", "neutral"


def fmt_num(value, decimals=2):
    if pd.isna(value):
        return "NA"
    return f"{value:.{decimals}f}"

def parse_cpu_to_cores(value):
    if pd.isna(value):
        return None

    s = str(value).strip().lower()
    try:
        if s.endswith("m"):
            return float(s[:-1]) / 1000.0
        return float(s)
    except ValueError:
        return None
    

def normalize_memory_limit_mib(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    if "memory_limit_mib" not in df.columns:
        if "memory_limit" in df.columns:
            def parse_mem_to_mib(v):
                if pd.isna(v):
                    return None
                s = str(v).strip().lower()
                if s.endswith("mi"):
                    return float(s[:-2])
                if s.endswith("mib"):
                    return float(s[:-3])
                if s.endswith("gi"):
                    return float(s[:-2]) * 1024
                if s.endswith("gib"):
                    return float(s[:-3]) * 1024
                try:
                    return float(s)
                except ValueError:
                    return None

            df["memory_limit_mib"] = df["memory_limit"].apply(parse_mem_to_mib)
        else:
            df["memory_limit_mib"] = None

    return df


def add_cpu_efficiency(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    if "cpu_limit_cores" not in df.columns:
        if "cpu_limit" in df.columns:
            df["cpu_limit_cores"] = df["cpu_limit"].apply(parse_cpu_to_cores)
        else:
            df["cpu_limit_cores"] = None

    def calc_eff(row):
        thr = row.get("throughput")
        cores = row.get("cpu_limit_cores")
        if pd.isna(thr) or pd.isna(cores) or cores in (0, 0.0):
            return None
        return float(thr) / float(cores)

    df["cpu_efficiency"] = df.apply(calc_eff, axis=1)
    return df


def add_memory_usage_mib(df: pd.DataFrame) -> pd.DataFrame:
    """
    Prefer an observed memory usage column if present; otherwise fall back to memory_limit_mib.
    """
    df = df.copy()

    candidates = [
        "memory_usage_mib",
        "memory_used_mib",
        "avg_memory_usage_mib",
        "peak_memory_usage_mib",
    ]

    selected = None
    for c in candidates:
        if c in df.columns:
            selected = c
            break

    if selected:
        df["memory_usage_chart_mib"] = pd.to_numeric(df[selected], errors="coerce")
        df["memory_usage_chart_label"] = selected
    else:
        df["memory_usage_chart_mib"] = pd.to_numeric(df.get("memory_limit_mib"), errors="coerce")
        df["memory_usage_chart_label"] = "memory_limit_mib (fallback)"

    return df


def compute_pareto_frontier(df: pd.DataFrame) -> pd.DataFrame:
    """
    Pareto frontier for:
      - maximize throughput
      - minimize response_time_ms
    A point is dominated if another point has:
      throughput >= current throughput
      response_time_ms <= current response_time_ms
    and at least one strict inequality.
    """
    candidates = df.dropna(subset=["throughput", "response_time_ms"]).copy()
    if candidates.empty:
        candidates["is_pareto"] = []
        return candidates

    is_pareto = []

    for i, row_i in candidates.iterrows():
        dominated = False
        for j, row_j in candidates.iterrows():
            if i == j:
                continue

            better_or_equal_thr = row_j["throughput"] >= row_i["throughput"]
            better_or_equal_rt = row_j["response_time_ms"] <= row_i["response_time_ms"]
            strictly_better = (
                row_j["throughput"] > row_i["throughput"]
                or row_j["response_time_ms"] < row_i["response_time_ms"]
            )

            if better_or_equal_thr and better_or_equal_rt and strictly_better:
                dominated = True
                break

        is_pareto.append(not dominated)

    candidates["is_pareto"] = is_pareto

    frontier = candidates[candidates["is_pareto"]].copy()
    frontier = frontier.sort_values(by=["response_time_ms", "throughput"], ascending=[True, False])
    return frontier


def build_iteration_table(df, baseline_row):
    rows = []

    df.sort_values(by="objective_score", inplace=True)

    for _, r in df.iterrows():
        thr_sym, thr_class = indicator(
            r.get("throughput"), baseline_row.get("throughput"), True
        )
        rt_sym, rt_class = indicator(
            r.get("response_time_ms"), baseline_row.get("response_time_ms"), False
        )
        err_sym, err_class = indicator(
            r.get("error_rate_pct"), baseline_row.get("error_rate_pct"), False
        )
        mem_sym, mem_class = indicator(
            r.get("memory_limit_mib"), baseline_row.get("memory_limit_mib"), False
        )
        eff_sym, eff_class = indicator(
            r.get("cpu_efficiency"), baseline_row.get("cpu_efficiency"), True
        )

        iteration_display = int(r["iteration"]) if pd.notna(r.get("iteration")) else "NA"
        cpu_request = r.get("cpu_request", "NA")
        cpu_limit = r.get("cpu_limit", "NA")
        memory_request = r.get("memory_request", "NA")
        memory_limit = r.get("memory_limit", "NA")
        heap_mib = r.get("heap_mib", "NA")
        gc_type = r.get("gc_type", "NA")
        throughput = fmt_num(r.get("throughput"))
        response_time = fmt_num(r.get("response_time_ms"))
        error_rate = fmt_num(r.get("error_rate_pct"))
        memory_limit_mib = fmt_num(r.get("memory_limit_mib"), 0)
        cpu_efficiency = fmt_num(r.get("cpu_efficiency"))
        objective_score = fmt_num(r.get("objective_score"))
        acceptable = r.get("acceptable", "NA")

        row = f"""
        <tr>
            <td data-sort-value="{iteration_display}">{iteration_display}</td>
            <td data-sort-value="{cpu_request}">{cpu_request}</td>
            <td data-sort-value="{cpu_limit}">{cpu_limit}</td>
            <td data-sort-value="{memory_request}">{memory_request}</td>
            <td data-sort-value="{memory_limit}">{memory_limit}</td>
            <td data-sort-value="{heap_mib}">{heap_mib}</td>
            <td data-sort-value="{gc_type}">{gc_type}</td>

            <td class="{thr_class}" data-sort-value="{'' if pd.isna(r.get('throughput')) else float(r.get('throughput'))}">
                {thr_sym} {throughput}
            </td>

            <td class="{rt_class}" data-sort-value="{'' if pd.isna(r.get('response_time_ms')) else float(r.get('response_time_ms'))}">
                {rt_sym} {response_time}
            </td>

            <td class="{err_class}" data-sort-value="{'' if pd.isna(r.get('error_rate_pct')) else float(r.get('error_rate_pct'))}">
                {err_sym} {error_rate}
            </td>

            <td class="{mem_class}" data-sort-value="{'' if pd.isna(r.get('memory_limit_mib')) else float(r.get('memory_limit_mib'))}">
                {mem_sym} {memory_limit_mib}
            </td>

            <td class="{eff_class}" data-sort-value="{'' if pd.isna(r.get('cpu_efficiency')) else float(r.get('cpu_efficiency'))}">
                {eff_sym} {cpu_efficiency}
            </td>

            <td data-sort-value="{objective_score}">{objective_score}</td>
            <td data-sort-value="{acceptable}">{acceptable}</td>
        </tr>
        """
        rows.append(row)

    return "\n".join(rows)


def build_html(summary, df):
    df = normalize_memory_limit_mib(df)
    df = add_cpu_efficiency(df)
    df = add_memory_usage_mib(df)

    baseline = summary["baseline_config"]
    best = summary["best_configuration"]
    baseline_row = df.iloc[0]

    table_rows = build_iteration_table(df, baseline_row)

    throughput_series = df["throughput"].fillna(0).tolist()
    response_series = df["response_time_ms"].fillna(0).tolist()
    memory_series = df["memory_limit_mib"].tolist()
    cpu_efficiency_series = df["cpu_efficiency"].fillna(0).tolist() if "cpu_efficiency" in df.columns else []
    iterations = df["iteration"].tolist()

    mem_req_sym, mem_req_class = indicator(baseline_row["memory_request_mib"], best["memory_request_mib"], True)
    mem_lim_sym, mem_lim_class = indicator(baseline_row["memory_limit_mib"], best["memory_limit_mib"], True) 
    cpu_req_sym, cpu_req_class = indicator(baseline_row["cpu_request"].replace("m", ""), best["cpu_request"].replace("m", ""), True)
    cpu_lim_sym, cpu_lim_class = indicator(baseline_row["cpu_limit"].replace("m", ""), best["cpu_limit"].replace("m", ""), True)
    mem_use_sym, mem_use_class = indicator(baseline_row["memory_usage_mib"], best["memory_usage_mib"], True)
    cpu_use_sym, cpu_use_class = indicator(baseline_row["cpu_usage_cores"], best["cpu_usage_cores"], True)

    frontier_df = compute_pareto_frontier(df)

    pareto_all_points = []
    for _, r in df.dropna(subset=["throughput", "response_time_ms"]).iterrows():
        pareto_all_points.append({
            "x": float(r["response_time_ms"]),
            "y": float(r["throughput"]),
            "iteration": int(r["iteration"]) if pd.notna(r["iteration"]) else None,
            "acceptable": bool(r["acceptable"]) if "acceptable" in r and pd.notna(r["acceptable"]) else False,
        })

    pareto_frontier_points = []
    for _, r in frontier_df.iterrows():
        pareto_frontier_points.append({
            "x": float(r["response_time_ms"]),
            "y": float(r["throughput"]),
            "iteration": int(r["iteration"]) if pd.notna(r["iteration"]) else None,
        })

    gc_palette = {
        "UseG1GC": "rgba(54, 162, 235, 0.8)",
        "UseParallelGC": "rgba(255, 99, 132, 0.8)",
        "UseZGC": "rgba(75, 192, 192, 0.8)",
        "UseShenandoah": "rgba(255, 159, 64, 0.8)",
        "UseSerialGC": "rgba(153, 102, 255, 0.8)",
        "default": "rgba(201, 203, 207, 0.8)",
    }

    memory_throughput_points_by_gc = {}
    for _, r in df.dropna(subset=["throughput", "memory_usage_chart_mib"]).iterrows():
        gc_type = str(r.get("gc_type", "default"))
        memory_throughput_points_by_gc.setdefault(gc_type, []).append({
            "x": float(r["memory_usage_chart_mib"]),
            "y": float(r["throughput"]),
            "iteration": int(r["iteration"]) if pd.notna(r["iteration"]) else None,
        })

    memory_gc_datasets = []
    for gc_type, points in memory_throughput_points_by_gc.items():
        color = gc_palette.get(gc_type, gc_palette["default"])
        memory_gc_datasets.append({
            "label": gc_type,
            "data": points,
            "backgroundColor": color,
            "borderColor": color,
            "pointRadius": 6,
            "showLine": False,
        })

    memory_usage_label = (
        df["memory_usage_chart_label"].dropna().iloc[0]
        if "memory_usage_chart_label" in df.columns and not df["memory_usage_chart_label"].dropna().empty
        else "memory_limit_mib (fallback)"
    )

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Optimization Report</title>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<style>

body {{
    font-family: Arial, Helvetica, sans-serif;
    margin: 40px;
    background:#f5f5f5;
}}

h1,h2 {{
    margin-top:40px;
}}

.card {{
    background:white;
    padding:20px;
    border-radius:8px;
    margin-bottom:20px;
    box-shadow:0 2px 4px rgba(0,0,0,0.1);
}}

table {{
    border-collapse: collapse;
    width:100%;
}}

th,td {{
    padding:8px;
    border-bottom:1px solid #ddd;
    text-align:center;
}}

th {{
    background:#333;
    color:white;
}}

th.sortable {{
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
}}

th.sortable:hover {{
    background: #444;
}}

th.sortable .sort-indicator {{
    margin-left: 6px;
    font-size: 12px;
    opacity: 0.8;
}}

.better {{
    color:green;
    font-weight:bold;
}}

.worse {{
    color:red;
    font-weight:bold;
}}

.neutral {{
    color:gray;
}}

.metric-grid {{
    display:grid;
    grid-template-columns: repeat(3,1fr);
    gap:20px;
}}

</style>

</head>

<body>

<h1>Online Boutique Optimization Report</h1>

<div class="card">

<h2>Summary</h2>

<ul>
<li>Total Iterations: {summary["total_iterations"]}</li>
<li>Acceptable Iterations: {summary["acceptable_iterations"]}</li>
<li>Best Iteration: {best["iteration"]}</li>
</ul>

</div>


<div class="card">

<h2>Best Recommendation</h2>

<table>
<thead>
<tr>
<th>Metric</th>
<th>Baseline (Iteration {baseline_row['iteration']})</th>
<th>Best (Iteration {best['iteration']})</th>
</tr>
</thead>
<tbody>
<tr>
<td>Memory Request</td>
<td>{baseline_row['memory_request_mib']}</td>
<td class="{mem_req_class}">{mem_req_sym} {best['memory_request_mib']}</td>
</tr>
<tr>
<td>Memory Limit</td>
<td>{baseline_row['memory_limit_mib']}</td>
<td class="{mem_lim_class}">{mem_lim_sym} {best['memory_limit_mib']}</td>
</tr>
<tr>
<td>Memory Use (Avg)</td>
<td>{baseline_row['memory_usage_mib']:.2f}</td>
<td class="{mem_use_class}">{mem_use_sym} {best['memory_usage_mib']:.2f}</td>
</tr>
<tr>
<td>Heap</td>
<td>{baseline_row['heap_mib']}</td>
<td>{best['heap_mib']}</td>
</tr>
<tr>
<td>GC Type</td>
<td>{baseline_row['gc_type']}</td>
<td>{best['gc_type']}</td>
</tr>
<tr>
<td>CPU Request</td>
<td>{baseline_row['cpu_request']}</td>
<td class="{cpu_req_class}">{cpu_req_sym} {best['cpu_request']}</td>
</tr>
<tr>
<td>CPU Limit</td>
<td>{baseline_row['cpu_limit']}</td>
<td class="{cpu_lim_class}">{cpu_lim_sym} {best['cpu_limit']}</td>
</tr>
<tr>
<td>CPU Use (Avg)</td>
<td>{baseline_row['cpu_usage_cores']:.2f}</td>
<td class="{cpu_use_class}">{cpu_use_sym} {best['cpu_usage_cores']:.2f}</td>
</tr>
<tr>
<td>Throughput</td>
<td>{baseline_row['throughput']:.2f}</td>
<td>{best['throughput']:.2f}</td>
</tr>
<tr>
<td>Response Time (ms)</td>
<td>{baseline_row['response_time_ms']:.2f}</td>
<td>{best['response_time_ms']:.2f}</td>
</tr>
<tr>
<td>Error Rate (%)</td>
<td>{baseline_row['error_rate_pct']}</td>
<td>{best['error_rate_pct']}</td>
</tr>
</tbody>
</table>

</div>


<h2>Performance Charts</h2>
<div class="metric-grid">
    <div class="chart-card" style="display:none;">
        <canvas id="throughput"></canvas>
    </div>
    <div class="chart-card" style="display:none;">
        <canvas id="response"></canvas>
    </div>
    <div class="chart-card" style="display:none;">
        <canvas id="memory"></canvas>
    </div>
    <div class="chart-card">
        <canvas id="cpuEfficiency"></canvas>
        <div class="small-note">
            CPU efficiency = throughput / CPU limit cores. Higher means more throughput per provisioned CPU.
        </div>
    </div>
    <div class="chart-card">
        <canvas id="pareto"></canvas>
        <div class="small-note">
            X = response time (lower is better), Y = throughput (higher is better).
            Red points/line show the Pareto frontier: non-dominated configurations.
        </div>
        <div class="legend">
            <span><i class="dot dot-all"></i> all iterations</span>
            <span><i class="dot dot-frontier"></i> Pareto frontier</span>
        </div>
    </div>
    <div class="chart-card">
        <canvas id="memoryThroughputGc"></canvas>
        <div class="small-note">
            X = {memory_usage_label}, Y = throughput. Dot color corresponds to GC type.
        </div>
    </div>
</div>

</div>


<h2>Iteration Comparison</h2>

<div class="card">

<table id="iterationTable">

<thead>
                <tr>
                    <th class="sortable">Iteration<span class="sort-indicator"></span></th>
                    <th class="sortable">CPU Req<span class="sort-indicator"></span></th>
                    <th class="sortable">CPU Lim<span class="sort-indicator"></span></th>
                    <th class="sortable">Mem Req<span class="sort-indicator"></span></th>
                    <th class="sortable">Mem Lim<span class="sort-indicator"></span></th>
                    <th class="sortable">Heap<span class="sort-indicator"></span></th>
                    <th class="sortable">GC<span class="sort-indicator"></span></th>
                    <th class="sortable">Throughput<span class="sort-indicator"></span></th>
                    <th class="sortable">Resp Time<span class="sort-indicator"></span></th>
                    <th class="sortable">Error %<span class="sort-indicator"></span></th>
                    <th class="sortable">Mem Limit MiB<span class="sort-indicator"></span></th>
                    <th class="sortable">CPU Efficiency<span class="sort-indicator"></span></th>
                    <th class="sortable">Score<span class="sort-indicator"></span></th>
                    <th class="sortable">Acceptable<span class="sort-indicator"></span></th>
                </tr>
            </thead>
<tbody>

{table_rows}

</tbody>

</table>

</div>


<script>

const iterations = {iterations};
const throughputSeries = {json.dumps(throughput_series)};
const responseSeries = {json.dumps(response_series)};
const memorySeries = {json.dumps(memory_series)};
const cpuEfficiencySeries = {json.dumps(cpu_efficiency_series)};
const paretoAllPoints = {json.dumps(pareto_all_points)};
const paretoFrontierPoints = {json.dumps(pareto_frontier_points)};
const memoryGcDatasets = {json.dumps(memory_gc_datasets)};

new Chart(document.getElementById('throughput'), {{
type: 'line',
data: {{
labels: iterations,
datasets: [{{
label: 'Throughput',
data: {throughput_series},
borderWidth:2
}}]
}}
}});

new Chart(document.getElementById('response'), {{
type: 'line',
data: {{
labels: iterations,
datasets: [{{
label: 'Response Time (ms)',
data: {response_series},
borderWidth:2
}}]
}}
}});

new Chart(document.getElementById('memory'), {{
type: 'line',
data: {{
labels: iterations,
datasets: [{{
label: 'Memory Limit (MiB)',
data: {memory_series},
borderWidth:2
}}]
}}
}});

new Chart(document.getElementById('cpuEfficiency'), {{
    type: 'line',
    data: {{
        labels: iterations,
        datasets: [{{
            label: 'CPU Efficiency',
            data: cpuEfficiencySeries,
            borderWidth: 2,
            tension: 0.15
        }}]
    }},
    options: {{
        responsive: true,
        plugins: {{
            title: {{
                display: true,
                text: 'CPU Utilization Efficiency by Iteration'
            }}
        }}
    }}
}});

new Chart(document.getElementById('pareto'), {{
    type: 'scatter',
    data: {{
        datasets: [
            {{
                label: 'All Iterations',
                data: paretoAllPoints,
                pointRadius: 5,
                showLine: false
            }},
            {{
                label: 'Pareto Frontier',
                data: paretoFrontierPoints,
                pointRadius: 6,
                borderWidth: 2,
                showLine: true
            }}
        ]
    }},
    options: {{
        responsive: true,
        parsing: false,
        plugins: {{
            title: {{
                display: true,
                text: 'Pareto Frontier: Throughput vs Response Time'
            }},
            tooltip: {{
                callbacks: {{
                    label: function(context) {{
                        const p = context.raw;
                        const parts = [];
                        if (p.iteration !== undefined && p.iteration !== null) {{
                            parts.push('Iteration ' + p.iteration);
                        }}
                        parts.push('Resp: ' + p.x.toFixed(2) + ' ms');
                        parts.push('Thr: ' + p.y.toFixed(2));
                        return parts;
                    }}
                }}
            }}
        }},
        scales: {{
            x: {{
                title: {{
                    display: true,
                    text: 'Response Time (ms) ↓'
                }}
            }},
            y: {{
                title: {{
                    display: true,
                    text: 'Throughput ↑'
                }}
            }}
        }}
    }}
}});

new Chart(document.getElementById('memoryThroughputGc'), {{
    type: 'scatter',
    data: {{
        datasets: memoryGcDatasets
    }},
    options: {{
        responsive: true,
        parsing: false,
        plugins: {{
            title: {{
                display: true,
                text: 'Throughput vs Memory Usage by GC Type'
            }},
            tooltip: {{
                callbacks: {{
                    label: function(context) {{
                        const p = context.raw;
                        const parts = [];
                        parts.push(context.dataset.label);
                        if (p.iteration !== undefined && p.iteration !== null) {{
                            parts.push('Iteration ' + p.iteration);
                        }}
                        parts.push('Memory: ' + p.x.toFixed(2) + ' MiB');
                        parts.push('Throughput: ' + p.y.toFixed(2));
                        return parts;
                    }}
                }}
            }}
        }},
        scales: {{
            x: {{
                title: {{
                    display: true,
                    text: '{memory_usage_label} →'
                }}
            }},
            y: {{
                title: {{
                    display: true,
                    text: 'Throughput ↑'
                }}
            }}
        }}
    }}
}});

// Table sorting
(function() {{
    const table = document.getElementById('iterationTable');
    const headers = table.querySelectorAll('thead th.sortable');
    const tbody = table.querySelector('tbody');

    let currentSortColumn = -1;
    let currentSortDirection = 'asc';

    function resetIndicators() {{
        headers.forEach(th => {{
            const indicator = th.querySelector('.sort-indicator');
            if (indicator) indicator.textContent = '';
        }});
    }}

    function parseSortValue(value) {{
        if (value === null || value === undefined) return {{ type: 'string', value: '' }};

        const s = String(value).trim();
        if (s === '' || s.toLowerCase() === 'na') {{
            return {{ type: 'empty', value: null }};
        }}

        const lower = s.toLowerCase();
        if (lower === 'true') return {{ type: 'boolean', value: 1 }};
        if (lower === 'false') return {{ type: 'boolean', value: 0 }};

        const num = Number(s);
        if (!Number.isNaN(num)) {{
            return {{ type: 'number', value: num }};
        }}

        return {{ type: 'string', value: s.toLowerCase() }};
    }}

    function compareValues(a, b, direction) {{
        const av = parseSortValue(a);
        const bv = parseSortValue(b);

        if (av.type === 'empty' && bv.type === 'empty') return 0;
        if (av.type === 'empty') return 1;
        if (bv.type === 'empty') return -1;

        let cmp = 0;
        if ((av.type === 'number' || av.type === 'boolean') && (bv.type === 'number' || bv.type === 'boolean')) {{
            cmp = av.value - bv.value;
        }} else {{
            cmp = String(av.value).localeCompare(String(bv.value), undefined, {{ numeric: true, sensitivity: 'base' }});
        }}

        return direction === 'asc' ? cmp : -cmp;
    }}

    headers.forEach((header, columnIndex) => {{
        header.addEventListener('click', () => {{
            const rows = Array.from(tbody.querySelectorAll('tr'));

            if (currentSortColumn === columnIndex) {{
                currentSortDirection = currentSortDirection === 'asc' ? 'desc' : 'asc';
            }} else {{
                currentSortColumn = columnIndex;
                currentSortDirection = 'asc';
            }}

            rows.sort((rowA, rowB) => {{
                const cellA = rowA.children[columnIndex];
                const cellB = rowB.children[columnIndex];

                const valueA = cellA.getAttribute('data-sort-value') ?? cellA.textContent.trim();
                const valueB = cellB.getAttribute('data-sort-value') ?? cellB.textContent.trim();

                return compareValues(valueA, valueB, currentSortDirection);
            }});

            rows.forEach(row => tbody.appendChild(row));

            resetIndicators();
            const indicator = header.querySelector('.sort-indicator');
            if (indicator) {{
                indicator.textContent = currentSortDirection === 'asc' ? '▲' : '▼';
            }}
        }});
    }});
}})();

</script>

</body>
</html>
"""

    return html


def main():

    summary, df = load_data()
    html = build_html(summary, df)

    OUTPUT_HTML.write_text(html)

    print(f"Report generated: {OUTPUT_HTML}")


if __name__ == "__main__":
    main()