"""pm-agent CLI — Phase A commands: init-db, collect-markets, collect-orderbooks, replay."""
from __future__ import annotations

import asyncio
import logging

import click
from rich.console import Console
from rich.table import Table

from pm_agent.config import settings
from pm_agent.collectors import market_discovery, orderbook_collector
from pm_agent.db.repo import get_pool

console = Console()
log = logging.getLogger(__name__)


@click.group()
@click.option("--log-level", default=None, help="Override LOG_LEVEL")
def cli(log_level: str | None) -> None:
    """pm-agent: prediction market data + replay engine (Phase A)."""
    level = log_level or settings.log_level
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@cli.command("init-db")
def init_db() -> None:
    """Run schema.sql + all migrations against configured database."""
    import pathlib
    import psycopg
    base = pathlib.Path(__file__).parent / "db"
    sql_files = [
        base / "schema.sql",
        base / "migrations" / "02_phase_b.sql",
        base / "migrations" / "03_phase_cd.sql",
    ]
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        for sql_file in sql_files:
            if not sql_file.exists():
                console.print(f"[yellow]skip (not found): {sql_file.name}[/yellow]")
                continue
            sql = sql_file.read_text()
            conn.execute(sql)
            console.print(f"[green]applied: {sql_file.name}[/green]")
    console.print("[green]All schema + migrations applied.[/green]")


@cli.command("collect-markets")
@click.option("--pages", default=5, help="Pagination pages per venue")
def collect_markets(pages: int) -> None:
    """Discover markets from Polymarket + Kalshi."""
    asyncio.run(_collect_markets(pages))


async def _collect_markets(pages: int) -> None:
    await get_pool()
    poly = market_discovery.discover_polymarket(pages=pages)
    kalshi = market_discovery.discover_kalshi(events_pages=pages, markets_pages=pages)
    p, k = await asyncio.gather(poly, kalshi)
    console.print(f"Polymarket: {p} markets | Kalshi: {k} markets")


@cli.command("collect-orderbooks")
@click.option("--interval", default=None, type=int, help="Seconds between snapshots")
@click.option("--venue", default=None, help="polymarket|kalshi|all")
def collect_orderbooks(interval: int | None, venue: str | None) -> None:
    """Continuously snapshot orderbooks for watchlist markets."""
    asyncio.run(_collect_orderbooks(interval, venue))


async def _collect_orderbooks(interval: int | None, venue: str | None) -> None:
    await get_pool()
    await orderbook_collector.run_loop(
        interval_sec=interval or settings.snapshot_liquid_sec,
        venue=venue,
    )


@cli.command("show-status")
def show_status() -> None:
    """Print DB row counts per table."""
    asyncio.run(_show_status())


async def _show_status() -> None:
    from pm_agent.db.repo import db_conn
    await get_pool()
    tables = ["markets", "outcomes", "market_rules", "orderbook_snapshots",
              "trade_prints", "resolutions", "replay_runs", "paper_orders", "paper_fills"]
    t = Table(title="pm-agent DB status")
    t.add_column("table"); t.add_column("rows", justify="right")
    async with db_conn() as conn:
        for tbl in tables:
            try:
                v = await (await conn.execute(f"SELECT count(*) FROM {tbl}")).fetchone()
                t.add_row(tbl, str(v[0]))
            except Exception as e:
                t.add_row(tbl, f"err: {e}")
    console.print(t)


if __name__ == "__main__":
    cli()


# ---- Phase B commands: matching, arb scan, arb gate ----

@cli.command("run-matching")
@click.option("--threshold", default=0.5, help="Title similarity threshold")
@click.option("--limit", default=50, help="Max candidate pairs")
@click.option("--model", default=None, help="LLM model (default GLM-5.2)")
def run_matching(threshold: float, limit: int, model: str | None) -> None:
    """Run LLM-assisted contract matching on candidate pairs."""
    from pm_agent.matching.contract_matcher import run_matching as _run, GLM_MODEL
    asyncio.run(_run(threshold=threshold, limit=limit, model=model or GLM_MODEL))


@cli.command("matching-queue")
def matching_queue() -> None:
    """Show pairs awaiting human review."""
    from pm_agent.matching.contract_matcher import human_review_queue
    asyncio.run(_show_queue(human_review_queue))


async def _show_queue(fn) -> None:
    await get_pool()
    rows = await fn(limit=50)
    t = Table(title="Human review queue")
    t.add_column("id"); t.add_column("poly"); t.add_column("kalshi"); t.add_column("risk", justify="right")
    for r in rows:
        t.add_row(str(r["id"]), r["poly_title"][:40], r["kalshi_title"][:40], f"{r['mismatch_risk']:.2f}")
    console.print(t)


