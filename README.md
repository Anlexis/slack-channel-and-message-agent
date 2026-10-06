# Slack Channel Messaging Agent

AI agent for managing Slack channels and messages, built with Agentic Star.

> **Category**: Cat 2 (a fixed multi-step pipeline for one job-to-be-done)
> **Industry**: Common
> **Template ID**: CMN-C2-233

## Overview

Turns a plain-language instruction into a single, confirmed Slack workspace action.

Give it a sentence like *"post 'deploy finished' to #release-notes"* and it works out which
operation you meant, extracts the target channel, message text or member handles from the
wording, calls the matching Slack Web API method, and hands back a confirmation carrying the
channel ID and a permalink to the result.

Three operations are supported: posting a message (`chat.postMessage`), creating a channel
(`conversations.create`), and inviting members to a channel (`conversations.invite`). Every
one of them writes to the workspace, so the agent refuses any request it cannot classify with
confidence rather than guessing at a write, and it never invents a target channel — the
channel must be stated explicitly in the text or supplied by the caller.

Intent classification and field extraction are deterministic (keyword and pattern based), so
the pipeline runs and is testable without a language model. Out of the box it ships with a
network-free stub transport that returns the documented Slack response shapes, which makes
the template runnable end-to-end before you connect a real workspace; injecting a live HTTP
transport and providing a bot token is all that is needed to go live.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. The agent imports its base classes from the framework package at start-up, so without that
package installed and configured, import and graph compile fail outright rather than leaving the
agent running in a partially working state. This is intentional — a half-running agent is worse
than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design document and the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Point the Slack settings in `config/config.yaml` at your own workspace endpoint.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

