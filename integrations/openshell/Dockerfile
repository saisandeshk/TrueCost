# OpenCode plus the ordinary tools a coding agent expects (curl, python3, bash), so a blocked exfiltration attempt is
# stopped by OpenShell's policy, not by a missing binary. Build: docker build -t truecost/opencode-agent:0.1 .
FROM ghcr.io/anomalyco/opencode:latest
RUN apk add --no-cache curl python3 bash
