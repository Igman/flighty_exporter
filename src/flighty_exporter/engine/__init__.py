"""The run engine: the whole pipeline for a session, reported as events.

Sits above ``stages`` and ``output`` and below the command line. It prints
nothing and parses no arguments: callers subscribe to its events.
"""
