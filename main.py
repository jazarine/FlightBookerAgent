"""
FlightBookerAgent — A2A-compatible flight search and booking agent.

Receives task delegations from Switchboard, searches flights via Duffel API,
books the cheapest option, and reports actual_spend back to Switchboard.

A2A endpoint: POST /a2a/agents/{agent_id}
  JSON-RPC 2.0: tasks/send, tasks/get
"""

from __future__ import annotations

import os
import json
import asyncio
import uuid
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="FlightBookerAgent")

DUFFEL_API_KEY     = os.getenv("DUFFEL_API_KEY", "")
SWITCHBOARD_URL    = os.getenv("SWITCHBOARD_URL", "https://switchboard-api-production-8c8c.up.railway.app")
AGENT_API_KEY      = os.getenv("AGENT_API_KEY", "")   # Switchboard API key for this agent
DUFFEL_BASE        = "https://api.duffel.com"
DUFFEL_HEADERS     = {
    "Authorization": f"Bearer {DUFFEL_API_KEY}",
    "Duffel-Version": "v2",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# In-memory task store
tasks: dict[str, dict] = {}


# ── Root + Health ────────────────────────────────────────────────────────────


@app.get("/")
async def root():
    return {
        "agent": "FlightBookerAgent",
        "status": "ok",
        "endpoints": ["/health", "/a2a", "/task", "/.well-known/agent.json"],
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent": "FlightBookerAgent",
        "duffel_configured": bool(DUFFEL_API_KEY),
        "switchboard_configured": bool(AGENT_API_KEY),
    }


# ── A2A endpoint ──────────────────────────────────────────────────────────


@app.post("/a2a")
async def a2a(request: Request, background_tasks: BackgroundTasks):
    """JSON-RPC 2.0 A2A task endpoint."""
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})
    rpc_id = body.get("id")

    if method == "tasks/send":
        task = params.get("task", {})
        task_id = task.get("id") or str(uuid.uuid4())
        message = task.get("message", {})
        text = ""
        for part in message.get("parts", []):
            if part.get("type") == "text":
                text += part.get("text", "")

        # Parse spend token from metadata
        spend_token = params.get("spend_token") or task.get("metadata", {}).get("spend_token")

        tasks[task_id] = {
            "id": task_id,
            "status": {"state": "working"},
            "spend_token": spend_token,
            "task_description": text,
            "created_at": datetime.utcnow().isoformat(),
        }

        # Run booking in background
        background_tasks.add_task(run_flight_booking, task_id, text, spend_token)

        return JSONResponse({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "id": task_id,
                "status": {"state": "working"},
                "message": {"role": "agent", "parts": [{"type": "text", "text": "Searching for flights..."}]},
            }
        })

    elif method == "tasks/get":
        task_id = params.get("id")
        task = tasks.get(task_id)
        if not task:
            return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32600, "message": "Task not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": task})

    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": "Method not found"}})


# ── Also accept Switchboard-style direct dispatch ─────────────────────────


@app.post("/task")
async def receive_task(request: Request, background_tasks: BackgroundTasks):
    """Simple task dispatch (non-A2A). Accepts Switchboard delegation payload."""
    body = await request.json()
    task_id = str(uuid.uuid4())
    description = body.get("task_description", "")
    spend_token = body.get("spend_token")

    tasks[task_id] = {
        "id": task_id,
        "status": {"state": "working"},
        "spend_token": spend_token,
        "task_description": description,
    }

    background_tasks.add_task(run_flight_booking, task_id, description, spend_token)
    return {"task_id": task_id, "status": "working"}


