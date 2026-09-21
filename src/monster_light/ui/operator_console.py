"""Streamlit operator console for the governed Monster Light workflow."""

from __future__ import annotations

import io
import os
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import streamlit as st
from openai import OpenAI

from monster_light.application.ai_proposal import AIProposalService
from monster_light.application.approval import ApprovalService
from monster_light.application.market_evidence import (
    MarketEvidenceMismatch,
    StaleMarketEvidence,
)
from monster_light.application.model_proposal_adapter import ModelProposalAdapter
from monster_light.application.proposal import ProposalStatus
from monster_light.application.proposal_execution import ApprovedProposalExecutionService
from monster_light.demo.deterministic_execution import run_demo as run_deterministic_demo
from monster_light.domain.portfolio import Portfolio, PortfolioError
from monster_light.infrastructure.sqlite_approval_repository import SQLiteApprovalRepository
from monster_light.infrastructure.sqlite_audit_repository import SQLiteAuditRepository
from monster_light.infrastructure.sqlite_market_evidence_repository import SQLiteMarketEvidenceRepository
from monster_light.infrastructure.sqlite_proposal_repository import SQLiteProposalRepository
from monster_light.infrastructure.sqlite_repository import (
    PortfolioNotFound,
    SQLitePortfolioRepository,
)
from monster_light.infrastructure.yfinance_quote_provider import (
    MarketDataUnavailable,
    YFinanceQuoteProvider,
)


