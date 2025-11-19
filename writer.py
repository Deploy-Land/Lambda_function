import json
import boto3
import os
import http.client 
import urllib.parse

# --- 1. DynamoDB 설정 ---
TABLE_NAME = "deploy-land-status"
PK_NAME = "pipelineID" # 사용자의 파티션 키 (D가 대문자)
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)

# --- 2. 디스코드 & 슬랙 Webhook URL ---
DISCORD_URL = os.environ.get('DISCORD_WEBHOOK_URL', None)
SLACK_URL = os.environ.get('SLACK_WEBHOOK_URL', None)

# CodePipeline API에 접근하기 위한 클라이언트 생성
codepipeline_client = boto3.client('codepipeline')

# Bedrock 클라이언트 리전 설정 + 사용 모델 설정
bedrock_runtime = boto3.client('bedrock-runtime', region_name='ap-northeast-2')
BEDROCK_MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"

LOG_GROUP_NAME = "/aws/codebuild/sample-app2-eb-build"
LOG_GROUP_NAME_DEPLOY = "/aws/codebuild/deployer-project"
CLOUDWATCH_CONSOLE_BASE = "https://ap-northeast-2.console.aws.amazon.com/cloudwatch/home?region=ap-northeast-2#logs:log-group"

def lambda_handler(event, context):
    print(f"Received event: {json.dumps(event)}")
    
    try:
        # --- 3. EventBridge 이벤트 파싱 ---
        pipeline_id = event['detail']['execution-id']
        pipeline_name = event['detail']['pipeline'] # API 호출에 필요
        stage_name = ""
        status = ""

        # 'build_id'의 기본값(fallback)으로 파이프라인 ID를 먼저 설정
        build_id = event['detail'].get('execution-id')

        # --- 3.1. CodePipeline API를 통해 파이프라인 이름 추출 ---
        if event['detail-type'] == 'CodePipeline Stage Execution State Change':
            # "Stage" 이벤트는 'stage' 필드를 stage_name으로 사용 (예: Source, Build, Deploy)
            stage_name = event['detail']['stage']
            status = event['detail']['state']
            
            # "Stage" 레벨의 FAILED 이벤트는 무시 (Action 이벤트가 진짜 헤드라인을 가짐)  
            if status == 'FAILED':
                print(f"Ignoring STAGE-level FAILED event for: {stage_name}")
                return { 'statusCode': 200 } # 람다 종료-> 이거 안하면 계속 헤더 로그가 덮어씌워짐

        elif event['detail-type'] == 'CodePipeline Action Execution State Change':
            # 3. "Action" 이벤트는 오직 "FAILED" 상태일 때 (헤드라인이 있을 때)만 사용합니다.
            stage_name = event['detail']['stage'] 
            status = event['detail']['state']

            # "Action" 이벤트의 'STARTED', 'SUCCEEDED'는 "Stage" 이벤트와 중복되므로 무시합니다.
            if status != 'FAILED':
                print(f"Ignoring duplicate ACTION-level {status} event for: {stage_name}")
                return { 'statusCode': 200 }
            
            # (이 코드는 "Action: FAILED" 이벤트만 통과시킴)
            if stage_name in ('Build', 'Deploy'):
                for artifact in event['detail'].get('output-artifacts', []):
                    if 'codeBuildId' in artifact:
                        build_id = artifact['codeBuildId']
                        break

        else:
            print(f"Ignoring event type: {event['detail-type']}")
            return
            
        error_message = ""
        ai_solution = ""

        if status == 'FAILED':
            try:
                error_message = event['detail']['execution-result']['external-execution-summary']
            except KeyError:
                error_message = "Unknown error (no execution-summary)."
            
            ai_solution = get_bedrock_solution(error_message)
            
        # --- 4. DynamoDB에 상태 업데이트 (쓰기) ---
        print(f"Updating DynamoDB: Key={pipeline_id}, Stage={stage_name}, Status={status}")
        
        # "첫 번째 이벤트" (Source: STARTED)일 때만 전체 구조를 가져옵니다.
        if stage_name == 'Source' and status == 'STARTED':
            try:
                response = codepipeline_client.get_pipeline(name=pipeline_name)
                stages = response['pipeline']['stages']
                stage_list = [s['name'] for s in stages] # 예: ['Source', 'Build', 'Deploy']
                stage_count = len(stage_list) 
                log_url = generate_log_url(stage_name, build_id)
                ai_solution = ""

                print(f"Pipeline Structure: {stage_count} stages found: {stage_list}")

                # 웹 소켓으로 확장 가능하나 해커톤 시간 상 후순위로 
                print(f"Updating LATEST_EXECUTION pointer to: {pipeline_id}")
                table.update_item(
                    Key={ PK_NAME: "LATEST_EXECUTION" }, # "LATEST_EXECUTION"이라는 "고정된" ID
                    UpdateExpression="SET latestExecutionId = :pid, lastStartTime = :time",
                    ExpressionAttributeValues={
                        ':pid': pipeline_id, # "새 파이프라인 ID"로 덮어쓰기
                        ':time': event['time'] # "언제 시작했는지" 시간도 저장
                    }
                )

                # DynamoDB 저장
                table.update_item(
                    Key={ PK_NAME: pipeline_id },
                    UpdateExpression="SET currentStage = :stage, #s = :status, errorMessage = :errMsg, totalStages = :tc, stageList = :sl, logUrl = :lUrl",
                    ExpressionAttributeNames={'#s': 'status'}, 
                    ExpressionAttributeValues={
                        ':stage': stage_name, # 현재 스테이지 이름 ['Source', 'Build', 'Deploy']
                        ':status': status, # STARTED, IN_PROGRESS, SUCCEEDED, FAILED
                        ':errMsg': error_message,
                        ':tc': stage_count,  # 총 스테이지 개수
                        ':sl': stage_list,    # 스테이지 이름 목록
                        ':lUrl': log_url, # 에어 로그
                        ':ai': ai_solution # AI 에러 로그 반환 
                    }
                )
            except Exception as e:
                print(f"Error getting pipeline structure: {e}")
                update_simple_status(pipeline_id, stage_name, status, error_message, build_id=build_id, ai_solution=ai_solution)
        
        else:
            # "첫 번째 이벤트"가 아닐 경우, 상태만 업데이트
            update_simple_status(pipeline_id, stage_name, status, error_message, build_id=build_id, ai_solution=ai_solution)

        # --- 5. 알림 보내기 ---
        send_notification(pipeline_id, stage_name, status, error_message, ai_solution)

        return { 'statusCode': 200 }

    except Exception as e:
        print(f"Error processing event: {e}")
        return { 'statusCode': 200, 'body': json.dumps(f"Error: {str(e)}") }

