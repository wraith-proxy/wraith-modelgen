from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("wraith-modelgen")
except PackageNotFoundError:
    # Used when not installed (running from source): reports 0.0.0.
    __version__ = "0.0.0"
