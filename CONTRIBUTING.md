# Contributing to LeanExplore


## Project Structure

```
lean-explore/
├── src/lean_explore/
│   ├── api/          # API client for remote backend
│   ├── cli/          # Command-line interface
│   ├── local/        # Local search backend (BM25 + semantic search)
│   ├── mcp/          # MCP server implementation
│   └── shared/       # Shared data models and utilities
├── tests/            # Test suite
├── scripts/          # Development and deployment scripts
├── extractor/        # Lean declaration extraction tools
├── benchmarking/     # Performance benchmarks
└── dev_tools/        # Development utilities
```


## Make System

The project uses a Makefile for common development tasks:

```bash
make install          # Install package in editable mode with dev dependencies
make lint             # Run ruff linter
make format           # Run ruff formatter
make test             # Run tests with coverage (excludes slow, integration, external)
make test-fast        # Run only fast tests without coverage
make test-integration # Run integration tests
make test-external    # Run tests requiring external services
make test-all         # Run all tests including slow, integration, and external
make clean            # Remove cache and build artifacts
make help             # Show all available commands
```


## Coding Guidelines

**Absolute imports.** Use absolute imports throughout the codebase (e.g., `from lean_explore.api.client import Client`). Avoid relative imports. All imports must be at the top of the file.

**Naming.** Avoid abbreviations in variable, function, and class names—use full words for clarity (e.g., `configuration` not `config`, `document` not `doc`). For acronyms in class names, capitalize only the first letter (e.g., `ReplHandler`, `McpServer`, `HtmlParser`).

**Documentation.** Every module must have a module-level docstring. Every method which is not self-explanatory must have a Google-style docstring. Public-facing methods must use detailed docstrings that explain parameters, return values, and exceptions.

**Inline comments.** Add inline comments for complex code and questions that will come up during code review.

**Methods.** Keep method bodies under 40 lines (soft limit). Write self-documenting code: use descriptive variable and function names.

**Logging.** Use loggers and not print statements when needing to print to console.

**Type hints.** Use modern Python type hints (PEP 604 and PEP 585): use `|` instead of `Union`, use `| None` instead of `Optional`, and use built-in collection types like `list`, `dict`, `set`, and `tuple` instead of their `typing` module equivalents (`List`, `Dict`, `Set`, `Tuple`).


## Testing

**Coverage.** We aim for nearly 100% test coverage for all code in `src/`. 

**Style.** Do not overtly mock tests; instead, focus on testing core components with real implementations.
 
**Markers.** Use `@pytest.mark.slow` for tests that take >1 second, `@pytest.mark.integration` for tests that require multiple components, and `@pytest.mark.external` for tests that require external services/APIs.


## Maintainer

For questions or discussion about the project, contact Justin Asher at **justinchadwickasher@gmail.com**.


## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.

## Testing on the development website

Use the `development` branch for changes being tested on dev.leanexplore.com.
Package pushes run CI but do not publish a PyPI release or change production.
To deploy a package change, push it first, then update
`deploy/lean-explore-revision` in the website repository
(`lean-explore/lean-explore-app`, branch `development`) to the full package
commit SHA. Push that website commit to test and deploy the exact pair.
The development website requires the shared development login.
