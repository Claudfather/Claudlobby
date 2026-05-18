# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Security

- Restrict .env file permissions to 0o600 (owner read/write only) in scaffold_env_files and env-migrate. Previously these files were created with default umask (typically 0o644), making fleet secrets world-readable on shared hosts.
