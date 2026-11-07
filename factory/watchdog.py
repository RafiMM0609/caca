"""Keep a local app in its launcher's process group; reap it if the API/worker dies."""
import os
import signal
import subprocess
import sys
import time


if __name__ == "__main__":
    parent = int(sys.argv[1])
    child = subprocess.Popen(sys.argv[2:])
    while child.poll() is None:
        if os.getppid() != parent:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        time.sleep(0.2)
    raise SystemExit(child.returncode)
