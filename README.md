# FlightBookerAgent

A2A-compatible flight booking agent for Switchboard.

Searches and books flights via Duffel API. Receives delegations from Switchboard orchestrators, finds the cheapest flight within budget, books it (sandbox), and reports actual_spend back.

## Setup

1. Get a Duffel API key at https://duffel.com → Dashboard → API tokens
2. Register this agent on Switchboard at /register to get an AGENT_API_KEY
3. Copy `.env.example` to `.env` and fill in both keys

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## Deploy to Railway

Push this directory as a separate Railway service. Set env vars:
- `DUFFEL_API_KEY`
- `AGENT_API_KEY`
- `SWITCHBOARD_URL`

## Endpoints

- `GET /health` — status
- `POST /a2a` — JSON-RPC 2.0 A2A task endpoint
- `POST /task` — simple task dispatch
- `GET /task/{id}` — task status
- `GET /.well-known/agent.json` — A2A agent card