@app.get("/task/{task_id}")
async def get_task(task_id: str):
    task = tasks.get(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return task


# ── Flight booking logic ──────────────────────────────────────────────────


def parse_flight_request(description: str) -> dict:
    """
    Parse natural language flight request into structured params.
    E.g. "Book flight SFO to JFK on April 10 budget $500"
    Returns dict with origin, destination, date, budget.
    Falls back to sensible defaults for demo.
    """
    import re
    desc = description.upper()

    # Airport codes
    airports = re.findall(r'\b([A-Z]{3})\b', desc)
    origin = airports[0] if len(airports) > 0 else "LHR"
    destination = airports[1] if len(airports) > 1 else "JFK"

    # Date — look for month + day
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
               "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    date = None
    for m, n in months.items():
        match = re.search(rf'{m}\w*\s+(\d+)', desc)
        if match:
            day = int(match.group(1))
            year = 2026
            date = f"{year}-{n:02d}-{day:02d}"
            break
    if not date:
        # Default: 7 days from now
        from datetime import timedelta
        date = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d")

    # Budget
    budget_match = re.search(r'\$(\d+)', description)
    budget = float(budget_match.group(1)) if budget_match else 500.0

    return {"origin": origin, "destination": destination, "date": date, "budget": budget}


async def search_flights(origin: str, destination: str, date: str, passenger: dict | None = None) -> tuple[list[dict], str]:
    """Search flights via Duffel API. Returns (offers sorted by price, passenger_id)."""
    passenger_payload = {
        "type": "adult",
        "title": passenger.get("title", "mr") if passenger else "mr",
        "gender": passenger.get("gender", "m") if passenger else "m",
        "given_name": passenger.get("given_name", "Jaz") if passenger else "Jaz",
        "family_name": passenger.get("family_name", "Jamal") if passenger else "Jamal",
        "born_on": passenger.get("born_on", "1990-01-01") if passenger else "1990-01-01",
        "email": passenger.get("email", "jaz@switchboard.ai") if passenger else "jaz@switchboard.ai",
        "phone_number": passenger.get("phone_number", "+14155550001") if passenger else "+14155550001",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        # Create offer request WITH passenger details
        r = await client.post(
            f"{DUFFEL_BASE}/air/offer_requests",
            headers=DUFFEL_HEADERS,
            json={
                "data": {
                    "slices": [{"origin": origin, "destination": destination, "departure_date": date}],
                    "passengers": [passenger_payload],
                    "cabin_class": "economy",
                }
            }
        )
        r.raise_for_status()
        data = r.json()["data"]
        request_id = data["id"]
        passenger_id = data["passengers"][0]["id"]

        # Get offers
        r2 = await client.get(
            f"{DUFFEL_BASE}/air/offers?offer_request_id={request_id}&sort=total_amount&limit=5",
            headers=DUFFEL_HEADERS,
        )
        r2.raise_for_status()
        return r2.json()["data"], passenger_id


async def book_flight(offer_id: str, passenger_id: str, passenger: dict, offer_amount: str = "0", offer_currency: str = "USD") -> dict:
    """Book a flight offer. Returns order details."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{DUFFEL_BASE}/air/orders",
            headers=DUFFEL_HEADERS,
            json={
                "data": {
                    "type": "instant",
                    "selected_offers": [offer_id],
                    "passengers": [{
                        "id": passenger_id,
                        "title": passenger.get("title", "mr"),
                        "gender": passenger.get("gender", "m"),
                        "given_name": passenger.get("given_name", "Jaz"),
                        "family_name": passenger.get("family_name", "Jamal"),
                        "born_on": passenger.get("born_on", "1990-01-01"),
                        "email": passenger.get("email", "jaz@switchboard.ai"),
                        "phone_number": passenger.get("phone_number", "+14155550001"),
                    }],
                    "payments": [{"type": "balance", "currency": offer_currency, "amount": offer_amount}],
                }
            }
        )
        if r.status_code != 200:
            print(f"[DUFFEL] Booking error: {r.text}")
        r.raise_for_status()
        return r.json()["data"]


async def report_to_switchboard(spend_token: str, actual_spend: float, result: str):
    """POST /complete to Switchboard with actual spend."""
    if not spend_token or not AGENT_API_KEY:
        print(f"[SWITCHBOARD] Skipping report — token={spend_token} key_set={bool(AGENT_API_KEY)}")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{SWITCHBOARD_URL}/complete",
            headers={"Authorization": f"Bearer {AGENT_API_KEY}", "Content-Type": "application/json"},
            json={"token": spend_token, "actual_spend": actual_spend, "result": result},
        )
        print(f"[SWITCHBOARD] Reported: {r.status_code} actual_spend=${actual_spend:.2f}")


async def run_flight_booking(task_id: str, description: str, spend_token: Optional[str]):
    """Main booking flow — runs in background."""
    print(f"[TASK {task_id[:8]}] Starting: {description}")

    try:
        params = parse_flight_request(description)
        print(f"[TASK {task_id[:8]}] Parsed: {params}")

        # Default passenger profile (replace with user profile lookup later)
        passenger = {
            "title": "mr", "gender": "m",
            "given_name": "Jaz", "family_name": "Jamal",
            "born_on": "1990-01-01",
            "email": "jaz@switchboard.ai",
            "phone_number": "+14155550001",
        }

        # Search flights (passenger attached to offer request)
        offers, passenger_id = await search_flights(
            params["origin"], params["destination"], params["date"], passenger
        )

        if not offers:
            tasks[task_id]["status"] = {"state": "failed"}
            tasks[task_id]["result"] = "No flights found for this route/date."
            await report_to_switchboard(spend_token, 0, "No flights found")
            return

        # In sandbox: prefer Duffel Airways (only test airline that supports booking)
        # In live mode: pick cheapest within budget
        duffel_offers = [o for o in offers if "duffel" in o["owner"]["name"].lower()]
        cheapest = None
        if duffel_offers:
            cheapest = duffel_offers[0]  # Duffel Airways in sandbox
        else:
            for offer in offers:
                price = float(offer["total_amount"])
                if price <= params["budget"]:
                    cheapest = offer
                    break

        if not cheapest:
            cheapest = offers[0]  # take cheapest even if over budget

        price = float(cheapest["total_amount"])
        airline = cheapest["owner"]["name"]
        slices = cheapest["slices"]
        departure = slices[0]["segments"][0]["departing_at"]
        arrival = slices[0]["segments"][-1]["arriving_at"]
        flight_num = slices[0]["segments"][0]["operating_carrier_flight_number"]

        # Book the flight (sandbox — no real charge)
        order = await book_flight(
            cheapest["id"], passenger_id, passenger,
            offer_amount=cheapest["total_amount"],
            offer_currency=cheapest["total_currency"],
        )
        booking_ref = order.get("booking_reference", "DEMO01")

        result_text = (
            f"✅ Booked: {airline} flight {flight_num}\n"
            f"   {params['origin']} → {params['destination']}\n"
            f"   Departure: {departure}\n"
            f"   Arrival: {arrival}\n"
            f"   Fare: ${price:.2f}\n"
            f"   Booking ref: {booking_ref}"
        )

        tasks[task_id].update({
            "status": {"state": "completed"},
            "result": result_text,
            "actual_spend": price,
            "booking_reference": booking_ref,
            "airline": airline,
            "flight_number": flight_num,
            "departure": departure,
            "arrival": arrival,
        })

        print(f"[TASK {task_id[:8]}] {result_text}")

        # Report back to Switchboard
        await report_to_switchboard(spend_token, price, result_text)

    except Exception as e:
        error = f"Booking failed: {e}"
        print(f"[TASK {task_id[:8]}] ERROR: {e}")
        tasks[task_id]["status"] = {"state": "failed"}
        tasks[task_id]["result"] = error
        await report_to_switchboard(spend_token, 0, error)


# ── Agent Card (A2A discovery) ────────────────────────────────────────────


@app.get("/.well-known/agent.json")
async def agent_card(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "schema_version": "0.0.1",
        "name": "FlightBookerAgent",
        "description": "Searches and books flights via Duffel API. Accepts natural language task descriptions.",
        "url": base,
        "provider": {"organization": "Switchboard"},
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{
            "id": "flight_booking",
            "name": "Flight Booking",
            "description": "Search and book flights given origin, destination, date and budget.",
            "examples": ["Book SFO to JFK on April 10 budget $500"],
        }],
        "switchboard:meta": {
            "capability": "flight_booking",
            "fee_type": "flat",
            "fee_value": 25,
            "fee_display": "$25 flat fee",
        }
    }
