"""Phase C: Pattern diagnostic harness — measures adverse selection illusion.

Compares P&L across 4 fill modes for the SAME signals to quantify the gap
between fantasy (naive) and executable (tape/conservative) results.

Key metric: illusion_ratio = naive_pnl / max(abs(conservative_pnl), eps)
If illusion_ratio >> 1, the strategy is a paper mirage.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row

from pm_agent.db.repo import db_conn
from pm_agent.replay.fill_models import FillMode, simulate_fill
from pm_agent.scanners.interfaces import Signal

log = logging.getLogger(__name__)

EPS = 1e-9


@dataclass
class SignalDiagnostics:
    signal_id: str
    pattern: str
    market_id: str
    naive_pnl: float
    latency_pnl_2s: float
    latency_pnl_10s: float
    latency_pnl_30s: float
    tape_confirmed_pnl: float
    conservative_pnl: float
    illusion_ratio: float
    adverse_selection_loss: float
    fill_rate_by_mode: dict
    quote_survival_rate: float
    slippage_bps: float
    time_to_resolution_sec: int | None
    bucket: str


@dataclass
class DiagnosticReport:
    pattern: str
    total_signals: int = 0
    avg_naive_pnl: float = 0.0
    avg_tape_pnl: float = 0.0
    avg_conservative_pnl: float = 0.0
    avg_illusion_ratio: float = 0.0
    avg_adverse_selection_loss: float = 0.0
    avg_slippage_bps: float = 0.0
    fill_rates: dict = field(default_factory=dict)
    by_bucket: dict = field(default_factory=dict)
    verdict: str = ""  # 'MIRAGE'|'MARGINAL'|'REAL_EDGE'


def time_bucket(time_to_res_sec: int | None) -> str:
    if time_to_res_sec is None:
        return "unknown"
    if time_to_res_sec < 3600:
        return "0-1h"
    if time_to_res_sec < 21600:
        return "1-6h"
    if time_to_res_sec < 86400:
        return "6-24h"
    return "24h+"


async def diagnose_signal(signal: Signal, time_to_resolution_sec: int | None = None) -> SignalDiagnostics:
    """Run one signal through all fill modes, compute illusion metrics."""
    decision_time = signal.signal_time + timedelta(seconds=1.0)
    market_id = signal.market_id
    outcome_id = signal.outcome_id

    # Run all 4 modes
    naive = await simulate_fill(FillMode.NAIVE, market_id, outcome_id, signal.side,
                                signal.limit_price, signal.size, decision_time)
    lat_2s = await simulate_fill(FillMode.LATENCY, market_id, outcome_id, signal.side,
                                 signal.limit_price, signal.size, decision_time, latency_sec=2.0)
    lat_10s = await simulate_fill(FillMode.LATENCY, market_id, outcome_id, signal.side,
                                  signal.limit_price, signal.size, decision_time, latency_sec=10.0)
    lat_30s = await simulate_fill(FillMode.LATENCY, market_id, outcome_id, signal.side,
                                  signal.limit_price, signal.size, decision_time, latency_sec=30.0)
    tape = await simulate_fill(FillMode.TAPE, market_id, outcome_id, signal.side,
                               signal.limit_price, signal.size, decision_time)
    cons = await simulate_fill(FillMode.CONSERVATIVE, market_id, outcome_id, signal.side,
                               signal.limit_price, signal.size, decision_time)

    def pnl(fr):
        if not fr.filled:
            return 0.0
        # proxy P&L: (limit - fill) * size for BUY; (fill - limit) * size for SELL
        if signal.side == "BUY":
            return (signal.limit_price - (fr.fill_price or 0)) * (fr.fill_size or 0)
        return ((fr.fill_price or 0) - signal.limit_price) * (fr.fill_size or 0)

    naive_pnl = pnl(naive)
    tape_pnl = pnl(tape)
    cons_pnl = pnl(cons)

    illusion = naive_pnl / max(abs(cons_pnl), EPS) if cons_pnl != 0 else (999.0 if naive_pnl > 0 else 0.0)
    adverse_loss = naive_pnl - cons_pnl
    slippage = ((naive.fill_price or 0) - (tape.fill_price or 0)) * 10000 if (naive.filled and tape.filled) else 0.0
    survival = 1.0 if lat_2s.filled else (0.5 if lat_10s.filled else (0.1 if lat_30s.filled else 0.0))

    return SignalDiagnostics(
        signal_id=signal.signal_id if hasattr(signal, "signal_id") else "",
        pattern=signal.pattern,
        market_id=market_id,
        naive_pnl=naive_pnl,
        latency_pnl_2s=pnl(lat_2s),
        latency_pnl_10s=pnl(lat_10s),
        latency_pnl_30s=pnl(lat_30s),
        tape_confirmed_pnl=tape_pnl,
        conservative_pnl=cons_pnl,
        illusion_ratio=illusion,
        adverse_selection_loss=adverse_loss,
        fill_rate_by_mode={
            "naive": int(naive.filled), "latency_2s": int(lat_2s.filled),
            "latency_10s": int(lat_10s.filled), "latency_30s": int(lat_30s.filled),
            "tape": int(tape.filled), "conservative": int(cons.filled),
        },
        quote_survival_rate=survival,
        slippage_bps=slippage,
        time_to_resolution_sec=time_to_resolution_sec,
        bucket=time_bucket(time_to_resolution_sec),
    )


async def store_diagnostics(diag: SignalDiagnostics, signal_id: str) -> None:
    async with db_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO pattern_diagnostics
                    (pattern, signal_id, market_id, naive_pnl, latency_pnl_2s, latency_pnl_10s,
                     latency_pnl_30s, tape_confirmed_pnl, conservative_pnl, illusion_ratio,
                     adverse_selection_loss, fill_rate_by_mode, quote_survival_rate, slippage_bps,
                     time_to_resolution_sec, bucket)
                VALUES (%(pat)s, %(sid)s, %(mid)s, %(np)s, %(l2)s, %(l10)s, %(l30)s, %(tp)s, %(cp)s,
                        %(ir)s, %(al)s, %(fr)s, %(qs)s, %(sl)s, %(tr)s, %(bk)s)
                """,
                dict(pat=diag.pattern, sid=signal_id, mid=diag.market_id,
                     np=diag.naive_pnl, l2=diag.latency_pnl_2s, l10=diag.latency_pnl_10s,
                     l30=diag.latency_pnl_30s, tp=diag.tape_confirmed_pnl, cp=diag.conservative_pnl,
                     ir=diag.illusion_ratio, al=diag.adverse_selection_loss,
                     fr=__import__("json").dumps(diag.fill_rate_by_mode),
                     qs=diag.quote_survival_rate, sl=diag.slippage_bps,
                     tr=diag.time_to_resolution_sec, bk=diag.bucket),
            )


