# CLAUDE.md

@AGENTS.md

Follow the shared repository instructions in AGENTS.md. This checkout manages
Studio and the Python launcher using published browser artifacts; it does not
compile Firefox. The staged upgrade and current implementation status are in
`docs/studio-client-server.md`.

Start with the affected test file and capture its output in a temporary log.
The full Python gate is `python -m ci.run_pythonlib`; Vue tests/build run in
`frontend/studio`. Real native acceptance is separate in `service-tester`.
