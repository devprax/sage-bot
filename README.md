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
