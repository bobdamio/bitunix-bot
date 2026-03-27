"""RPyC bridge server for MT5 on Linux via Wine."""
import sys
import os

# Redirect stdout/stderr before rpyc import
log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "rpyc_server.log")
os.makedirs(os.path.dirname(log_path), exist_ok=True)
log_file = open(log_path, "w")
sys.stdout = log_file
sys.stderr = log_file

import rpyc
from rpyc.utils.server import ThreadedServer
from rpyc.core import SlaveService

port = int(sys.argv[1]) if len(sys.argv) > 1 else 18812
host = sys.argv[2] if len(sys.argv) > 2 else "0.0.0.0"

print(f"Starting RPyC server on {host}:{port}", flush=True)
server = ThreadedServer(SlaveService, hostname=host, port=port, reuse_addr=True)
server.start()
