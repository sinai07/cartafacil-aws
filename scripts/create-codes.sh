#!/bin/bash
# Creates one code per person (4 letters each) in the DynamoDB table.
# Run it in AWS CloudShell (us-east-1). Edit the list at the bottom: CODE NAME
# Existing codes are never overwritten (attribute_not_exists).
while read -r CODE NAME; do
  aws dynamodb put-item --table-name letter-explainer-codes \
    --item "{\"code\":{\"S\":\"$CODE\"},\"friend\":{\"S\":\"$NAME\"},\"uses_left\":{\"N\":\"4\"},\"active\":{\"BOOL\":true}}" \
    --condition-expression "attribute_not_exists(code)" < /dev/null 2>/dev/null \
    && echo "Created: $CODE ($NAME)" || echo "Already exists or failed: $CODE ($NAME)"
done <<LIST
CARTA-ANA Ana
CARTA-LUIS Luis
TEST-7K2P Test
LIST
