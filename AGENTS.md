# Project guidance

## Working model

- Develop and test in the local Windows workspace.
- Use Git for source control and transfer.
- Treat the Ubuntu server as a runtime/deployment target only.
- Do not edit source directly under `/opt/tracker` on the server.
- Never commit credentials, private keys, tokens, or production data.

## Repository layout

- `backend/app`: Python backend source
- `backend/tests`: automated backend tests
- `web`: web client source
- `docs`: protocol and deployment documentation

## Development rules

- Keep TCP framing/parsing separate from connection handling.
- Keep persistence behind functions or classes in `database.py`.
- Add tests for protocol samples before changing parser behavior.
- Read runtime settings such as ports and secrets from environment variables.
