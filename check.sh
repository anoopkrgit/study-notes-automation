#!/bin/bash
sleep 45
RUN_ID=$(gh run list -L 1 --json databaseId -q '.[0].databaseId')
echo "Run ID: $RUN_ID"
gh run view $RUN_ID --log | grep -i 'review'
