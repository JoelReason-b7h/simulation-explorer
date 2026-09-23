"""Serves the live view on every interface so another machine on the same network can open it.

`python3 -m http.server` binds 127.0.0.1 only, so the page is reachable from this laptop and from
nothing else. This binds 0.0.0.0 and prints the address to hand to the other machine.

    python3 serve.py [port]
"""

import os
import re
import socket
import subprocess
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # run.json is rewritten on every publish, and a cached copy makes the page look frozen.
        self.send_header("Cache-Control", "no-store")
        SimpleHTTPRequestHandler.end_headers(self)

    def log_message(self, fmt, *args):
        pass


def addresses():
    """Every IPv4 address this machine holds, with the interface each one sits on.

    The interface name is the whole point. A route probe picks one address, and on this laptop the
    AWS VPN wins it and answers 10.11.1.189, which only reaches machines through the tunnel. A
    phone on the same Wi-Fi needs the address on en0 or en5 and gets nothing from the tunnel one,
    so an unlabelled list of three addresses sends the reader to the one that cannot work.
    """
    try:
        out = subprocess.run(["ifconfig", "-a"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    found = []
    interface = ""
    for line in out.splitlines():
        if line and not line[0].isspace():
            interface = line.split(":")[0]
            continue
        match = re.match(r"\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if match and not match.group(1).startswith("127."):
            found.append((interface, match.group(1)))
    if found:
        return found
    try:
        return [("", socket.gethostbyname(socket.gethostname()))]
    except OSError:
        return []


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8420
    server = ThreadingHTTPServer(("0.0.0.0", port), partial(Handler, directory=HERE))
    print("this machine       http://localhost:{}/live.html".format(port))
    for interface, host in addresses():
        # A tunnel interface carries the VPN, which no device on the Wi-Fi can route to.
        kind = "through the VPN only" if interface.startswith("utun") else "on the local network"
        print("{:18} http://{}:{}/live.html   ({}, {})".format(
            interface, host, port, interface, kind))
    print("serving {}".format(HERE))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
