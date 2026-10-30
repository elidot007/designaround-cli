# DesignAround

DesignAround is a command-line tool for people who sell or design physical products. It checks a product's appearance against published design patents in the US, China and Europe, helps change the design until it is visibly different, and prepares patent drawings and a filing package for your own application.

It runs on your computer: a CLI, a chat workbench, an agent Skill and an MCP server. The CAD engine, your models and your cases stay in `~/.designaround`. The server at [designaround.elidot.com](https://designaround.elidot.com) handles your account, registry searches and hosted AI models.

DesignAround is software, not a law firm, and does not give legal advice or freedom-to-operate opinions. A screen is a starting point. A registered patent attorney or agent you choose reviews and files an application.

## Install

Python 3.11 or 3.12 is required. Install from PyPI:

```bash
uv tool install designaround --python 3.12
# or: python3.12 -m pip install designaround
designaround onboard
```

The same packages are also on the website package index:

```bash
uv tool install designaround --python 3.12 --index https://designaround.elidot.com/simple/
```

This repository is the source for those client packages (`packages/core`, `packages/engine`, `packages/cli`), the agent Skill (`skills/designaround`) and the agent runtime sources the CLI bundles (`agents/`).

`designaround onboard` stores a trial key in your OS keychain. `designaround --version` prints the version on your computer.

## MCP

```bash
designaround mcp
designaround mcp --print-config
```

`designaround mcp` serves seven tools on stdio: screen, design_around, run_status, viewpack, compare, case_list and decision_card. Paid tools need a credit cap. `--print-config` prints the snippet for Claude Code, Codex, Cursor, Claude Desktop and VS Code.

## Skill

```bash
designaround skill install claude-code
```

The same command accepts `cursor`, `codex`, `gemini-cli`, `opencode`, `openclaw`, `kiro` or `all`. The Skill text in this repository is `skills/designaround/SKILL.md`.

## Guides and support

Guides (they stand alone, without this tool): <https://designaround.elidot.com/guides/>

Website: <https://designaround.elidot.com>

Support: <support@elidot.com>. We reply within 3 business days (Monday to Friday, China Standard Time, UTC+8).

## License

Apache-2.0. See [LICENSE](LICENSE).

## 中文

DesignAround 是给产品卖家和设计师用的命令行工具。它把产品外观和美国、中国、欧洲已经公开的外观设计做比对，帮你把设计改到看起来明显不同，并准备专利附图和申请材料包，供你自己去申请。程序在你的电脑上运行。它是软件，不是律师事务所，不提供法律意见。安装、指南和邮箱见上文。官网：<https://designaround.elidot.com>。
