"""
Tool Gateway: every tool call the agent makes passes through here.
This is the central abstraction that lets the platform:
  - record every call (the trajectory)
  - execute it against the simulated environment
  - return results back to the agent

This is deliberately framework-agnostic: any agent adapter can call
gateway.call("tool_name", **kwargs) and get recorded + executed.
"""

import copy


class ToolGateway:
    """
    Takes a {tool_name: callable} mapping supplied by whichever domain is
    under test. The gateway itself knows nothing about banking, support, or
    any other world — it only records and dispatches.
    """

    def __init__(self, tools):
        self._tools = dict(tools)
        self.trajectory = []  # ordered list of every tool call made

    def call(self, tool_name, **kwargs):
        if tool_name not in self._tools:
            result = {"status": "error", "reason": f"unknown_tool:{tool_name}"}
        else:
            result = self._tools[tool_name](**kwargs)

        self.trajectory.append({
            "tool": tool_name,
            "args": dict(kwargs),
            "result": copy.deepcopy(result),
        })
        return result

    def record(self, tool_name, args, result=None):
        """
        Append a call the harness did not execute.

        Used when the agent under test runs out-of-process — it already
        executed the call against its own environment, so re-executing here
        would report results it never actually saw. Rules evaluate tool
        names and arguments, so a recorded call is indistinguishable from a
        dispatched one for evaluation purposes.
        """
        self.trajectory.append({
            "tool": tool_name,
            "args": dict(args or {}),
            "result": copy.deepcopy(result),
        })

    def get_trajectory(self):
        return list(self.trajectory)
