"""Map Agent Commerce Gateway resources to Microsoft Agent Framework tools"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from agent_commerce_client import AgentCommerceClient, AgentCommerceError, ResourceTool
from agent_framework import FunctionTool


async def agent_commerce_tools(**kwargs: Any) -> list[FunctionTool]:
    """Discover ACG resources and return them as `FunctionTool`s.

    Accepts the keyword arguments of `agent_commerce_client.AgentCommerceClient`,
    which handles payments, limits and filtering. This function maps its resource
    descriptions to tools and sets `approval_mode="always_require"` for those
    that need approval.
    """
    client = AgentCommerceClient(**kwargs)
    return [_tool(client, tool) for tool in await client.alist_tools()]


def _tool(client: AgentCommerceClient, tool: ResourceTool) -> FunctionTool:
    async def run(**arguments: Any) -> Any:
        try:
            return asdict(await client.ainvoke(tool.name, arguments))
        except AgentCommerceError as exc:
            # Return the error as tool output because Agent Framework hides exception text by default
            return f"Error: {exc}"

    return FunctionTool(
        name=tool.name,
        description=tool.description,
        func=run,
        input_model=tool.input_schema,
        approval_mode="always_require" if tool.needs_approval else "never_require",
    )
