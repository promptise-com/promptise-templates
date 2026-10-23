"""Offline stand-ins for the systems a finance team runs on.

``store`` holds the data (one SQLite file), ``reconcile`` holds the matching
logic, and the two ``*_server`` modules expose them as MCP servers.  Replace
these with your real billing provider and email service; see the README.
"""
