import json
import hashlib
import boto3
from decimal import Decimal

TABLE_NAME = "deploy-land-status"

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type,If-None-Match',
    'Access-Control-Expose-Headers': 'ETag,X-Next-Poll-Ms',
}

# ponytail: status→hint map, add entries if new statuses appear
POLL_HINTS = {
    'BUILDING': 3000,
    'DEPLOYING': 3000,
    'TESTING': 3000,
    'IDLE': 8000,
    'SUCCEEDED': 8000,
    'FAILED': 8000,
}


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == int(o) else float(o)
        return super().default(o)


def lambda_handler(event, context):
    try:
        pipelineId = event['pathParameters']['pipelineId']
    except KeyError:
        return {
            'statusCode': 400,
            'headers': CORS_HEADERS,
            'body': json.dumps({'message': "Error: 'pipelineId' missing from path parameters."})
        }

    try:
        response = table.get_item(
            Key={'pipelineID': pipelineId},
            ProjectionExpression='pipelineID, #s, #st, lastUpdated, jobs',
            ExpressionAttributeNames={'#s': 'status', '#st': 'startTime'}
        )

        if 'Item' not in response:
            return {
                'statusCode': 404,
                'headers': CORS_HEADERS,
                'body': json.dumps({'message': f"Item not found for pipelineId: {pipelineId}"})
            }

        item = response['Item']
        body = json.dumps(item, cls=DecimalEncoder, sort_keys=True)
        etag = '"' + hashlib.md5(body.encode()).hexdigest() + '"'

        client_etag = (event.get('headers') or {}).get('If-None-Match') or \
                      (event.get('headers') or {}).get('if-none-match')
        if client_etag == etag:
            return {
                'statusCode': 304,
                'headers': {**CORS_HEADERS, 'ETag': etag},
                'body': ''
            }

        status = str(item.get('status', '')).upper()
        hint = POLL_HINTS.get(status, 5000)

        return {
            'statusCode': 200,
            'headers': {
                **CORS_HEADERS,
                'ETag': etag,
                'X-Next-Poll-Ms': str(hint),
            },
            'body': body
        }

    except Exception as e:
        print(f"DynamoDB error: {e}")
        return {
            'statusCode': 500,
            'headers': CORS_HEADERS,
            'body': json.dumps({'message': 'Internal server error', 'error': str(e)})
        }
