"""Telemetry subsystem for codexforge.

All telemetry is optional. If the OpenTelemetry SDK is not installed
or not configured, every hook becomes a no-op and the runtime keeps
working without instrumentation.
"""
