"""Map Agent Commerce Gateway resources to LangChain tools"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from agent_commerce_client import AgentCommerceClient, AgentCommerceError, ResourceTool
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.tools import BaseTool, BaseToolkit, StructuredTool, ToolException
from pydantic import ConfigDict, PrivateAttr


class AgentCommerceToolkit(BaseToolkit):
    """LangChain tools for ACG resources, paid with x402 within spending limits.

    Accepts the keyword arguments of `agent_commerce_client.AgentCommerceClient`,
    which handles payments, limits and filtering. This toolkit maps its resource
    descriptions to `StructuredTool`s and returns client errors as tool output text.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: AgentCommerceClient
    _gated: dict[str, str] | None = PrivateAttr(default=None)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(client=AgentCommerceClient(**kwargs))

    def get_tools(self) -> list[BaseTool]:
        tools = self.client.list_tools()
        self._gated = {tool.name: tool.description for tool in tools if tool.needs_approval}
        return [self._tool(tool) for tool in tools]

    def approval_middleware(self) -> HumanInTheLoopMiddleware:
        """Build approval middleware for `create_agent`.

        Uses the most recent `get_tools()` result, or discovers tools if needed.
        The agent needs a checkpointer to pause and resume calls.
        """
        if self._gated is None:
            self.get_tools()
        # Include the tool description so the reviewer can see its price information
        return HumanInTheLoopMiddleware(
            interrupt_on={
                name: {
                    "allowed_decisions": ["approve", "edit", "reject"],
                    "description": f"Tool call needs approval: {description}",
                }
                for name, description in (self._gated or {}).items()
            }
        )

    def _tool(self, tool: ResourceTool) -> StructuredTool:
        client = self.client

        def run(**arguments: Any) -> dict[str, Any]:
            try:
                return asdict(client.invoke(tool.name, arguments))
            except AgentCommerceError as exc:
                raise ToolException(str(exc)) from exc

        async def arun(**arguments: Any) -> dict[str, Any]:
            try:
                return asdict(await client.ainvoke(tool.name, arguments))
            except AgentCommerceError as exc:
                raise ToolException(str(exc)) from exc

        return StructuredTool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.input_schema,
            func=run,
            coroutine=arun,
            handle_tool_error=True,
            metadata={"needs_approval": tool.needs_approval},
        )