APP_TITLE = "Monster Light 2.0"
FUNDED_PORTFOLIO_ID = "operator-funded"
TIGHT_PORTFOLIO_ID = "operator-insufficient-cash"
DEFAULT_MODEL = os.getenv("MONSTER_LIGHT_MODEL", "gpt-5.6-luna")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _database_path() -> Path:
    configured = os.getenv("MONSTER_LIGHT_DB")
    path = Path(configured) if configured else Path(".monster-light/operator.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _open_database() -> sqlite3.Connection:
    connection = sqlite3.connect(_database_path(), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    SQLitePortfolioRepository(connection).initialize_schema()
    SQLiteProposalRepository(connection).initialize_schema()
    SQLiteApprovalRepository(connection).initialize_schema()
    SQLiteMarketEvidenceRepository(connection).initialize_schema()
    SQLiteAuditRepository(connection).initialize_schema()

    portfolios = SQLitePortfolioRepository(connection)
    _ensure_portfolio(portfolios, FUNDED_PORTFOLIO_ID, Decimal("10000.00"))
    _ensure_portfolio(portfolios, TIGHT_PORTFOLIO_ID, Decimal("100.00"))
    return connection


def _ensure_portfolio(
    portfolios: SQLitePortfolioRepository,
    portfolio_id: str,
    cash: Decimal,
) -> None:
    try:
        portfolios.load(portfolio_id)
    except PortfolioNotFound:
        portfolios.save(portfolio_id, Portfolio(cash))


def _portfolio_label(portfolio_id: str) -> str:
    if portfolio_id == FUNDED_PORTFOLIO_ID:
        return "Funded demo portfolio — $10,000 cash"
    return "Governance proof portfolio — $100 cash"


def _proposal_for_session(
    proposals: SQLiteProposalRepository,
    portfolio_id: str,
):
    proposal_id = st.session_state.get("proposal_id")
    if proposal_id:
        try:
            proposal = proposals.load(proposal_id)
        except Exception:
            st.session_state.pop("proposal_id", None)
        else:
            if proposal.portfolio_id == portfolio_id:
                return proposal

    existing = proposals.list_for_portfolio(portfolio_id)
    if existing:
        proposal = existing[-1]
        st.session_state["proposal_id"] = proposal.proposal_id
        return proposal
    return None


def _record_event(kind: str, message: str, **details) -> None:
    st.session_state["last_event"] = {
        "kind": kind,
        "message": message,
        "details": details,
    }


def _render_event() -> None:
    event = st.session_state.get("last_event")
    if not event:
        return

    writer = {
        "success": st.success,
        "warning": st.warning,
        "error": st.error,
        "info": st.info,
    }.get(event["kind"], st.info)
    writer(event["message"])
    if event["details"]:
        with st.expander("Execution evidence", expanded=False):
            st.json(event["details"])


def _render_authority_model() -> None:
    st.caption("Paper trading only. No broker connection and no real money.")
    st.markdown(
        "**AI proposes → human authorizes → system verifies fresh execution evidence "
        "and current portfolio reality → execute or reject → immutable audit**"
    )

    labels = (
        ("AI", "PROPOSE", "No portfolio mutation authority"),
        ("Human", "AUTHORIZE", "Permits an execution attempt"),
        ("System", "VERIFY / EXECUTE", "Deterministic controls decide"),
        ("Audit", "RECORD EVIDENCE", "Append-only outcome history"),
    )
    columns = st.columns(4)
    for column, (actor, action, detail) in zip(columns, labels):
        with column:
            st.metric(actor, action)
            st.caption(detail)


def _render_live_workflow(connection: sqlite3.Connection) -> None:
    portfolios = SQLitePortfolioRepository(connection)
    proposals = SQLiteProposalRepository(connection)
    evidence_repository = SQLiteMarketEvidenceRepository(connection)

    st.subheader("Live governed workflow")
    st.write(
        "A near-real-time market observation may inform a proposal. It never grants "
        "approval or execution authority."
    )

    portfolio_id = st.selectbox(
        "Portfolio",
        options=[FUNDED_PORTFOLIO_ID, TIGHT_PORTFOLIO_ID],
        format_func=_portfolio_label,
    )
    portfolio = portfolios.load(portfolio_id)
    left, right = st.columns(2)
    left.metric("Available cash", f"${portfolio.cash:,.2f}")
    right.metric("Positions", len(portfolio.positions))

    symbol = st.text_input("Symbol", value="AAPL").strip().upper()
    instruction = st.text_area(
        "Operator intent",
        value=(
            "Using only the trusted market context, propose a single illustrative BUY "
            "of exactly one share. Do not claim approval or execution."
        ),
        height=100,
    )

    if st.button("1. Generate AI proposal", type="primary", width="stretch"):
        try:
            with st.status("Building governed proposal...", expanded=True) as status:
                st.write("Fetching near-real-time market evidence...")
                grounding = YFinanceQuoteProvider().fetch(symbol)
                evidence_repository.append(grounding)
                st.write("Market evidence recorded. Requesting structured AI proposal...")
                with OpenAI(max_retries=0, timeout=30.0) as client:
                    proposal = ModelProposalAdapter(
                        client,
                        DEFAULT_MODEL,
                        AIProposalService(proposals),
                    ).propose(
                        instruction,
                        portfolio_id=portfolio_id,
                        evidence=grounding,
                    )
                status.update(
                    label="Proposal recorded as PENDING",
                    state="complete",
                    expanded=False,
                )
            st.session_state["proposal_id"] = proposal.proposal_id
            _record_event(
                "success",
                "AI proposal recorded as PENDING. No trade occurred.",
                proposal_id=proposal.proposal_id,
                grounding_evidence_id=grounding.evidence_id,
                source=grounding.source,
                symbol=grounding.symbol,
                price=str(grounding.price),
                observed_at=grounding.observed_at.isoformat(),
                retrieved_at=grounding.retrieved_at.isoformat(),
            )
            st.session_state["active_tab"] = "Live workflow"
            st.rerun()
        except (MarketDataUnavailable, ValueError, RuntimeError) as error:
            _record_event("error", str(error))
            st.session_state["active_tab"] = "Live workflow"
            st.rerun()
        except Exception as error:
            _record_event(
                "error",
                "Proposal generation failed before authority changed.",
                error_type=type(error).__name__,
                detail=str(error),
            )
            st.session_state["active_tab"] = "Live workflow"
            st.rerun()

    proposal = _proposal_for_session(proposals, portfolio_id)
    if proposal is None:
        _render_event()
        st.info("No proposal exists for this portfolio yet.")
        return

    st.divider()
    st.markdown("#### Current proposal")
    status_columns = st.columns(5)
    status_columns[0].metric("Status", proposal.status.value)
    status_columns[1].metric("Side", proposal.side.value)
    status_columns[2].metric("Symbol", proposal.symbol)
    status_columns[3].metric("Quantity", proposal.quantity)
    status_columns[4].metric("Price", f"${proposal.price:,.2f}")
    st.caption(f"Proposal ID: {proposal.proposal_id}")
    st.write(proposal.rationale)

    if proposal.status is ProposalStatus.PENDING:
        approve_column, reject_column = st.columns(2)
        with approve_column:
            if st.button(
                "2. Approve proposal",
                type="primary",
                width="stretch",
            ):
                try:
                    approval = ApprovalService(proposals).approve(
                        proposal.proposal_id,
                        "operator-demo",
                    )
                    _record_event(
                        "success",
                        "Human approval recorded. Execution has still not occurred.",
                        proposal_id=proposal.proposal_id,
                        approval_id=approval.approval_id,
                        approved_at=approval.approved_at.isoformat(),
                    )
                except Exception as error:
                    _record_event(
                        "error",
                        "Approval failed.",
                        error_type=type(error).__name__,
                        detail=str(error),
                    )
                st.session_state["active_tab"] = "Live workflow"
                st.rerun()

        with reject_column:
            rejection_reason = st.text_input(
                "Rejection reason",
                value="Operator rejected recommendation.",
            )
            if st.button("Reject proposal", width="stretch"):
                try:
                    proposals.reject(proposal.proposal_id, rejection_reason)
                    _record_event(
                        "warning",
                        "Proposal rejected by the operator. No execution is possible.",
                        proposal_id=proposal.proposal_id,
                    )
                except Exception as error:
                    _record_event(
                        "error",
                        "Rejection failed.",
                        error_type=type(error).__name__,
                        detail=str(error),
                    )
                st.session_state["active_tab"] = "Live workflow"
                st.rerun()

    elif proposal.status is ProposalStatus.APPROVED:
        st.info(
            "Approval permits an attempt. The system must still obtain fresh evidence "
            "and validate current portfolio state."
        )
        if st.button(
            "3. Verify fresh evidence and attempt execution",
            type="primary",
            width="stretch",
        ):
            fresh = None
            try:
                fresh = YFinanceQuoteProvider().fetch(proposal.symbol)
                evidence_repository.append(fresh)
                ApprovedProposalExecutionService(
                    proposals,
                    maximum_age=timedelta(minutes=5),
                    clock=_utc_now,
                ).execute(proposal.proposal_id, fresh.evidence_id)
            except MarketEvidenceMismatch as error:
                _record_event(
                    "warning",
                    "Execution blocked: the fresh market price no longer matches the "
                    "approved immutable proposal price.",
                    proposal_id=proposal.proposal_id,
                    approved_price=str(proposal.price),
                    fresh_evidence_id=fresh.evidence_id if fresh else None,
                    fresh_price=str(fresh.price) if fresh else None,
                    reason=type(error).__name__,
                )
            except StaleMarketEvidence as error:
                _record_event(
                    "warning",
                    "Execution blocked: market evidence was stale.",
                    proposal_id=proposal.proposal_id,
                    reason=type(error).__name__,
                )
            except PortfolioError as error:
                _record_event(
                    "warning",
                    "Execution rejected by deterministic portfolio controls. "
                    "The approved proposal remains approved.",
                    proposal_id=proposal.proposal_id,
                    reason=type(error).__name__,
                    detail=str(error),
                )
            except MarketDataUnavailable as error:
                _record_event("error", str(error))
            except Exception as error:
                _record_event(
                    "error",
                    "Execution attempt failed safely.",
                    error_type=type(error).__name__,
                    detail=str(error),
                )
            else:
                _record_event(
                    "success",
                    "Execution succeeded only after fresh evidence and deterministic "
                    "portfolio validation passed.",
                    proposal_id=proposal.proposal_id,
                    execution_evidence_id=fresh.evidence_id,
                    price=str(fresh.price),
                )
            st.session_state["active_tab"] = "Live workflow"
            st.rerun()

    elif proposal.status is ProposalStatus.EXECUTED:
        st.success("Proposal executed. The successful consequence is at-most-once.")

    elif proposal.status is ProposalStatus.REJECTED:
        st.warning(f"Proposal rejected: {proposal.rejection_reason}")

    _render_event()
    _render_recent_audit(connection, proposal.proposal_id)


def _render_recent_audit(
    connection: sqlite3.Connection,
    proposal_id: str | None = None,
) -> None:
    sql = """
        SELECT timestamp, outcome, reason_code, symbol, quantity, price,
               proposal_id, approval_id, market_evidence_id
        FROM trade_audits
    """
    parameters: tuple[str, ...] = ()
    if proposal_id is not None:
        sql += " WHERE proposal_id = ?"
        parameters = (proposal_id,)
    sql += " ORDER BY timestamp DESC LIMIT 20"
    rows = connection.execute(sql, parameters).fetchall()

    st.markdown("#### Immutable execution audit")
    if not rows:
        st.caption("No execution audit exists for this proposal yet.")
        return
    st.dataframe(
        [dict(row) for row in rows],
        width="stretch",
        hide_index=True,
    )


def _render_governance_proof() -> None:
    st.subheader("Deterministic governance proofs")
    st.write(
        "The same governed execution path proves both outcomes: controls allow a "
        "valid approved trade and block an approved trade that current portfolio "
        "reality cannot support."
    )

    if st.button("Run deterministic control proofs", type="primary"):
        with redirect_stdout(io.StringIO()):
            success, failure = run_deterministic_demo()
        st.session_state["control_proofs"] = {
            "success": {
                "proposal_status_before": success.before.status.value,
                "proposal_status_after": success.after.status.value,
                "audit_outcome": success.audit["outcome"],
                "reason_code": success.audit["reason_code"],
                "cash_before": str(success.portfolio_before.cash),
                "cash_after": str(success.portfolio_after.cash),
                "quantity_before": success.portfolio_before.quantity_for("AAPL"),
                "quantity_after": success.portfolio_after.quantity_for("AAPL"),
                "proposal_id": success.before.proposal_id,
                "approval_id": success.approval.approval_id,
                "execution_evidence_id": success.audit["market_evidence_id"],
            },
            "blocked": {
                "proposal_status_before": failure.before.status.value,
                "proposal_status_after": failure.after.status.value,
                "audit_outcome": failure.audit["outcome"],
                "reason_code": failure.audit["reason_code"],
                "cash_before": str(failure.portfolio_before.cash),
                "cash_after": str(failure.portfolio_after.cash),
                "quantity_before": failure.portfolio_before.quantity_for("AAPL"),
                "quantity_after": failure.portfolio_after.quantity_for("AAPL"),
                "proposal_id": failure.before.proposal_id,
                "approval_id": failure.approval.approval_id,
                "execution_evidence_id": failure.audit["market_evidence_id"],
            },
        }
        st.session_state["active_tab"] = "Governance proof"
        st.rerun()

    proofs = st.session_state.get("control_proofs")
    if not proofs:
        return

    allowed = proofs["success"]
    blocked = proofs["blocked"]
    allowed_column, blocked_column = st.columns(2)

    with allowed_column:
        st.success("ALLOWED — deterministic controls passed")
        metrics = st.columns(2)
        metrics[0].metric("Proposal", allowed["proposal_status_after"])
        metrics[1].metric("Audit", allowed["audit_outcome"])
        metrics[0].metric("Cash after", f"${Decimal(allowed['cash_after']):,.2f}")
        metrics[1].metric("AAPL shares", allowed["quantity_after"])
        st.caption(
            "Approval permitted an attempt; fresh evidence and portfolio validation "
            "passed, so the governed consequence occurred."
        )

    with blocked_column:
        st.error("BLOCKED — InsufficientCash")
        metrics = st.columns(2)
        metrics[0].metric("Proposal", blocked["proposal_status_after"])
        metrics[1].metric("Audit", blocked["audit_outcome"])
        metrics[0].metric("Cash after", f"${Decimal(blocked['cash_after']):,.2f}")
        metrics[1].metric("AAPL shares", blocked["quantity_after"])
        st.caption(
            "Approval remained historical truth, but authoritative portfolio state "
            "prevented the consequence."
        )

    with st.expander("Control proof evidence", expanded=False):
        st.json(proofs)


def _render_evidence_view(connection: sqlite3.Connection) -> None:
    st.subheader("Evidence and audit trail")
    evidence_rows = connection.execute(
        """
        SELECT evidence_id, source, symbol, price, observed_at, retrieved_at
        FROM market_evidence
        ORDER BY retrieved_at DESC
        LIMIT 25
        """
    ).fetchall()

    if evidence_rows:
        st.markdown("#### Market evidence")
        st.dataframe(
            [dict(row) for row in evidence_rows],
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption("No market evidence has been recorded yet.")

    _render_recent_audit(connection)


def main() -> None:
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon="⚡",
        layout="wide",
    )
    st.title(APP_TITLE)
    _render_authority_model()
    connection = _open_database()
    try:
        live_tab, proof_tab, evidence_tab = st.tabs(
            ["Live workflow", "Governance proof", "Evidence & audit"],
            default=st.session_state.get("active_tab", "Live workflow"),
        )
        with live_tab:
            _render_live_workflow(connection)
        with proof_tab:
            _render_governance_proof()
        with evidence_tab:
            _render_evidence_view(connection)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
