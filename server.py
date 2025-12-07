#!/usr/bin/env python3
"""
Simple Alpaca MCP Server using FastMCP

Tools exposed:
- get_quote
- get_account
- get_positions
- place_order
- close_position
- analyze_portfolio (text summary only)

Works with both paper and live trading depending on ALPACA_BASE_URL.
"""

import os
import json
import logging
from typing import List, Optional

import alpaca_trade_api as tradeapi
import requests  # NEW: for sending analytics to your app

from fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alpaca-mcp-service")

# ---------------------------------------------------------------------------
# Environment / config
# ---------------------------------------------------------------------------

# Prefer ALPACA_* but fall back to APCA_*
ALPACA_KEY = (
    os.getenv("ALPACA_API_KEY")
    or os.getenv("ALPACA_API_KEY_ID")
    or os.getenv("APCA_API_KEY_ID")
)

ALPACA_SECRET = (
    os.getenv("ALPACA_SECRET_KEY")
    or os.getenv("ALPACA_API_SECRET_KEY")
    or os.getenv("APCA_API_SECRET_KEY")
)

# Default to paper if nothing set
ALPACA_BASE_URL = (
    os.getenv("ALPACA_BASE_URL")
    or os.getenv("APCA_API_BASE_URL")
    or "https://paper-api.alpaca.markets"
)

MAX_POSITION_SIZE = int(os.getenv("MAX_POSITION_SIZE", "1000"))
MAX_POSITION_VALUE = float(os.getenv("MAX_POSITION_VALUE", "10000"))
ALLOWED_SYMBOLS_FILE = os.getenv("ALLOWED_SYMBOLS_FILE", "data/universe_liquid.txt")

# NEW: Analytics config for your IW Positions app
ANALYTICS_ENDPOINT = os.getenv("ANALYTICS_ENDPOINT")  # e.g. https://iw-positions-app.vercel.app/api/analytics
ANALYTICS_TOKEN = os.getenv("ANALYTICS_TOKEN")        # should match APP_TOKEN in the app

logger.info("=== Alpaca MCP Server config ===")
logger.info(f"ALPACA_BASE_URL: {ALPACA_BASE_URL}")
logger.info(f"ALPACA key present: {bool(ALPACA_KEY)}")
logger.info(f"ALPACA secret present: {bool(ALPACA_SECRET)}")
logger.info(f"ANALYTICS_ENDPOINT set: {bool(ANALYTICS_ENDPOINT)}")
logger.info("================================")


# ---------------------------------------------------------------------------
# Analytics helper
# ---------------------------------------------------------------------------

def send_analytics(event_type: str, data: dict, chart_base64: Optional[str] = None) -> None:
    """
    Send analytics payload to the IW Positions app.

    The app expects:
      {
        "type": "portfolio_analysis" | "price_chart" | ...,
        "data": { ...metrics... },
        "chart_data": "<base64 png>" | null
      }
    """
    if not ANALYTICS_ENDPOINT:
        logger.debug("ANALYTICS_ENDPOINT not set; skipping analytics send")
        return

    payload = {
        "type": event_type,
        "data": data,
        "chart_data": chart_base64,
    }

    headers = {"Content-Type": "application/json"}
    if ANALYTICS_TOKEN:
        headers["x-app-token"] = ANALYTICS_TOKEN

    try:
        resp = requests.post(
            ANALYTICS_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=5,
        )
        if resp.ok:
            logger.info("Analytics event '%s' sent successfully", event_type)
        else:
            logger.warning(
                "Analytics send failed (%s): %s",
                resp.status_code,
                resp.text[:200],
            )
    except Exception as e:
        logger.warning("Analytics send error: %s", e)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

alpaca: tradeapi.REST

try:
    alpaca = tradeapi.REST(
        key_id=ALPACA_KEY,
        secret_key=ALPACA_SECRET,
        base_url=ALPACA_BASE_URL,
        api_version="v2",
    )
    acct = alpaca.get_account()
    logger.info(
        "Alpaca auth OK at startup: status=%s equity=%s buying_power=%s",
        acct.status,
        acct.equity,
        acct.buying_power,
    )
except Exception as e:
    logger.error("Alpaca auth FAILED at startup: %s", e)

# ---------------------------------------------------------------------------
# Allowed symbols (optional universe file)
# ---------------------------------------------------------------------------


def load_allowed_symbols() -> set:
    try:
        with open(ALLOWED_SYMBOLS_FILE, "r") as f:
            return {line.strip().upper() for line in f if line.strip()}
    except FileNotFoundError:
        logger.info(
            "No universe file %s found; all symbols will be allowed.", ALLOWED_SYMBOLS_FILE
        )
        return set()


ALLOWED_SYMBOLS = load_allowed_symbols()


def validate_symbol(symbol: str) -> bool:
    if not ALLOWED_SYMBOLS:
        return True
    return symbol.upper() in ALLOWED_SYMBOLS


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("alpaca-mcp-service")

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(description="Get real-time quote for a symbol from Alpaca.")
async def get_quote(symbol: str) -> str:
    symbol = symbol.upper()
    try:
        snap = alpaca.get_snapshot(symbol)
        price = (
            float(snap.latest_trade.p)
            if getattr(snap, "latest_trade", None) is not None
            else None
        )
        data = {"symbol": symbol, "price": price}
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error getting quote: {e}"


