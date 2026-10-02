from __future__ import annotations

import csv
import json
from dataclasses import asdict, fields
from html import escape
from pathlib import Path

from mlb_prop_predictor.pipeline import HrRow, KRow, Report


def _pct(x: float | None, digits: int = 1) -> str:
    return "-" if x is None else f"{x * 100:.{digits}f}%"


def _odds(x: int | None) -> str:
    return "-" if x is None else (f"+{x}" if x > 0 else str(x))


def _signed_pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:+.1f}%"


# -- console -----------------------------------------------------------------


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows]
    return "\n".join(out)


def render_console(report: Report, top: int = 15) -> str:
    parts = [f"MLB Prop Predictor - {report.date} ({len(report.games)} games)"]
    for note in report.notes:
        parts.append(f"note: {note}")

    hr_rows = [
        [
            r.player,
            r.team,
            f"vs {r.pitcher} ({r.pitcher_hand})",
            str(r.slot or "-"),
            _pct(r.prob),
            _odds(r.fair_odds),
            _odds(r.best_price),
            _signed_pct(r.edge),
        ]
        for r in report.home_runs[:top]
    ]
    parts += [
        "",
        "HOME RUNS (P of 1+ HR)",
        _table(["Batter", "Team", "Opp. starter", "Slot", "Model", "Fair", "Best", "Edge"], hr_rows),
    ]

    k_rows = [
        [
            r.pitcher,
            r.team,
            f"vs {r.opponent}",
            f"{r.expected_k:.2f}",
            f"{r.line:g}",
            _pct(r.p_over),
            _odds(r.over_price),
            _odds(r.under_price),
            r.pick or "-",
            _signed_pct(r.edge),
        ]
        for r in report.strikeouts[:top]
    ]
    parts += [
        "",
        "PITCHER STRIKEOUTS",
        _table(
            ["Pitcher", "Team", "Opp.", "Proj K", "Line", "P(Over)", "Over", "Under", "Pick", "Edge"], k_rows
        ),
    ]
    if report.odds_credits_remaining is not None:
        parts += [
            "",
            f"Odds API credits used this run: {report.odds_credits_used}; "
            f"remaining: {report.odds_credits_remaining}",
        ]
    return "\n".join(parts)


# -- JSON / CSV ----------------------------------------------------------------


def write_json(report: Report, path: Path) -> Path:
    path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    return path


def write_csv(report: Report, directory: Path) -> list[Path]:
    written = []
    for name, rows, cls in (("home_runs", report.home_runs, HrRow), ("strikeouts", report.strikeouts, KRow)):
        path = directory / f"{name}_{report.date}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=[f.name for f in fields(cls)])
            writer.writeheader()
            for r in rows:
                writer.writerow(asdict(r))
        written.append(path)
    return written


# -- HTML ----------------------------------------------------------------------

