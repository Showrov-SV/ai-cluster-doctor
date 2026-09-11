#!/usr/bin/env bash
# Generates a self-signed TLS certificate for local/dev HTTPS testing of the Host.
# For production, use a certificate from a real CA (e.g. Let's Encrypt) instead.
set -e
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout key.pem -out cert.pem -days 365 \
  -subj "/CN=ai-cluster-doctor-host"
echo ""
echo "Generated cert.pem and key.pem next to this script."
echo "Run the Host over HTTPS with:"
echo "  uvicorn main:app --host 0.0.0.0 --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem"
echo ""
echo "Then tell Agents to expect HTTPS by setting on the Host:"
echo "  CLUSTER_DOCTOR_SCHEME=https"
echo "(self-signed certs will fail default verification on Agents - see"
echo "CLUSTER_DOCTOR_VERIFY_TLS=false in agent_core.py for local testing only)"
