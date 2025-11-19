import json
import boto3
import os

# 1. DynamoDB 테이블 이름
TABLE_NAME = "deploy-land-status"

# Boto3 리소스를 초기화합니다.
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)

def lambda_handler(event, context):
    print(f"Received event: {json.dumps(event)}")

    try:
        pipelineId = event['pathParameters']['pipelineId']
    except KeyError:
        print("Error: 'pipelineId' missing from path parameters.")
        return {
            'statusCode': 400,
            'body': json.dumps({'message': "Error: 'pipelineId' missing from path parameters."})
        }

    try:
        # 3. DynamoDB에서 GetItem 수행 (대소문자 수정됨)
        response = table.get_item(
            Key={
                'pipelineID': pipelineId  
            }
        )
        
        if 'Item' not in response:
            print(f"Item not found for pipelineId: {pipelineId}")
            return {
                'statusCode': 404,
                'body': json.dumps({'message': f"Item not found for pipelineId: {pipelineId}"})
            }
        
        item = response['Item']
        print(f"Found item: {json.dumps(item)}")
        
        return {
            'statusCode': 200,
            'body': json.dumps(item)
        }
        
    except Exception as e:
        print(f"DynamoDB error: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({'message': 'Internal server error', 'error': str(e)}) # 에러 메시지를 포함
        }