@cli.command("approve-pair")
@click.argument("pair_id", type=int)
@click.option("--reviewer", default="vladimir")
@click.option("--notes", default=None)
def approve_pair(pair_id: int, reviewer: str, notes: str | None) -> None:
    """Manually approve a matched pair after human review."""
    from pm_agent.matching.contract_matcher import human_approve
    asyncio.run(_approve(pair_id, reviewer, notes, human_approve))


async def _approve(pair_id: int, reviewer: str, notes: str | None, fn) -> None:
    await get_pool()
    await fn(pair_id, reviewer, notes)
    console.print(f"[green]Pair {pair_id} approved by {reviewer}[/green]")


@cli.command("scan-arb")
@click.option("--threshold", default=0.03, help="Min net spread pct (0.03 = 3%)")
def scan_arb(threshold: float) -> None:
    """Scan approved matched pairs for arb opportunities."""
    from pm_agent.scanners.stubs.arb import ArbScanner
    asyncio.run(_scan_arb(ArbScanner, threshold))


async def _scan_arb(ScannerCls, threshold: float) -> None:
    await get_pool()
    scanner = ScannerCls(threshold_pct=threshold)
    count = 0
    async for opp in scanner.scan():
        count += 1
        console.print(f"[cyan]ARB[/cyan] {opp.arb_id} spread={opp.expected_net_spread:.4f} risk={opp.resolution_mismatch_risk:.2f}")
    console.print(f"[green]{count} opportunities found[/green]")


@cli.command("arb-gate")
def arb_gate() -> None:
    """Run decision gate on detected arb opportunities (all 4 fill modes)."""
    from pm_agent.scanners.stubs.arb import ArbScanner
    from pm_agent.replay.arb_engine import arb_decision_gate
    asyncio.run(_arb_gate(ArbScanner, arb_decision_gate))


async def _arb_gate(ScannerCls, gate_fn) -> None:
    await get_pool()
    scanner = ScannerCls()
    opps = [opp async for opp in scanner.scan()]
    if not opps:
        console.print("[yellow]No arb opportunities detected. Run collectors + matching first.[/yellow]")
        return
    report = await gate_fn(opps)
    console.print_json(data=report)


# ---- Phase C/D commands: transcripts, diagnostics, patterns #2/#3 ----

@cli.command("add-transcript")
@click.argument("market_id")
@click.option("--speaker", default=None)
@click.option("--source-type", default="youtube_auto", help="youtube_auto|official|cspan|rev")
@click.option("--url", required=True, help="Transcript URL")
@click.option("--text", default=None, help="Transcript text (or fetch from URL via yt-dlp)")
def add_transcript(market_id: str, speaker: str | None, source_type: str, url: str, text: str | None) -> None:
    """Add a transcript source for a mention market (semi-manual QA)."""
    asyncio.run(_add_transcript(market_id, speaker, source_type, url, text))


async def _add_transcript(market_id: str, speaker: str | None, source_type: str, url: str, text: str | None) -> None:
    await get_pool()
    async with db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO transcript_sources (market_id, speaker, source_type, source_url, transcript_text)
            VALUES (%(mid)s, %(sp)s, %(st)s, %(url)s, %(tx)s)
            ON CONFLICT (market_id, source_url) DO UPDATE SET transcript_text=EXCLUDED.transcript_text
            """,
            dict(mid=market_id, sp=speaker, st=source_type, url=url, tx=text),
        )
    console.print(f"[green]Transcript added for {market_id}[/green]")


@cli.command("compute-base-rate")
@click.argument("market_id")
@click.option("--phrase", required=True, help="Target word/phrase to count")
def compute_base_rate(market_id: str, phrase: str) -> None:
    """Compute mention base rate from stored transcripts."""
    asyncio.run(_compute_base_rate(market_id, phrase))


async def _compute_base_rate(market_id: str, phrase: str) -> None:
    await get_pool()
    from pm_agent.db.queries import get_latest_yes_price
    async with db_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT transcript_text FROM transcript_sources WHERE market_id=%s AND transcript_text IS NOT NULL",
                (market_id,),
            )
            rows = await cur.fetchall()
            if not rows:
                console.print("[red]No transcripts with text. Add with --text or fetch via yt-dlp.[/red]")
                return
            transcript_count = len(rows)
            mention_count = sum(1 for r in rows if phrase.lower() in (r["transcript_text"] or "").lower())
            base_rate = mention_count / transcript_count if transcript_count else 0
            market_price = await get_latest_yes_price(market_id) or 0.0
            edge = base_rate - market_price
            await cur.execute(
                """
                INSERT INTO mention_base_rates (market_id, phrase, transcript_count, mention_count, base_rate, market_price, edge)
                VALUES (%(mid)s, %(ph)s, %(tc)s, %(mc)s, %(br)s, %(mp)s, %(e)s)
                """,
                dict(mid=market_id, ph=phrase, tc=transcript_count, mc=mention_count,
                     br=base_rate, mp=market_price, e=edge),
            )
    console.print(f"[green]Base rate: {mention_count}/{transcript_count} = {base_rate:.2%}, market={market_price:.2f}, edge={edge:+.2f}[/green]")


@cli.command("add-context")
@click.argument("market_id")
@click.option("--incumbent", required=True)
@click.option("--country", required=True)
@click.option("--approval", type=float, default=None)
@click.option("--economy", default="strong", help="strong|weak|recession")
@click.option("--scandal", default="none", help="none|minor|severe")
def add_context(market_id: str, incumbent: str, country: str, approval: float | None, economy: str, scandal: str) -> None:
    """Add context validation for an incumbent market."""
    asyncio.run(_add_context(market_id, incumbent, country, approval, economy, scandal))


async def _add_context(market_id: str, incumbent: str, country: str, approval: float | None, economy: str, scandal: str) -> None:
    await get_pool()
    async with db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO context_validations (market_id, incumbent, country, is_oecd, approval_rating, economy_context, scandal_severity, validated_by)
            VALUES (%(mid)s, %(inc)s, %(ctry)s, %(oecd)s, %(ap)s, %(ec)s, %(sc)s, 'human')
            """,
            dict(mid=market_id, inc=incumbent, ctry=country, oecd=False, ap=approval, ec=economy, sc=scandal),
        )
    console.print(f"[green]Context added for {market_id}[/green]")


