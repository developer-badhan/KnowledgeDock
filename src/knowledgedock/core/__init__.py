"""Typed application configuration loaded from environment variables.

Every tunable value in `.env.sample` has a field here. Nothing in the codebase
reads `os.environ` directly, so the full configuration surface is discoverable
in one place and validated once at startup.
"""
