# Tracker Server

Backend and web source for the tracker service.

## Workflow

Development and Git operations happen in the local Windows workspace. The
Ubuntu 24.04 server runs deployed revisions under `/opt/tracker`; it is not the
primary editing environment.

## Layout

```text
backend/
  app/          Python backend source
  tests/        Backend tests
  requirements.txt
web/            Web client
docs/           Protocol and deployment documentation
```

The device protocol, runtime dependencies, ports, database, and deployment
method are intentionally left open until their requirements are documented.