# --- Bedrock API 호출 헬퍼 함수 ---
def get_bedrock_solution(error_headline):
    import time
    
    try:
        prompt = f"""
        AWS CodePipeline 빌드가 실패했습니다.
        실패 요약: "{error_headline}"
        이 오류의 의미가 무엇이며, 어떻게 해결할 수 있는지 3줄 요약으로 한국어로 설명해주세요.
        """
        
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 500,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.7,
            "top_p": 0.9
        })
        
        print(f"Calling Bedrock - Model: {BEDROCK_MODEL_ID}, Region: ap-northeast-2")
        
        # Exponential backoff 재시도 로직
        max_retries = 3
        base_delay = 2
        
        for attempt in range(max_retries):
            try:
                response = bedrock_runtime.invoke_model(
                    body=body,
                    modelId=BEDROCK_MODEL_ID,
                    contentType='application/json',
                    accept='application/json'
                )
                break
                
            except Exception as retry_error:
                if "ThrottlingException" in str(retry_error) and attempt < max_retries - 1:
                    wait_time = base_delay * (2 ** attempt)
                    print(f"ThrottlingException 발생. {wait_time}초 후 재시도... (시도 {attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                else:
                    raise
        
        response_body = json.loads(response.get('body').read())
        print(f"Bedrock response: {json.dumps(response_body)}")
        
        if 'content' in response_body and len(response_body['content']) > 0:
            solution_text = response_body['content'][0].get('text', 'AI가 응답을 생성하지 못했습니다.')
        else:
            solution_text = 'AI 응답 형식이 올바르지 않습니다.'
        
        print(f"Bedrock Solution: {solution_text}")
        return solution_text
        
    except Exception as e:
        error_msg = str(e)
        print(f"Error calling Bedrock: {error_msg}")
        print(f"Error type: {type(e).__name__}")
        
        if "ValidationException" in error_msg:
            return "모델 ID가 잘못되었거나 해당 리전에서 사용할 수 없는 모델입니다."
        elif "AccessDeniedException" in error_msg:
            return "Bedrock 모델 액세스 권한이 없습니다. IAM 정책을 확인하세요."
        elif "ResourceNotFoundException" in error_msg:
            return "요청한 모델을 찾을 수 없습니다. 모델 ID와 리전을 확인하세요."
        else:
            return f"Bedrock AI 호출 실패: {error_msg}"


# --- 상태만 간단히 업데이트하는 헬퍼 함수 ---
def update_simple_status(pipeline_id, stage_name, status, error_message, build_id=None, ai_solution=""):
    log_url = generate_log_url(stage_name, build_id)
    
    UpdateExpression = "SET currentStage = :stage, #s = :status, errorMessage = :errMsg, logUrl = :lUrl, aiSolution = :ai"

    ExpressionAttributeValues = {
        ':stage': stage_name,
        ':status': status,
        ':errMsg': error_message,
        ':lUrl': log_url,
        ':ai': ai_solution
    }

    table.update_item(
        Key={ PK_NAME: pipeline_id },
        UpdateExpression=UpdateExpression,
        ExpressionAttributeNames={'#s': 'status'}, 
        ExpressionAttributeValues= ExpressionAttributeValues
    )

# --- Log URL 생성 헬퍼 함수 ---
def generate_log_url(stage_name, build_id):
    # 'Source' 단계는 로그가 없음,  Build/Deploy 단계에서만 생성
    if stage_name in ('Build', 'Deploy') and build_id:
        log_group = ""
        if stage_name == 'Build':
            log_group = LOG_GROUP_NAME
        elif stage_name == 'Deploy':
            log_group = LOG_GROUP_NAME_DEPLOY
        else:
            return ""
        log_group_encoded = urllib.parse.quote_plus(log_group)
        log_stream_encoded = urllib.parse.quote_plus(build_id)

        return f"{CLOUDWATCH_CONSOLE_BASE}/{log_group_encoded}/log-stream/{log_stream_encoded}"
    return ""

# --- 알림 전송 헬퍼 함수 ---
def send_notification(pipeline_id, stage_name, status, error_message, ai_solution=""):
    message = ""

    if status == 'STARTED' and stage_name == 'Source':
        message = f"🚀 [Deploy Land] '{pipeline_id[:8]}' 배포가 시작되었습니다!"
    elif status == 'STARTED' and stage_name == 'Build':
        message = f"🔨 [Deploy Land] '{pipeline_id[:8]}' 빌드 시작! Build 중입니다..."
    elif status == 'SUCCEEDED' and stage_name == 'Build':
         message = f"✅ [Deploy Land] '{pipeline_id[:8]}' 빌드 성공! Deploy 단계로 이동합니다..."
    elif status == 'STARTED' and stage_name == 'Deploy':
        message = f"🚚 [Deploy Land] '{pipeline_id[:8]}' 배포 시작! Deploy 중입니다..."
    elif status == 'SUCCEEDED' and stage_name == 'Deploy':
        message = f"🎉 [Deploy Land] '{pipeline_id[:8]}' 배포 성공!"
    
    elif status == 'FAILED':
        message = f"🐛 앗! **[{stage_name}]** 단계에서 배포 실패!\n> **이유:** {error_message}"
        
        # DB에서 로그 URL 가져오기
        item = get_item_from_db(pipeline_id)
        log_url = item.get('logUrl', '')

        if ai_solution:
            message += f"\n** お父さんの　ワン :**\n> {ai_solution}"
        if log_url:
            message += f"\n> **로그 확인:** {log_url}"
            
    if message:
        if DISCORD_URL: send_discord_notification(message)
        if SLACK_URL: send_slack_notification(message.replace("**", "*"))

# --- DB에서 값들 가져오기 ---
def get_item_from_db(pipeline_id):
    try:
        response = table.get_item(Key={PK_NAME: pipeline_id})
        return response.get('Item', {})
    except Exception:
        return {}
        
# --- Discord 알림 헬퍼 함수 ---
def send_discord_notification(message):
    try:
        url = http.client.urlsplit(DISCORD_URL)
        conn = http.client.HTTPSConnection(url.hostname)
        payload = json.dumps({'content': message}) 
        headers = {'Content-Type': 'application/json'}
        conn.request("POST", url.path, payload, headers)
        conn.getresponse()
        conn.close()
    except Exception as e: print(f"Error sending to Discord: {e}")

# --- Slack 알림 헬퍼 함수 ---
def send_slack_notification(message):
    try:
        url = http.client.urlsplit(SLACK_URL)
        conn = http.client.HTTPSConnection(url.hostname)
        payload = json.dumps({'text': message})
        headers = {'Content-Type': 'application/json'}
        conn.request("POST", url.path, payload, headers)
        conn.getresponse()
        conn.close()
    except Exception as e: print(f"Error sending to Slack: {e}")