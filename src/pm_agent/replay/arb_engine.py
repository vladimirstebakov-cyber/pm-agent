"""Arb replay — runs paired paper orders through fill modes, evaluates paired outcome.

Both legs filled  -> valid arb
One leg filled    -> leg risk / invalid (exposed directional)
Neither filled    -> no execution
Resolution mismatch -> critical failure (flag separately)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row

from pm_agent.db.repo import db_conn
from pm_agent.replay.fill_models import FillMode, FillResult, simulate_fill
from pm_agent.replay.engine import PaperOrder, create_run, record_order
from pm_agent.scanners.stubs.arb import ArbOpportunity

log = logging.getLogger(__name__)


@dataclass
class ArbPairResult:
    arb_id: str
    matched_pair_id: int
    fill_mode: FillMode
    leg_a_filled: bool
    leg_b_filled: bool
    paired_fill: bool       # both legs filled
    net_profit: float       # only valid if paired_fill
    resolution_mismatch: bool = False
    notes: str = ""


@dataclass
class ArbGateReport:
    fill_mode: FillMode
    detected_opportunities: int = 0
    paired_filled: int = 0
    one_leg_filled: int = 0
    neither_filled: int = 0
    resolution_mismatches: int = 0
    net_ev: float = 0.0
    fill_rate: float = 0.0
    results: list[ArbPairResult] = field(default_factory=list)


async def create_arb_group(replay_run_id: int, matched_pair_id: int, arb_id: str) -> int:
    async with db_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO paper_order_groups
                    (group_type, matched_pair_id, replay_run_id, notes)
                VALUES ('arb', %(mp)s, %(rr)s, %(notes)s) RETURNING id
                """,
                dict(mp=matched_pair_id, rr=replay_run_id, notes=f"arb_id={arb_id}"),
            )
            row = await cur.fetchone()
            return row["id"]


