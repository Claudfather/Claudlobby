# Security policy

## Reporting a vulnerability

Report it privately, through GitHub: on this repository's **Security** tab, choose **Report a vulnerability**. The report reaches the maintainers without being made public.

Please do not put the details in a public issue, pull request or discussion. If the **Report a vulnerability** button is missing, open a public issue that says only that you have a security report and asks a maintainer to contact you, with no details in it.

A useful report says what an attacker can do, how to reproduce it, and which commit you tested.

## Supported versions

There are no releases yet. Only the `main` branch is supported.

## Scope

Claudlobby runs Claude Code bots on your machine, under your user account, with shell access. The README's [Safety model](README.md#safety-model) says what a bot can do and what bounds it, and [What it changes on your machine](README.md#what-it-changes-on-your-machine) lists every change setup makes.

In scope:

- a way for someone the operator has not admitted to get a bot to act for them;
- a way past a bound the Safety model states;
- a credential stored anywhere the README does not say;
- a setup or `lib/` script that changes more than the README lists.

Out of scope: what a bot does with access the Safety model says it has (shell access as the operator, for example), and defects in Claude Code or its plugins, which go to their own maintainers.

## What a deployment holds

A running fleet holds its operator's credentials: the Claude Code login or an Anthropic API key, a GitHub token or a GitHub App's private key, a Telegram bot token for each bot, and any other service credential its bots are given. This repository commits none of them: `.env` files and fleet overlays are gitignored. The README's "Where secrets live" says where each one is kept.
