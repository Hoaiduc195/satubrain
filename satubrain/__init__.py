"""SatuBrain local-first agent memory."""

from importlib.metadata import PackageNotFoundError, version

try:
    VERSION = version("satubrain")
except PackageNotFoundError:
    VERSION = "1.0.0rc1"
