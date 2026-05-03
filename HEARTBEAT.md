# HEARTBEAT.md

Built-in OpenClaw heartbeat is disabled for this project. The production health check runs as the durable cron job `heartbeat` defined in `setup/cron/heartbeat.md`.

If this file is invoked by an older live config, do not introduce a second health-check path. Re-register or repair the `heartbeat` cron from `setup/cron/heartbeat.md`, then let that cron read `docs/heartbeat-checks.md` and execute the checks.
