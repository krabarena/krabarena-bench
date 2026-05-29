"""Launch Camoufox as a Playwright WS server on a known port + path.

The CLI `python -m camoufox server` in camoufox 0.4.x takes no
options; configurable launch is only via the Python API. This script
is the canonical entrypoint for the bench image — bind to a stable
port and root-path WS so the runner connects to a predictable URL.
"""

from camoufox.server import launch_server

if __name__ == "__main__":
    launch_server(
        headless=True,
        port=9222,
        ws_path="/",
    )