async def record_arb_order(group_id: int, leg_role: str, order: PaperOrder, result: FillResult,
                          replay_run_id: int) -> int:
    """Record one leg of an arb pair, linked to its group."""
    async with db_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO paper_orders
                    (replay_run_id, signal_time, decision_time, venue_id, market_id, outcome_id,
                     side, limit_price, size, fill_mode, status, group_id, leg_role)
                VALUES (%(rid)s, %(sig)s, %(dec)s, %(venue)s, %(mid)s, %(oid)s,
                        %(side)s, %(lp)s, %(sz)s, %(fm)s, %(status)s, %(gid)s, %(leg)s)
                RETURNING id
                """,
                dict(rid=replay_run_id,
                     sig=order.signal_time, dec=order.decision_time,
                     venue=order.market_id.split(":")[0], mid=order.market_id, oid=order.outcome_id,
                     side=order.side, lp=order.limit_price, sz=order.size,
                     fm=order.fill_mode.value, status="filled" if result.filled else "rejected",
                     gid=group_id, leg=leg_role),
            )
            order_row = await cur.fetchone()
            order_id = order_row["id"]
            if result.filled:
                await cur.execute(
                    """
                    INSERT INTO paper_fills
                        (paper_order_id, simulated_fill_time, fill_price, fill_size, fill_reason, tape_trade_id)
                    VALUES (%(oid)s, now(), %(fp)s, %(fs)s, %(reason)s, %(tt)s)
                    """,
                    dict(oid=order_id, fp=result.fill_price, fs=result.fill_size,
                         reason=result.reason, tt=result.tape_trade_id),
                )
            return order_id


async def eval_arb_pair(opp: ArbOpportunity, fill_mode: FillMode, window_sec: float = 30.0) -> ArbPairResult:
    """Run both legs through one fill mode. Returns paired outcome."""
    # Replay uses decision_time = signal_time + small delta (models decision latency)
    decision_time = opp.signal_time + timedelta(seconds=1.0)

    leg_a_order = PaperOrder(
        market_id=opp.leg_a.market_id, outcome_id=opp.leg_a.outcome_id,
        side=opp.leg_a.side, limit_price=opp.leg_a.limit_price, size=opp.leg_a.size,
        signal_time=opp.signal_time, decision_time=decision_time, fill_mode=fill_mode,
    )
    leg_b_order = PaperOrder(
        market_id=opp.leg_b.market_id, outcome_id=opp.leg_b.outcome_id,
        side=opp.leg_b.side, limit_price=opp.leg_b.limit_price, size=opp.leg_b.size,
        signal_time=opp.signal_time, decision_time=decision_time, fill_mode=fill_mode,
    )

    fr_a = await simulate_fill(
        mode=fill_mode, market_id=leg_a_order.market_id, outcome_id=leg_a_order.outcome_id,
        side=leg_a_order.side, limit_price=leg_a_order.limit_price, size=leg_a_order.size,
        decision_time=decision_time, window_sec=window_sec,
    )
    fr_b = await simulate_fill(
        mode=fill_mode, market_id=leg_b_order.market_id, outcome_id=leg_b_order.outcome_id,
        side=leg_b_order.side, limit_price=leg_b_order.limit_price, size=leg_b_order.size,
        decision_time=decision_time, window_sec=window_sec,
    )

    paired = fr_a.filled and fr_b.filled
    # Arb payout: if YES wins on poly, pays $1*size; NO on kalshi loses (resolves 0). Net = payout - costs.
    # If YES loses on poly, NO wins on kalshi. Either way, one leg pays $1, other pays $0.
    # Net profit = 1*size - cost_a - cost_b (only meaningful if both filled)
    net_profit = 0.0
    if paired:
        contracts = opp.leg_a.size
        payout = 1.0 * contracts  # exactly one leg resolves YES
        cost_a = (fr_a.fill_price or 0) * (fr_a.fill_size or contracts)
        cost_b = (fr_b.fill_price or 0) * (fr_b.fill_size or contracts)
        net_profit = payout - cost_a - cost_b

    return ArbPairResult(
        arb_id=opp.arb_id,
        matched_pair_id=opp.matched_pair_id,
        fill_mode=fill_mode,
        leg_a_filled=fr_a.filled,
        leg_b_filled=fr_b.filled,
        paired_fill=paired,
        net_profit=net_profit,
        notes=f"leg_a:{fr_a.reason}; leg_b:{fr_b.reason}",
    )


async def arb_decision_gate(opportunities: list[ArbOpportunity]) -> dict:
    """Run all arb opportunities through 4 fill modes, evaluate paired outcomes.

    Decision gate (paper -> live):
      - tape_confirmed net EV > 0
      - conservative net EV > 0
      - paired fill rate > 70% (tape_confirmed)
      - 0 resolution mismatches
    """
    report: dict[str, ArbGateReport] = {}
    for mode in FillMode:
        r = ArbGateReport(fill_mode=mode)
        for opp in opportunities:
            try:
                res = await eval_arb_pair(opp, mode)
                r.detected_opportunities += 1
                if res.paired_fill:
                    r.paired_filled += 1
                    r.net_ev += res.net_profit
                elif res.leg_a_filled or res.leg_b_filled:
                    r.one_leg_filled += 1
                else:
                    r.neither_filled += 1
                if res.resolution_mismatch:
                    r.resolution_mismatches += 1
                r.results.append(res)
            except Exception as e:
                log.error("arb eval error %s: %s", opp.arb_id, e)
        r.fill_rate = r.paired_filled / r.detected_opportunities if r.detected_opportunities else 0
        report[mode.value] = r

    tape = report["tape_confirmed"]
    cons = report["conservative"]
    go_live = (
        tape.net_ev > 0
        and cons.net_ev > 0
        and tape.fill_rate > 0.70
        and tape.resolution_mismatches == 0
    )
    return {
        "modes": {
            m: {
                "detected": r.detected_opportunities,
                "paired_filled": r.paired_filled,
                "one_leg_filled": r.one_leg_filled,
                "neither_filled": r.neither_filled,
                "fill_rate": round(r.fill_rate, 3),
                "net_ev": round(r.net_ev, 4),
                "resolution_mismatches": r.resolution_mismatches,
            }
            for m, r in report.items()
        },
        "DECISION_GATE": "GO_LIVE" if go_live else "STAY_PAPER",
        "criteria": {
            "tape_net_ev_positive": tape.net_ev > 0,
            "conservative_net_ev_positive": cons.net_ev > 0,
            "fill_rate_gt_70pct": tape.fill_rate > 0.70,
            "zero_resolution_mismatches": tape.resolution_mismatches == 0,
        },
    }