_CSS = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1d1f23;--muted:#6b6f76;--line:#e4e2dc;--accent:#0b5d3b;--pos:#0b7a46;
--neg:#b4372f;--chip:#eef2ee}
@media (prefers-color-scheme:dark){:root{--bg:#121416;--card:#1b1e21;--ink:#e9eaec;--muted:#9aa0a6;
--line:#2c3035;--accent:#5cc49a;--pos:#5cc49a;--neg:#ef8a80;--chip:#23282c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}h2{font-size:17px;margin:34px 0 10px}
.sub{color:var(--muted)}.notes{margin:14px 0;padding:10px 14px;border-left:3px solid var(--accent);
background:var(--card);border-radius:6px}
.games{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:10px;margin-top:14px}
.game{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.game b{display:block}.game small{color:var(--muted);display:block}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:7px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{position:sticky;top:0;background:var(--card);cursor:pointer;font-weight:600;color:var(--muted);user-select:none}
th:hover{color:var(--ink)}td:first-child,th:first-child,td.l,th.l{text-align:left}
tr:last-child td{border-bottom:0}.pos{color:var(--pos);font-weight:600}.neg{color:var(--neg)}
.chip{display:inline-block;padding:1px 7px;border-radius:99px;background:var(--chip);font-size:12px;color:var(--muted)}
footer{margin-top:40px;color:var(--muted);font-size:12px}
"""

_JS = """
document.querySelectorAll('table').forEach(t=>{t.querySelectorAll('th').forEach((th,i)=>{th.addEventListener('click',()=>{
const b=t.tBodies[0],rows=[...b.rows],asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
const v=r=>{const c=r.cells[i];const n=parseFloat(c.dataset.v);return isNaN(n)?c.textContent:n};
rows.sort((a,c)=>{const x=v(a),y=v(c);return (x>y?1:x<y?-1:0)*(asc?1:-1)});rows.forEach(r=>b.appendChild(r))})})});
"""


def _td(text: str, value: float | None = None, cls: str = "") -> str:
    attr = f' data-v="{value}"' if value is not None else ""
    klass = f' class="{cls}"' if cls else ""
    return f"<td{klass}{attr}>{escape(text)}</td>"


def _edge_cls(x: float | None) -> str:
    if x is None:
        return ""
    return "pos" if x > 0.02 else ("neg" if x < -0.02 else "")


def write_html(report: Report, path: Path, top: int = 60) -> Path:
    games = "".join(
        f'<div class="game"><b>{escape(g.label)}</b><small>{escape(g.venue)}</small>'
        f"<small>HR park {g.park_hr:.2f} · K park {g.park_k:.2f} · "
        f"{'roof closed' if g.roof_closed else (f'{g.temp_f:.0f}°F' if g.temp_f is not None else 'temp n/a')}"
        f"</small><small>Lineups: {escape(g.away_lineup)} / {escape(g.home_lineup)}</small></div>"
        for g in report.games
    )

    hr_body = "".join(
        "<tr>"
        + _td(r.player, cls="l")
        + _td(r.team, cls="l")
        + _td(f"{r.pitcher} ({r.pitcher_hand})", cls="l")
        + _td(str(r.slot or "-"), r.slot)
        + f'<td class="l"><span class="chip">{escape(r.lineup_status)}</span></td>'
        + _td(_pct(r.batter_hr_rate, 2), r.batter_hr_rate)
        + _td(_pct(r.pitcher_hr_rate, 2), r.pitcher_hr_rate)
        + _td(f"{r.hr_env:.2f}", r.hr_env)
        + _td(_pct(r.prob), r.prob)
        + _td(_odds(r.fair_odds), r.prob)
        + _td(_odds(r.best_price) + (f" {r.best_book}" if r.best_book else ""), r.best_price)
        + _td(
            _pct(r.market_prob) + ("" if r.market_devigged or r.market_prob is None else "*"), r.market_prob
        )
        + _td(_signed_pct(r.edge), r.edge, _edge_cls(r.edge))
        + "</tr>"
        for r in report.home_runs[:top]
    )

    k_body = "".join(
        "<tr>"
        + _td(r.pitcher, cls="l")
        + _td(f"{r.team} vs {r.opponent}", cls="l")
        + _td(r.throws)
        + f'<td class="l"><span class="chip">{escape(r.opp_lineup_status)}</span></td>'
        + _td(_pct(r.k_rate), r.k_rate)
        + _td(f"{r.expected_bf:.1f}", r.expected_bf)
        + _td(f"{r.expected_k:.2f}", r.expected_k)
        + _td(f"{r.line:g}" + ("" if r.line_source == "market" else " (model)"), r.line)
        + _td(_pct(r.p_over), r.p_over)
        + _td(_odds(r.over_price), r.over_price)
        + _td(_odds(r.under_price), r.under_price)
        + _td(r.pick or "-", cls="l")
        + _td(_signed_pct(r.edge), r.edge, _edge_cls(r.edge))
        + "</tr>"
        for r in report.strikeouts
    )

    notes = "".join(f"<div>{escape(n)}</div>" for n in report.notes)
    credits = (
        f" · Odds API credits used: {report.odds_credits_used}, remaining: {escape(report.odds_credits_remaining)}"
        if report.odds_credits_remaining is not None
        else ""
    )
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>MLB Props {report.date}</title>
<style>{_CSS}</style></head><body><main>
<h1>MLB Prop Predictor</h1><div class="sub">{report.date} · {len(report.games)} games · generated {report.generated_at}
{credits}</div>
{f'<div class="notes">{notes}</div>' if notes else ""}
<div class="games">{games}</div>
<h2>Home runs: probability of 1+ HR</h2>
<div class="wrap"><table><thead><tr><th class="l">Batter</th><th class="l">Team</th><th class="l">Opp. starter</th>
<th>Slot</th><th class="l">Lineup</th><th>Batter HR/PA</th><th>SP HR/BF</th><th>Env.</th><th>Model</th>
<th>Fair odds</th><th>Best price</th><th>Market</th><th>Edge</th></tr></thead><tbody>{hr_body}</tbody></table></div>
<h2>Pitcher strikeouts</h2>
<div class="wrap"><table><thead><tr><th class="l">Pitcher</th><th class="l">Matchup</th><th>Throws</th>
<th class="l">Opp. lineup</th><th>K/BF</th><th>Exp. BF</th><th>Proj. K</th><th>Line</th><th>P(Over)</th>
<th>Over</th><th>Under</th><th class="l">Pick</th><th>Edge</th></tr></thead><tbody>{k_body}</tbody></table></div>
<footer>Model probabilities are estimates, not guarantees. Market = average vig-free probability across books
(* = only one side quoted, vig not removed). Edge = model minus market. Click a column to sort.<br>
Data: MLB Stats API and Baseball Savant (© MLB Advanced Media, individual non-commercial use);
weather by <a href="https://open-meteo.com/">Open-Meteo.com</a> (CC BY 4.0); odds by The Odds API.</footer>
</main><script>{_JS}</script></body></html>"""
    path.write_text(html, encoding="utf-8")
    return path
