#!/usr/bin/env python3
"""
Simple Alpaca MCP Server

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
import asyncio
import logging
from typing import Any, Dict, List, Union

import alpaca_trade_api as tradeapi

from mcp.server import Server
from mcp.types import Tool, TextContent
from mcp.server.http import StreamableHTTPSessionManager
import uvicorn

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alpaca-mcp-server")

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

logger.info("=== Alpaca MCP Server config ===")
logger.info(f"ALPACA_BASE_URL: {ALPACA_BASE_URL}")
logger.info(f"ALPACA key present: {bool(ALPACA_KEY)}")
logger.info(f"ALPACA secret present: {bool(ALPACA_SECRET)}")
logger.info("================================")

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
# MCP server
# ---------------------------------------------------------------------------

app = Server("alpaca-mcp-service")

TextLike = TextContent  # for type alias


@app.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="get_quote",
            description="Get real-time quote for a symbol from Alpaca.",
            inputSchema={
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        ),
        Tool(
            name="get_account",
            description="Get Alpaca account information.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_positions",
            description="Get all open positions.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="place_order",
            description="Place a market order with basic risk checks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "qty": {"type": "integer"},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "time_in_force": {
                        "type": "string",
                        "enum": ["day", "gtc"],
                        "default": "day",
                    },
                },
                "required": ["symbol", "qty", "side"],
            },
        ),
        Tool(
            name="close_position",
            description="Close the entire position for a given symbol.",
            inputSchema={
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        ),
        Tool(
            name="analyze_portfolio",
            description="Summarize portfolio P&L and positions (text only).",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextLike]:
    try:
        if name == "get_quote":
            return await get_quote(arguments["symbol"])
        if name == "get_account":
            return await get_account()
        if name == "get_positions":
            return await get_positions()
        if name == "place_order":
            return await place_order(
                symbol=arguments["symbol"],
                qty=arguments["qty"],
                side=arguments["side"],
                time_in_force=arguments.get("time_in_force", "day"),
            )
        if name == "close_position":
            return await close_position(arguments["symbol"])
        if name == "analyze_portfolio":
            return await analyze_portfolio()

        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        logger.exception("Tool %s failed", name)
        return [TextContent(type="text", text=f"Error running tool {name}: {e}")]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def get_quote(symbol: str) -> List[TextLike]:
    symbol = symbol.upper()
    try:
        snap = alpaca.get_snapshot(symbol)
        price = (
            float(snap.latest_trade.p)
            if getattr(snap, "latest_trade", None) is not None
            else None
        )
        data = {"symbol": symbol, "price": price}
        return [TextContent(type="text", text=json.dumps(data, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error getting quote: {e}")]


async def get_account() -> List[TextLike]:
    try:
        acct = alpaca.get_account()
        data = {
            "status": acct.status,
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
        }
        return [TextContent(type="text", text=json.dumps(data, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error getting account: {e}")]


async def get_positions() -> List[TextLike]:
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
        return [TextContent(type="text", text=json.dumps(data, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error getting positions: {e}")]


async def place_order(
    symbol: str, qty: int, side: str, time_in_force: str
) -> List[TextLike]:
    symbol = symbol.upper()

    if not validate_symbol(symbol):
        return [
            TextContent(
                type="text",
                text=f"ERROR: {symbol} is not in the allowed universe (risk check failed).",
            )
        ]

    if qty > MAX_POSITION_SIZE:
        return [
            TextContent(
                type="text",
                text=f"ERROR: qty={qty} exceeds MAX_POSITION_SIZE={MAX_POSITION_SIZE}",
            )
        ]

    try:
        snap = alpaca.get_snapshot(symbol)
        price = (
            float(snap.latest_trade.p)
            if getattr(snap, "latest_trade", None) is not None
            else None
        )
        if not price:
            return [TextContent(type="text", text="ERROR: Could not fetch latest price")]

        notional = qty * price
        if side.lower() == "buy" and notional > MAX_POSITION_VALUE:
            return [
                TextContent(
                    type="text",
                    text=(
                        f"ERROR: Order value ${notional:,.2f} exceeds "
                        f"MAX_POSITION_VALUE ${MAX_POSITION_VALUE:,.2f}"
                    ),
                )
            ]

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
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=f"ERROR placing order: {e}")]


async def close_position(symbol: str) -> List[TextLike]:
    symbol = symbol.upper()
    try:
        alpaca.close_position(symbol)
        return [TextContent(type="text", text=f"Closed position in {symbol}.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error closing position: {e}")]


async def analyze_portfolio() -> List[TextLike]:
    """Text-only portfolio summary."""
    try:
        positions = alpaca.list_positions()
        if not positions:
            return [TextContent(type="text", text="No open positions.")]

        symbols = [p.symbol for p in positions]
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

        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error analyzing portfolio: {e}")]


# ---------------------------------------------------------------------------
# Entry points: stdio and HTTP
# ---------------------------------------------------------------------------


async def main_stdio() -> None:
    """StdIO mode – used by Claude Desktop."""
    from mcp.server.stdio import stdio_server

    logger.info("Starting Alpaca MCP Server (stdio mode)...")

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream, write_stream, app.create_initialization_options()
        )


async def run_http_server() -> None:
    """HTTP mode – used by Claude web/mobile."""
    port = int(os.getenv("PORT", "8000"))

    logger.info("Starting Alpaca MCP Server (HTTP mode)...")
    logger.info(f"Listening on 0.0.0.0:{port}")

    manager = StreamableHTTPSessionManager(app, stateless=False)

    uvicorn.run(
        manager.as_asgi_app(),
        host="0.0.0.0",
        port=port,
    )


if __name__ == "__main__":
    import sys

    if "--http" in sys.argv:
        asyncio.run(run_http_server())
    else:
        asyncio.run(main_stdio())

