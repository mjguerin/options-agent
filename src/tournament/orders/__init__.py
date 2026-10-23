"""Order manager, position/exit manager and reconciliation (BRD 9, components 6-7; OM, EX).

All broker writes pass through here (OM-01). Intents and client order IDs are
persisted before submission (OM-02).
"""
