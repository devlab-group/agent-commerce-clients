# Agent Commerce Gateway clients

Clients and framework tools for discovering and paying for API endpoints behind a self-hosted Agent Commerce Gateway,
using x402 within spending limits you set.

[Agent Commerce Gateway](https://github.com/devlab-group/agent-commerce) (ACG) puts x402 payments in front of an
existing HTTP API. The clients in this repository let agents discover those endpoints and pay for them. Both clients
enforce spending limits before signing a payment.

| Directory                               | Package                                   | Purpose                                          |
| --------------------------------------- | ----------------------------------------- | ------------------------------------------------ |
| `python/agent-commerce-client`          | PyPI `agent-commerce-client`              | Python buyer core                                |
| `python/langchain-agent-commerce`       | PyPI `langchain-agent-commerce`           | LangChain toolkit                                |
| `python/agent-framework-agent-commerce` | PyPI `agent-framework-agent-commerce`     | Microsoft Agent Framework tools                  |
| `typescript/agent-commerce-client`      | npm `@devlab.group/agent-commerce-client` | TypeScript buyer core                            |
| `typescript/agent-commerce-ai-sdk`      | npm `@devlab.group/agent-commerce-ai-sdk` | Vercel AI SDK tools                              |
| `conformance`                           | -                                         | Scenarios both cores must pass                   |
| `docs`                                  | -                                         | Source of the acg.devlab.group integration pages |

The Python and TypeScript cores handle payments. The framework packages turn gateway resources into tools and connect
them to each framework's approval mechanism.

Start with [docs/buyer-core.md](docs/buyer-core.md) for configuration, spending limits and errors, then read the guide
for your framework: [LangChain](docs/langchain.md), [Microsoft Agent Framework](docs/microsoft-agent-framework.md),
[Vercel AI SDK](docs/ai-sdk.md).

The gateway image, demo merchant and deployment templates are in the gateway repository under
[`deploy/`](https://github.com/devlab-group/agent-commerce/tree/master/deploy).

## License

Apache-2.0
