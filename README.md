# Sage Bot

An open-source AI assistant for Slack, built by [DevPrax](https://github.com/devprax).

Sage Bot is being developed to help teams find answers and take action through configurable language-model providers and compatible [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers.

## Project status

This repository is at an early development stage. It currently provides an HTTP server with a liveness endpoint only; there is no supported release or installation procedure yet.

Implementation will arrive through small, focused pull requests. Planned capabilities described here are goals, not available features.

## Run the HTTP server

From a development environment set up with `make setup`:

```sh
uv run sage-bot-migrate                # upgrades the schema (uses MIGRATION_DATABASE_URL)
uv run sage-bot-server                 # binds 127.0.0.1:8000
uv run sage-bot-server --host 0.0.0.0 --port 9000
curl http://127.0.0.1:8000/health/live  # {"status":"ok"}
curl http://127.0.0.1:8000/health/ready # {"status":"ok"} or 503 {"status":"not_ready"}
```

`/health/live` answers whenever the process is running and needs no configuration. `/health/ready` returns `200` only when the settings (`DATABASE_URL`, `SAGE_ENCRYPTION_KEY`) are valid and, within `READINESS_TIMEOUT_SECONDS` (default 2), PostgreSQL reports the database schema revision this release expects and the `DATABASE_URL` role passes the runtime role check (see [Database credentials](#database-credentials)). Otherwise it returns `503` without saying why. The reason is logged by error type or fixed problem code only, never with values. Settings are read when the server starts, so changing them requires a restart.

Schema migrations are an explicit step: run `sage-bot-migrate` (or `python -m sage_bot.db.migrate`) before starting a release. It is idempotent, needs only `MIGRATION_DATABASE_URL` (it never falls back to `DATABASE_URL`), and reports failures by error type only. Concurrent runs are serialized by a database lock. A run that waits more than 60 seconds for another runner fails instead of hanging. The server never migrates on startup; readiness stays `503` until the schema is at the expected revision and the runtime role has been granted access to it.

The server binds to the local interface unless `--host` is given. Port `0` picks a free port. The server does not send a `Server` header, does not trust forwarded headers, and does not write an access log, since request lines can carry credentials. Once the server has started, SIGTERM and Ctrl+C both shut it down gracefully: the process then exits with status 0 after SIGTERM and 130 after Ctrl+C. A signal that arrives while the process is still starting stops it with the default signal status. `python -m sage_bot.api.server` is equivalent.

### Database credentials

The server and the migration command use different PostgreSQL roles, and neither receives the other's credentials:

- `MIGRATION_DATABASE_URL`, read only by `sage-bot-migrate`, connects as the role that owns the database and its schema. The server never reads it.
- `DATABASE_URL`, read only by the server, connects as a restricted runtime role. The server does not need, and must not be given, the migration credentials.

Roles, passwords and grants belong to the deployment; Sage Bot never creates roles or grants privileges. For example, as a PostgreSQL administrator, once:

```sql
CREATE ROLE sage_owner LOGIN PASSWORD '…';
CREATE ROLE sage_runtime LOGIN PASSWORD '…'
  NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB;
CREATE DATABASE sage OWNER sage_owner;
```

Then, as `sage_owner`, after each `sage-bot-migrate` (the statement is idempotent):

```sql
GRANT SELECT ON alembic_version, slack_workspaces TO sage_runtime;
GRANT EXECUTE ON FUNCTION resolve_slack_workspace(text) TO sage_runtime;
```

Readiness verifies the runtime role on every probe, from its effective privileges rather than its name. The privileges of every role it belongs to count too, whether that membership confers them by inheritance, by `SET ROLE` or by `ADMIN`. Readiness reports `503` if the role:

- runs as a role other than its login role, for example through a default `SET ROLE`;
- has, directly or through a role it belongs to, superuser, `BYPASSRLS`, `REPLICATION`, `CREATEROLE` or `CREATEDB`;
- belongs to any predefined `pg_` role, such as `pg_monitor`, `pg_read_all_data` or `pg_execute_server_program`, whose powers are not recorded as grants;
- owns the application schema, a schema on its search path, or any table, function or type in them. This includes the database owner, which owns `public`;
- holds any grant beyond an allow-list. The allow-list is `SELECT` on `alembic_version` and `slack_workspaces`, `EXECUTE` on `resolve_slack_workspace(text)` in the application schema (identified by schema, name and argument types; no other function, `SECURITY DEFINER` or not), `USAGE` on the application schema, and `CONNECT` or `TEMPORARY` on databases. Grants checked include those on tables, columns, functions, schemas, databases, types, languages, foreign servers, large objects and tablespaces, plus default privileges for future objects and user mappings. On PostgreSQL 17, `MAINTAIN` counts too;
- can create objects in those schemas or in the database;
- benefits from anything the deployment granted to `PUBLIC` beyond what each object starts with. That baseline is the initial privileges PostgreSQL or an extension recorded, or else PostgreSQL's default for that kind of object. Examples include `EXECUTE` on `pg_read_binary_file` or `dblink_connect_u`, `SELECT` on `pg_statistic`, `USAGE` on a foreign server, a user mapping for `PUBLIC`, or (PostgreSQL 15 and later) a grant on a server parameter. Grants to other roles that merely spell out `PUBLIC`'s defaults are not flagged. `USAGE` on a schema other than the application schema, for example an `extensions` schema, also counts, when granted to the role or, beyond the schema's initial privileges, to `PUBLIC`;
- cannot read `slack_workspaces`, or that table has row-level security disabled;
- cannot execute `resolve_slack_workspace(text)`, or that function is missing or is not a `SECURITY DEFINER` function owned by the table's owner;
- shares `EXECUTE` on `resolve_slack_workspace(text)` with `PUBLIC`, whether granted explicitly or through PostgreSQL's default for functions;
- connects with `row_security` off, whether set in the options of `DATABASE_URL` or as a role or database default. This does not bypass the policy for a restricted role; it makes every tenant query on the table fail. Readiness checks the setting as the session has it and does not override it.

Before PostgreSQL 15, `PUBLIC` can create objects in `public` by default, so readiness reports `503` until the deployment runs `REVOKE CREATE ON SCHEMA public FROM PUBLIC`.

The check covers the runtime role's own privileges. It does not audit what the trusted owner grants or defines, such as additional row-level security policies, or views and `SECURITY DEFINER` functions that expose rows. Functions the owner defines must revoke `EXECUTE` from `PUBLIC` unless every role may run them. PostgreSQL records an extension's current privileges as its initial privileges when the extension is updated, so a grant to `PUBLIC` made before an `ALTER EXTENSION … UPDATE` is no longer reported.

The problem is logged as a fixed code, such as `owns_relation`, without role names or connection values. Liveness is unaffected.

### Workspaces and tenant isolation

Each Slack workspace has one record in `slack_workspaces`: an internal UUID, its unique Slack team ID, a creation time and a status, which is `disabled` until explicitly enabled. Nothing in this release enables a workspace, registers one, or serves workspace data over HTTP.

Access is split by trust:

- **Provisioning** (`sage_bot.db.workspaces.PostgresWorkspaceProvisioning`) is trusted. It registers a team and finds any team without a tenant context, so it connects as the role that owns the schema, the same role that runs `sage-bot-migrate`. Registration is idempotent: registering a team again, including concurrently, returns the same workspace (`created` is true exactly once) and never changes its status.
- **Resolution** (`PostgresWorkspaceResolver`) maps a Slack team ID to its workspace UUID, only when the workspace is enabled; an unknown or disabled team gives no match, and it never registers or enables a workspace. It connects with `DATABASE_URL` as the runtime role, needs no tenant binding, and calls `resolve_slack_workspace(text)` (described below). A malformed team ID is rejected before connecting. A database failure or timeout raises `WorkspaceLookupUnavailable`, never a no-match, so ingress can treat it as temporary. One deadline (2 seconds by default) bounds connecting and the query: past it the connection is closed, and the session's `statement_timeout` makes the server abandon the query too. **It is not authentication:** ingress must verify the Slack request's signature before trusting the team ID it carries.
- **Tenant-scoped access** (`PostgresTenantWorkspaces`, `tenant_transaction`) binds each transaction to one workspace through the `sage.workspace_id` setting. Row-level security then exposes only that workspace's row; without the setting, no rows. It connects with `DATABASE_URL` as the restricted runtime role, which cannot disable or change the policy. Readiness rejects write privileges on the table: `TRUNCATE` ignores row-level security, and writes would reveal whether another workspace's team ID exists, through the unique constraint.

The policy limits a transaction to the workspace it is bound to; it does not decide which workspace that is. Any session of the runtime role can bind itself to a workspace whose UUID it knows. It protects against unscoped or mis-scoped queries, not against stolen runtime credentials. Code must bind only to a workspace resolved on the server from an authenticated source, never from caller input.

Row-level security is enabled but not forced, because the owning role is the trusted provisioning role. A SECURITY DEFINER function owned by that role bypasses the policy only when it is not forced. `resolve_slack_workspace(text)` (migration `0003`) is such a function: owned by the migration owner, it returns only an enabled workspace's UUID, and nothing else about any workspace. Its body is SQL-standard (`BEGIN ATOMIC`), so PostgreSQL binds the objects it names when the function is created, and a caller's search path or temporary tables cannot substitute them; its own search path is fixed to `pg_catalog, pg_temp`. The migration revokes `EXECUTE` from `PUBLIC` in the transaction that creates it, so only roles the deployment grants it to can call it. Provisioning needs the owner's credentials, which the server never receives; readiness confirms that the server's own role cannot bypass the policy. Schema privileges are read from the catalog rather than assumed from the PostgreSQL version; CI uses PostgreSQL 17.

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
