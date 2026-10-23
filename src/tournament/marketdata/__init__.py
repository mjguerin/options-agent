"""The one shared market-data component (FR-03, MD-01 to MD-11).

Every strategy reads market data from here; none fetches its own. Historical
downloads (Step 2) run as a batch job outside the trading session (NFR-05).
The live feed handler (Step 3) runs inside the engine process and fans one
SIP + OPRA stream out to all five workers.
"""
