#!/bin/sh
# LocalStack lo ejecuta al arrancar.
set -e
awslocal s3 mb s3://facturas
for q in q-ingesta q-gpu; do
  awslocal sqs create-queue --queue-name $q-dlq
  awslocal sqs create-queue --queue-name $q --attributes \
    "{\"VisibilityTimeout\":\"120\",\"RedrivePolicy\":\"{\\\"deadLetterTargetArn\\\":\\\"arn:aws:sqs:us-east-1:000000000000:$q-dlq\\\",\\\"maxReceiveCount\\\":\\\"3\\\"}\"}"
done
awslocal sqs create-queue --queue-name q-validacion
