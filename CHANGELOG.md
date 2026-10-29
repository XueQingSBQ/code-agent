# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- SWE-bench Lite evaluation harness with Docker support
- Custom API endpoint support for Anthropic-compatible providers
- Comprehensive architecture documentation
- Contributing guidelines
- Evaluation metrics tracking (tokens, cost, time)

### Changed
- Provider layer now accepts `base_url` parameter for custom endpoints
- Updated README with evaluation instructions

### Fixed
- API authentication for custom endpoints

## [0.0.1] - 2024-09-16

### Added
- Initial release
- Five-layer context compression pyramid (L1-L4.5)
- Query loop with streaming tool execution
- Six core tools: Read, Write, Edit, Glob, Grep, Bash
- Permission engine with four modes (default, acceptEdits, plan, bypass)
- BashGuard AST-based command analysis
- Hooks system for lifecycle events
- Audit logging
- Session storage and resume capability
- CLI with Rich Live rendering
- SDK with async context manager API
- 184 tests (unit, property, integration)
- Import-linter architecture enforcement
- Strict mypy type checking

[Unreleased]: https://github.com/yourusername/carbide/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/yourusername/carbide/releases/tag/v0.0.1