async def run_diagnostics(signals: list[Signal], pattern: str) -> DiagnosticReport:
    """Run diagnostic harness on a batch of signals. Measures adverse selection illusion."""
    report = DiagnosticReport(pattern=pattern)
    if not signals:
        report.verdict = "NO_SIGNALS"
        return report

    for sig in signals:
        sig_id = f"{pattern}:{sig.market_id}:{sig.signal_time.isoformat()}"
        diag = await diagnose_signal(sig)
        await store_diagnostics(diag, sig_id)
        report.total_signals += 1
        report.avg_naive_pnl += diag.naive_pnl
        report.avg_tape_pnl += diag.tape_confirmed_pnl
        report.avg_conservative_pnl += diag.conservative_pnl
        report.avg_illusion_ratio += diag.illusion_ratio
        report.avg_adverse_selection_loss += diag.adverse_selection_loss
        report.avg_slippage_bps += diag.slippage_bps
        report.by_bucket.setdefault(diag.bucket, {"count": 0, "naive": 0, "conservative": 0})
        report.by_bucket[diag.bucket]["count"] += 1
        report.by_bucket[diag.bucket]["naive"] += diag.naive_pnl
        report.by_bucket[diag.bucket]["conservative"] += diag.conservative_pnl

    n = report.total_signals
    report.avg_naive_pnl /= n
    report.avg_tape_pnl /= n
    report.avg_conservative_pnl /= n
    report.avg_illusion_ratio /= n
    report.avg_adverse_selection_loss /= n
    report.avg_slippage_bps /= n

    # Verdict: compare naive vs conservative
    if report.avg_conservative_pnl <= 0 and report.avg_naive_pnl > 0:
        report.verdict = "MIRAGE"  # paper profit, live loss
    elif report.avg_illusion_ratio > 3.0:
        report.verdict = "MIRAGE"
    elif report.avg_conservative_pnl > 0 and report.avg_illusion_ratio < 2.0:
        report.verdict = "REAL_EDGE"
    else:
        report.verdict = "MARGINAL"

    log.info("diagnostics %s: %d signals, naive=%.4f conservative=%.4f illusion=%.2f verdict=%s",
             pattern, n, report.avg_naive_pnl, report.avg_conservative_pnl,
             report.avg_illusion_ratio, report.verdict)
    return report
