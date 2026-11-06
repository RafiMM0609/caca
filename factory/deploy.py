import json

from .process import run
from .workspace import digest_file


class Deployer:
    def status(self, target):
        argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
                "-o", "UserKnownHostsFile=" + target.known_hosts,
                "-i", target.identity_file, "-p", str(target.port), target.host, target.helper + " status"]
        return json.loads(run(argv, timeout=90))

    def apply(self, target, artifact, digest, cancelled=lambda: False):
        if digest_file(artifact) != digest:
            raise ValueError("Artifact checksum mismatch")
        argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                "-o", "UserKnownHostsFile=" + target.known_hosts,
                "-i", target.identity_file, "-p", str(target.port), target.host,
                target.helper + " apply " + digest]
        response = json.loads(run(argv, input_data=artifact.read_bytes(), timeout=180, cancelled=cancelled))
        if response.get("digest") != digest or response.get("status") != "healthy":
            raise ValueError("Remote helper did not confirm the expected healthy release")
        return response

