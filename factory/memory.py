import asyncio
import os
import subprocess

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .agents import BASE_ENV


class Memory:
    """Project-scoped Memorix MCP. The DB remains authoritative; memory failure is explicit."""
    def __init__(self, settings):
        self.settings = settings

    async def _call(self, project, tool, arguments):
        directory = self.settings.data / "memory" / project
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (directory / ".git").exists():
            subprocess.run(["git", "init", "-q", str(directory)], check=True, timeout=10)
        env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
        env.update({"XDG_DATA_HOME": str(directory / "state"), "MEMORIX_MODE": "micro"})
        params = StdioServerParameters(command=self.settings.memorix,
                                       args=["serve", "--cwd", str(directory), "--mode", "micro"],
                                       cwd=str(directory), env=env)
        # Never persist the server's stderr; it can contain user data and configuration.
        with open(os.devnull, "w") as errors:
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments)
                    if result.isError:
                        raise RuntimeError("Memorix rejected the operation")
                    return "\n".join(c.text for c in result.content if c.type == "text")[:8000]

    def call(self, project, tool, arguments):
        if not self.settings.memorix:
            return ""
        async def bounded():
            return await asyncio.wait_for(self._call(project, tool, arguments), timeout=40)
        return asyncio.run(bounded())

    def search(self, project, query):
        return self.call(project, "memorix_search", {"query": query[:1000], "limit": 5, "maxTokens": 1500,
                         "scope": "project", "quality": "fast", "purpose": "Retrieve prior verified factory decisions"})

    def save(self, project, title, narrative, reference):
        return self.call(project, "memorix_store", {"entityName": "software-factory", "type": "what-changed",
                         "title": title[:200], "narrative": narrative[:4000], "facts": [reference],
                         "topicKey": reference, "visibility": "project"})

