# Current qualification

Run `python3 -B tools/qualify.py --hermes-source /path/to/installed/hermes-agent
--timeout 900 --evidence-dir /path/to/private/new-evidence` from a read-only selected source generation. It resolves the
committed interpreter/site through read-only PM getters, never the launcher,
bootstrap, activation or an install. No dependency installation is attempted.

The full selected candidate suite is run in one process with a 900s outer
bound (refuses <600s), live stderr progress, exact node IDs and outcomes in JSON.
All inherited credentials/profile flags are cleared before imports; a new
private HOME/HERMES_HOME/TMPDIR is used and removed. In-process audit guards
reject external network, hardware, foreign writes and non-Python subprocesses.
These are trusted-source defence-in-depth guards, not an OS security sandbox.
Linux test-only fd path aliases avoid AF_UNIX pathname limits in the deeply
nested disposable workspace; actual private socket/SO_PEERCRED/HMAC are used.

NormalGateway uses real GatewayRunner startup, normal plugin discovery,
registered adapter creation/binding, real AIAgent conversation and send path,
and real gateway stop. Only provider HTTP transport is synthetic. It does not
prove a real model response, physical radio, firmware, USB or audio delivery.
Other native tests label callback fixtures explicitly. Historical v1 reports
are not evidence of current passes. Failed attempts are retained separately.
