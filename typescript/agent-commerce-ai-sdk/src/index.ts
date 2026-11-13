import { createAgentCommerceClient } from '@devlab.group/agent-commerce-client';
import type { AgentCommerceOptions } from '@devlab.group/agent-commerce-client';
import { jsonSchema, tool } from 'ai';
import type { JSONSchema7, ToolSet } from 'ai';

export { AgentCommerceError } from '@devlab.group/agent-commerce-client';
export type { AgentCommerceOptions, SpendStore } from '@devlab.group/agent-commerce-client';

/**
 * Build one AI SDK tool per eligible gateway resource.
 * The core client handles payments, limits and filtering. Client failures
 * throw `AgentCommerceError`, which the AI SDK returns to the model as a tool error.
 */
export async function agentCommerceTools(options: AgentCommerceOptions): Promise<ToolSet> {
  const client = createAgentCommerceClient(options);
  const tools = await client.listTools();
  return Object.fromEntries(
    tools.map((resource) => [
      resource.name,
      tool({
        description: resource.description,
        inputSchema: jsonSchema<Record<string, unknown>>(resource.inputSchema as JSONSchema7),
        needsApproval: resource.needsApproval,
        execute: (input) => client.invoke(resource.name, input),
      }),
    ]),
  );
}

/**
 * Build `toolApproval` for `generateText`, `streamText` or `ToolLoopAgent`.
 * Tools with `needsApproval: true` require human approval. Entries override the
 * tool-level flag; spread the result first to override an entry:
 * `{ ...agentCommerceToolApproval(tools), report: 'approved' }`.
 */
export function agentCommerceToolApproval(tools: ToolSet): Record<string, 'user-approval'> {
  return Object.fromEntries(
    Object.entries(tools)
      .filter(([, resource]) => resource.needsApproval === true)
      .map(([name]) => [name, 'user-approval' as const]),
  );
}