@mcp.tool(description="Get Alpaca account information.")
async def get_account() -> str:
    try:
        acct = alpaca.get_account()
        data = {
            "status": acct.status,
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
        }
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error getting account: {e}"


@mcp.tool(description="Get all open positions.")
async def get_positions() -> str:
    try:
        positions = alpaca.list_positions()
        data = [
            {
                "symbol": p.symbol,
                "qty": int(p.qty),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
            }
            for p in positions
        ]
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error getting positions: {e}"


@mcp.tool(description="Place a market order with basic risk checks.")
async def place_order(
    symbol: str, qty: int, side: str, time_in_force: str = "day"
) -> str:
    symbol = symbol.upper()

    if not validate_symbol(symbol):
        return (
            f"ERROR: {symbol} is not in the allowed universe (risk check failed)."
        )

    if qty > MAX_POSITION_SIZE:
        return (
            f"ERROR: qty={qty} exceeds MAX_POSITION_SIZE={MAX_POSITION_SIZE}"
        )

    try:
        snap = alpaca.get_snapshot(symbol)
        price = (
            float(snap.latest_trade.p)
            if getattr(snap, "latest_trade", None) is not None
            else None
        )
        if not price:
            return "ERROR: Could not fetch latest price"

        notional = qty * price
        if side.lower() == "buy" and notional > MAX_POSITION_VALUE:
            return (
                f"ERROR: Order value ${notional:,.2f} exceeds "
                f"MAX_POSITION_VALUE ${MAX_POSITION_VALUE:,.2f}"
            )

        order = alpaca.submit_order(
            symbol=symbol,
            qty=qty,
            side=side.lower(),
            type="market",
            time_in_force=time_in_force.lower(),
        )

        result = {
            "status": "submitted",
            "order_id": order.id,
            "symbol": order.symbol,
            "qty": int(order.qty),
            "side": order.side,
            "created_at": str(order.created_at),
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"ERROR placing order: {e}"


@mcp.tool(description="Close entire position for a given symbol.")
async def close_position(symbol: str) -> str:
    symbol = symbol.upper()
    try:
        alpaca.close_position(symbol)
        return f"Closed position in {symbol}."
    except Exception as e:
        return f"Error closing position: {e}"


@mcp.tool(description="Summarize portfolio P&L and positions (text only).")
async def analyze_portfolio() -> str:
    """Text-only portfolio summary + send analytics to the app."""
    try:
        positions = alpaca.list_positions()
        if not positions:
            return "No open positions."

        symbols: List[str] = [p.symbol for p in positions]
        values = [float(p.market_value) for p in positions]
        pnl = [float(p.unrealized_pl) for p in positions]
        pnl_pct = [float(p.unrealized_plpc) * 100 for p in positions]

        total_value = sum(values)
        total_pnl = sum(pnl)
        invested = total_value - total_pnl
        total_pnl_pct = (total_pnl / invested * 100) if invested > 0 else 0.0

        winners = len([x for x in pnl if x > 0])
        losers = len([x for x in pnl if x < 0])

        lines = [
            "Portfolio summary:",
            "",
            f"Total value: ${total_value:,.2f}",
            f"Total P&L:   ${total_pnl:,.2f} ({total_pnl_pct:+.2f}%)",
            f"Positions:   {len(positions)} (winners: {winners}, losers: {losers})",
            "",
            "Per-position:",
        ]

        for sym, v, p, pct in zip(symbols, values, pnl, pnl_pct):
            lines.append(
                f"- {sym}: value=${v:,.2f}, P&L=${p:,.2f} ({pct:+.2f}%)"
            )

        # --- NEW: send compact analytics payload to your app ---
        analytics_payload = {
            "total_value": total_value,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "position_count": len(positions),
            "winners": winners,
            "losers": losers,
            "positions": [
                {
                    "symbol": sym,
                    "market_value": v,
                    "unrealized_pl": p,
                    "unrealized_plpc": pct,
                }
                for sym, v, p, pct in zip(symbols, values, pnl, pnl_pct)
            ],
        }

        send_analytics("portfolio_analysis", analytics_payload)

        return "\n".join(lines)
    except Exception as e:
        return f"Error analyzing portfolio: {e}"


# ---------------------------------------------------------------------------
# Entrypoint: stdio vs HTTP
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if "--http" in sys.argv:
        # HTTP transport for Render / Claude Mobile
        port = int(os.getenv("PORT", "8000"))
        logger.info(
            f"Starting Alpaca MCP Server (HTTP) on 0.0.0.0:{port}..."
        )
        mcp.run(
            transport="http",  # FastMCP HTTP transport
            host="0.0.0.0",
            port=port,
        )
    else:
        # Default: stdio transport for Claude Desktop
        logger.info("Starting Alpaca MCP Server (stdio)...")
        mcp.run()
