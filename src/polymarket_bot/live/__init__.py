"""Live (real-money) trading on Polymarket US.

This is the ONLY subpackage under polymarket_bot allowed to reference an API
secret key, sign requests, or place/cancel real orders. Every other module
in polymarket_bot must remain read-only/simulated -- see
tests/test_no_live_orders.py, which enforces this boundary.

Polymarket US (api.polymarket.us / gateway.polymarket.us) is a separate,
CFTC-regulated, USD-settled exchange operated by QCX LLC -- a completely
different platform from the international polymarket.com. Authentication is
an Ed25519 API keypair (Key ID + Secret Key issued via Polymarket US's own
developer portal), not a blockchain wallet.

Nothing in this package runs on import. Placing a real order requires:
  1. LIVE_TRADING_ENABLED=true in your local .env
  2. A typed confirmation phrase entered interactively (see confirmation.py)
  3. Credentials present in your local, gitignored .env (never in
     .env.example or .env.live.example, which are templates only)
"""
