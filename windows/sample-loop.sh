#!/usr/bin/env bash
# Two more read-only samples, ~40s apart, spanning two natural 1-minute worker cycles.
set -u
for i in 1 2; do
  sleep 40
  timeout 60 ./winrun.sh probe-queue.ps1 > "sample-t${i}.json" 2>"sample-t${i}.err"
  echo "sample-t${i} exit=$? at $(date -u +%H:%M:%S)"
done
