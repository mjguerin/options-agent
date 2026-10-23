"""Event normalizer and shared feature engine (BRD 9, components 2-3; MD-01 to MD-07).

Features are computed once per eligible event and broadcast to every strategy
worker as a timestamped snapshot (FR-03). No feature may use future information.
"""
