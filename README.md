# Sage Bot

An open-source AI assistant for Slack, built by [DevPrax](https://github.com/devprax).

Sage Bot is being developed to help teams find answers and take action through configurable language-model providers and compatible [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers.

## Project status

This repository is at the initialization stage. It currently contains project information and licensing only; there is no runnable application, supported release, or installation procedure yet.

Implementation will arrive through small, focused pull requests. Planned capabilities described here are goals, not available features.

## Direction

- Slack as the messaging interface.
- Configurable language-model providers.
- Protocol-driven discovery and use of compatible MCP tools.
- Server-side authorization and validation before tool execution.
- Careful handling of credentials, user data, and untrusted tool content.

## Development approach

Each contribution should solve one clear problem and be reviewable on its own. Use a feature branch and a focused pull request, with small commits that explain the intent of the change.

Introduce the project scaffold and development tooling first, then add behavior incrementally with relevant tests. Add CI and code scanning alongside the source they validate, and require checks only after they work reliably. Publish setup instructions and release guidance as those capabilities become available.

Pull requests use squash merging. Describe the outcome, validation, and compatibility impact in each PR so its final commit is meaningful.

## API contract

The HTTP contract describes only the operations Sage Bot implements. Its source is the modular OpenAPI tree in `contracts/openapi/`. `contracts/openapi.yaml` is generated from it, so do not edit that file by hand.

- Run `make contract` after editing the source.
- A pull request that adds or changes an endpoint updates its implementation, contract, and tests together, and states any compatibility impact.

The application does not publish its schema or interactive docs; the contract lives in the repository. `make check` compares the contract with the application's route table in process.

It fails when:

- The bundle is stale, is not valid OpenAPI 3.1, or has an unresolved reference.
- The served and declared operations (path and method) differ.
- An operation's effective inputs differ from the contract in parameter name, location, or required flag, or in whether a request body is read.
  - Effective inputs are what FastAPI resolves for the route: its own signature plus dependencies at any depth, whether attached to the endpoint, the route, or the application.
  - Parameter models are expanded into their fields.
  - The checks derive these inputs from a per-route operation that FastAPI generates in process; the application never publishes it.
- An operation's declared status codes differ from that operation's codes, which include FastAPI's automatic `422` whenever the route has an effective input.
- A response obtained by calling an operation without inputs has an undeclared status code, a media type other than the declared one, or a body that does not validate against the declared schema. A `HEAD` response must also have the status and headers of `GET` and no body.

It does not yet compare:

- Parameter and request-body schemas.
- The `422` body.
- Other response headers.
- Security requirements.
- Status codes raised only at runtime.
- Inputs an endpoint reads directly from the `Request` object, including credentials read by security schemes.
- Parameters hidden with `include_in_schema=False`.

Endpoints that rely on these need their own tests. Routers added with `include_router` are not yet supported: the route-table check rejects them, so the checks fail rather than silently skipping a route.

## Contributing and support

DevPrax maintains shared community guidance:

- [Contributing](https://github.com/devprax/.github/blob/main/CONTRIBUTING.md)
- [Code of Conduct](https://github.com/devprax/.github/blob/main/CODE_OF_CONDUCT.md)
- [Support](https://github.com/devprax/.github/blob/main/SUPPORT.md)

Use [issues](https://github.com/devprax/sage-bot/issues) for actionable bugs and feature proposals. Discuss substantial changes before starting implementation.

## Security

Follow the [DevPrax security policy](https://github.com/devprax/.github/blob/main/SECURITY.md). Use the repository's [private vulnerability reporting form](https://github.com/devprax/sage-bot/security/advisories/new) for suspected vulnerabilities; do not publish sensitive details in an issue.

## License

Copyright 2026 Sage Bot contributors.

Licensed under the [Apache License, Version 2.0](LICENSE).
