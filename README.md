# Sage Bot

An open-source AI assistant for Slack, built by [DevPrax](https://github.com/devprax).

Sage Bot is being developed to help teams find answers and take action through configurable language-model providers and compatible [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers.

## Project status

This repository is at an early development stage. It currently provides an HTTP server with a liveness endpoint only; there is no supported release or installation procedure yet.

Implementation will arrive through small, focused pull requests. Planned capabilities described here are goals, not available features.

## Run the HTTP server

From a development environment set up with `make setup`:

```sh
uv run sage-bot-migrate                # upgrades the database schema (uses DATABASE_URL)
uv run sage-bot-server                 # binds 127.0.0.1:8000
uv run sage-bot-server --host 0.0.0.0 --port 9000
curl http://127.0.0.1:8000/health/live  # {"status":"ok"}
curl http://127.0.0.1:8000/health/ready # {"status":"ok"} or 503 {"status":"not_ready"}
```

`/health/live` answers whenever the process is running and needs no configuration. `/health/ready` returns `200` only when the settings (`DATABASE_URL`, `SAGE_ENCRYPTION_KEY`) are valid and, within `READINESS_TIMEOUT_SECONDS` (default 2), PostgreSQL reports the database schema revision this release expects. Otherwise it returns `503` without saying why. The reason is logged by error type only, never with values. Settings are read when the server starts, so changing them requires a restart.

Schema migrations are an explicit step: run `sage-bot-migrate` (or `python -m sage_bot.db.migrate`) before starting a release. It is idempotent, needs the same settings as the server, and reports failures by error type only. Concurrent runs are serialized by a database lock. A run that waits more than 60 seconds for another runner fails instead of hanging. The server never migrates on startup; readiness stays `503` until the schema is at the expected revision.

The server binds to the local interface unless `--host` is given. Port `0` picks a free port. The server does not send a `Server` header, does not trust forwarded headers, and does not write an access log, since request lines can carry credentials. Once the server has started, SIGTERM and Ctrl+C both shut it down gracefully: the process then exits with status 0 after SIGTERM and 130 after Ctrl+C. A signal that arrives while the process is still starting stops it with the default signal status. `python -m sage_bot.api.server` is equivalent.

### Workspaces and tenant isolation

Each Slack workspace has one record in `slack_workspaces`: an internal UUID, its unique Slack team ID, a creation time and a status, which is `disabled` until explicitly enabled. Nothing in this release enables a workspace, registers one, or serves workspace data over HTTP.

Access is split by trust:

- **Provisioning** (`sage_bot.db.workspaces.PostgresWorkspaceProvisioning`) is trusted. It registers a team and finds any team without a tenant context, so it connects as the role that owns the schema, the same role that runs `sage-bot-migrate`. Registration is idempotent: registering a team again, including concurrently, returns the same workspace (`created` is true exactly once) and never changes its status.
- **Tenant-scoped access** (`PostgresTenantWorkspaces`, `tenant_transaction`) binds each transaction to one workspace through the `sage.workspace_id` setting. Row-level security then exposes only that workspace's row; without the setting, no rows. It must connect as a separate role that does not own the tables and has neither superuser nor `BYPASSRLS`. Such a role cannot disable or change the policy. Grant it only `SELECT ON slack_workspaces`, as the owner. Never grant it `TRUNCATE`, which ignores row-level security. Write privileges would also let it learn whether another workspace's team ID exists, through the unique constraint.

The policy limits a transaction to the workspace it is bound to; it does not decide which workspace that is. Any session of the runtime role can bind itself to a workspace whose UUID it knows. It protects against unscoped or mis-scoped queries, not against stolen runtime credentials. Code must bind only to a workspace resolved on the server from an authenticated source, never from caller input.

Row-level security is enabled but not forced, because the owning role is the trusted provisioning role. A SECURITY DEFINER function owned by that role bypasses the policy only when it is not forced. Resolving a Slack team to its workspace at ingress is planned as such a narrow function, executable by the runtime role; until then, only provisioning can resolve a team. Today the server and `sage-bot-migrate` share one `DATABASE_URL`, so separate migration and runtime credentials must be introduced before any code serves tenant data. These guarantees assume PostgreSQL 15 or later, where ordinary roles cannot create objects in the `public` schema; CI uses 17.

### PostgreSQL integration tests

These tests run when `SAGE_TEST_DATABASE_URL` points to a disposable database whose user can create databases and roles; each test creates its own and drops them afterwards. Otherwise they are skipped locally, and they fail in CI, which provides a PostgreSQL service. For example, with Podman (or Docker):

```sh
podman run -d --rm --name sage-test-db -p 127.0.0.1:55432:5432 \
  -e POSTGRES_USER=sage_test -e POSTGRES_PASSWORD=sage_test_password -e POSTGRES_DB=sage_test \
  postgres:17.10
SAGE_TEST_DATABASE_URL=postgresql://sage_test:sage_test_password@127.0.0.1:55432/sage_test make check
```

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
