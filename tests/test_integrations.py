from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

from satubrain.integrations import client_config, hook_config, lifecycle
from satubrain.scope import Workspace
from satubrain.store import Store

ROOT = Path(__file__).resolve().parents[1]


class IntegrationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "memory.sqlite3"

    def test_configurations_point_to_the_same_project_task_and_database(self):
        space = Workspace("example.org/team/repo", "main")
        for client in ("claude", "codex", "copilot", "cursor", "generic"):
            with self.subTest(client=client):
                config = client_config(client, space, self.root, "DEMO-17", None, self.path)
                if client == "codex":
                    config = tomllib.loads(config)["mcp_servers"]
                else:
                    config = config["servers" if client == "copilot" else "mcpServers"]
                server = config["satubrain"]
                self.assertEqual(server["command"], "satubrain")
                self.assertIn(str(self.path.resolve()), server["args"])
                self.assertIn("DEMO-17", server["args"])
                self.assertIn(space.project, server["args"])

    def test_claude_and_codex_hooks_checkpoint_and_resume(self):
        for source, target in (("claude", "codex"), ("codex", "claude")):
            task = "release-" + source
            for agent, event in ((source, "Stop"), (target, "SessionStart")):
                command = hook_config(agent, task, "portable", "main")["hooks"][event][0]["hooks"][0]["command"]
                token = command.split()[-1]
                payload = {
                    "hook_event_name": event, "cwd": str(self.root), "session_id": "session-1",
                    "last_assistant_message": "\n".join((
                        "Unmarked reply text",
                        "```satubrain-handoff",
                        "SQLite migration passed. Review the PR next.",
                        "```",
                        "Unmarked trailing text",
                    )),
                }
                process = subprocess.run(
                    [sys.executable, "-m", "satubrain", "--db", str(self.path), "hook", token],
                    input=json.dumps(payload), text=True, capture_output=True, cwd=ROOT, check=True,
                )
                self.assertEqual(process.stderr, "")
                output = json.loads(process.stdout)
                if event == "Stop":
                    self.assertEqual(output, {})
                else:
                    context = output["hookSpecificOutput"]["additionalContext"]
                    self.assertIn("Review the PR next.", context)
                    self.assertIn("untrusted reference data", context)
                    self.assertNotIn("Unmarked", context)

    def test_automatic_capture_skips_unmarked_and_ambiguous_replies(self):
        store = Store(self.path)
        payload = {"hook_event_name": "Stop", "cwd": str(self.root), "session_id": "session"}
        fence = "```satubrain-handoff\n"
        for reply in (
            "A conversation with no checkpoint.",
            fence + "An incomplete block.",
            fence + "First block.\n```\n" + fence + "Second block.\n```",
        ):
            output = lifecycle("claude", "Stop", "DEMO-17", "portable", "main",
                               payload | {"last_assistant_message": reply}, store)
            self.assertEqual(output, {})
        self.assertEqual(store.search("portable", "DEMO-17", "main"), [])

    def test_marked_handoff_rejects_credentials(self):
        store = Store(self.path)
        payload = {
            "hook_event_name": "Stop", "cwd": str(self.root), "session_id": "session",
            "last_assistant_message": "```satubrain-handoff\nPassword=" + "x" * 16 + "\n```",
        }
        with self.assertRaisesRegex(ValueError, "credential"):
            lifecycle("codex", "Stop", "DEMO-17", "portable", "main", payload, store)
        self.assertEqual(store.search("portable", "DEMO-17", "main"), [])

    @unittest.skipUnless(find_spec("mcp"), "requires the optional official MCP SDK")
    def test_official_mcp_client_documented_stdio_workflow(self):
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run(agent, bound=True):
            command = ["-m", "satubrain", "--db", str(self.path), "mcp", "--agent", agent,
                       "--project", "portable", "--branch", "main"]
            if bound:
                command += ["--task", "DEMO-17"]
            config = StdioServerParameters(command=sys.executable, args=command, cwd=str(ROOT),
                                           env=dict(os.environ))
            async with stdio_client(config) as (read, write):
                async with ClientSession(read, write) as client:
                    initialized = await client.initialize()
                    self.assertEqual(initialized.serverInfo.name, "satubrain")
                    listed = await client.list_tools()
                    self.assertEqual(len(listed.tools), 5)
                    if agent == "claude":
                        result = await client.call_tool("handoff", {"text": "Linux tests passed."})
                        self.assertFalse(result.isError)
                        self.assertEqual(json.loads(result.content[0].text)["status"], "saved")
                    elif bound:
                        result = await client.call_tool("resume", {"budget": 128})
                        self.assertIn("Linux tests passed", result.content[0].text)
                        result = await client.call_tool("resume", {"task": "other"})
                        self.assertTrue(result.isError)
                    else:
                        result = await client.call_tool("resume", {"task": "other"})
                        self.assertEqual(json.loads(result.content[0].text)["items"], [])

        async def scenario():
            await run("claude")
            await run("codex")
            await run("generic", bound=False)

        anyio.run(scenario)
        self.assertEqual(Store(self.path).search("portable", "DEMO-17", "main")[0]["agent"], "claude")