@cli.command("scan-narrative")
@click.option("--bankroll", default=1000.0, type=float)
def scan_narrative(bankroll: float) -> None:
    """Scan mention markets for YES-fade signals."""
    from pm_agent.scanners.stubs.narrative_yes_fade import NarrativeYesFadeScanner
    asyncio.run(_scan_single_leg(NarrativeYesFadeScanner, bankroll, "narrative"))


@cli.command("scan-incumbent")
@click.option("--bankroll", default=5000.0, type=float)
def scan_incumbent(bankroll: float) -> None:
    """Scan electoral markets for incumbent YES signals."""
    from pm_agent.scanners.stubs.incumbent import IncumbentScanner
    asyncio.run(_scan_single_leg(IncumbentScanner, bankroll, "incumbent"))


async def _scan_single_leg(ScannerCls, bankroll: float, name: str) -> None:
    await get_pool()
    scanner = ScannerCls(bankroll=bankroll)
    signals = [s async for s in scanner.scan()]
    if not signals:
        console.print(f"[yellow]No {name} signals. Collect markets + add transcripts/context first.[/yellow]")
        return
    for s in signals:
        console.print(f"[cyan]{name.upper()}[/cyan] {s.market_id} {s.side} @ {s.limit_price:.2f} edge={s.edge:+.3f} {s.rationale[:60]}")
    console.print(f"[green]{len(signals)} signals[/green]")


@cli.command("run-diagnostics")
@click.option("--pattern", required=True, help="pre_resolution_creep|narrative_yes_fade|incumbent")
@click.option("--bankroll", default=1000.0, type=float)
def run_diagnostics(pattern: str, bankroll: float) -> None:
    """Phase C: run diagnostic harness (measure adverse selection illusion)."""
    asyncio.run(_run_diagnostics(pattern, bankroll))


async def _run_diagnostics(pattern: str, bankroll: float) -> None:
    await get_pool()
    from pm_agent.replay.diagnostics import run_diagnostics as run_diag
    # Pick scanner by pattern
    if pattern == "narrative_yes_fade":
        from pm_agent.scanners.stubs.narrative_yes_fade import NarrativeYesFadeScanner as S
    elif pattern == "incumbent":
        from pm_agent.scanners.stubs.incumbent import IncumbentScanner as S
    elif pattern == "pre_resolution_creep":
        from pm_agent.scanners.stubs.pre_resolution_creep import PreResolutionCreepScanner as S
    else:
        console.print(f"[red]Unknown pattern: {pattern}[/red]")
        return
    scanner = S(bankroll=bankroll) if hasattr(S, "__init__") and "bankroll" in S.__init__.__code__.co_varnames else S()
    signals = [s async for s in scanner.scan()]
    if not signals:
        console.print(f"[yellow]No {pattern} signals to diagnose.[/yellow]")
        return
    report = await run_diag(signals, pattern)
    console.print_json(data={
        "pattern": report.pattern,
        "total_signals": report.total_signals,
        "avg_naive_pnl": round(report.avg_naive_pnl, 4),
        "avg_tape_pnl": round(report.avg_tape_pnl, 4),
        "avg_conservative_pnl": round(report.avg_conservative_pnl, 4),
        "avg_illusion_ratio": round(report.avg_illusion_ratio, 2),
        "avg_adverse_selection_loss": round(report.avg_adverse_selection_loss, 4),
        "verdict": report.verdict,
    })
